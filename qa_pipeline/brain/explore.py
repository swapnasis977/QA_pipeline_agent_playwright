"""The App Explorer — live application in, ledger cards out.

This is the module that makes zero-touch possible at all, and it exists to
resolve a contradiction in the design. The pipeline forbids guessing: a
locator may not enter generated code unless something observed it. Taken
alone, that rule means every unknown locator becomes a scripting task on a
human's desk — which is precisely what we are trying to eliminate.

So a decline must escalate to ANOTHER WAY OF GETTING GROUND TRUTH rather than
to a person. That is this module. When the Planner cannot resolve an element,
the Explorer navigates to the screen and looks.

It reuses `utils/dom_inspector.inspect()` wholesale rather than driving
Playwright itself. That function already carries a great deal of earned
behavior — declared-route navigation from `utils/route_map`, multi-step
keyword navigation, blocking-dialog dismissal, login caching, the MCP backend
with a direct-Playwright fallback, and a launch-failure latch so 74 cases do
not mean 74 thirty-second connection attempts. Re-implementing crawling here
would mean re-learning all of it.

What this module adds on top is the part that was missing: turning a
throw-away snapshot into DURABLE, sourced cards, and computing the one thing
`_snapshot` does not — whether a role locator is actually unambiguous.

AMBIGUITY. `_snapshot` checks uniqueness for testid, id and css candidates
(`isUnique(candidate)` in its injected JS) but cannot for a role locator,
because role+name uniqueness is a property of the accessibility tree rather
than of a CSS query. That gap is not academic: an ambiguous
`getByRole('button', {name: /Identification/i})` matching both a sidebar item
and a dashboard tile was the single root cause of every Executor failure in
one real run. Here, duplicate (role, name) pairs within a snapshot are
counted and the resulting cards are marked `unique=False`, which
`trust.can_ground_locator` then refuses to let into generated code.
"""

from __future__ import annotations

import contextlib
import json
import re

from utils import dom_inspector
from utils.timestamps import utc_iso

from . import cards as card_builders

#: Snapshot fields that mean "this element is not addressable" — no locator
#: was derivable, so there is nothing to record.
_UNUSABLE = (None, "")


#: An id with a long digit run AND letters in it — `#confirmDlg_1788417737161`,
#: `#tooltip73348`. These are generated per render: the same control came back
#: as `#tooltip164575` on one crawl and `#tooltip73348` on the next, so a card
#: built on one is a locator that is already stale by the time it is written,
#: and two snapshots of the SAME screen no longer compare equal. A purely
#: numeric id (this app's `#4000` / `#4001` device entries) is left alone —
#: those are stable domain keys, not render counters.
_ID_IN_LOCATOR_RE = re.compile(r'#([A-Za-z0-9_-]+)')


def _is_volatile(expression: str) -> bool:
    """Whether this locator hangs off an id the app regenerates per render."""
    for ident in _ID_IN_LOCATOR_RE.findall(expression or ""):
        if not any(ch.isalpha() for ch in ident):
            continue  # a purely numeric id is a domain key, not a counter
        run = 0
        for ch in ident:
            run = run + 1 if ch.isdigit() else 0
            if run >= 5:
                return True
    return False


def locator_expression(element: dict, exact: bool = False) -> str:
    """Builds the Playwright expression a test would actually write.

    `_snapshot` emits `locator` in two different shapes depending on
    `locatorType` — a CSS/testid selector string, or a JSON role payload —
    and its own comment notes that callers must branch on `locatorType`
    rather than treating the field uniformly. Doing that branching in ONE
    place is why this function exists; every caller that re-derived it was a
    chance to emit `page.locator('{"role":"button",...}')`.
    """
    kind = element.get("locatorType")
    raw = element.get("locator")
    if raw in _UNUSABLE:
        return ""

    if kind == "role":
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError):
            return ""
        role, name = payload.get("role"), payload.get("name")
        if not role or not name:
            return ""
        exact_opt = ", exact: true" if exact else ""
        return f"page.getByRole('{role}', {{ name: {json.dumps(name)}{exact_opt} }})"

    if kind == "testid" and element.get("testId"):
        return f"page.getByTestId({json.dumps(element['testId'])})"

    if kind in ("id", "css"):
        return f"page.locator({json.dumps(raw)})"

    return ""


#: The snapshot truncates accessible names to this many characters; a name at
#: the limit is a prefix, so an exact-name match on it would never hit.
_NAME_TRUNCATED_AT = 80


def _ambiguity_counts(elements: list) -> dict:
    """(role, name) -> how many elements in this snapshot MATCH it the way
    getByRole does: same role and an accessible name that CONTAINS the name
    (case-insensitive substring). Counting only identical pairs missed
    `Save` vs `Save draft`, which getByRole('button', {name: 'Save'}) hits both
    of at run time (a strict-mode violation).

    Only role locators need this; the other kinds were already uniqueness-
    checked in the browser. See the module docstring for the run this
    prevents.
    """
    pairs = [(el.get("role"), (el.get("name") or "").strip())
             for el in elements or [] if el.get("locatorType") == "role"]
    counts = {}
    for role, name in pairs:
        low = name.lower()
        counts[(role, name)] = sum(1 for r, n in pairs if r == role and low in n.lower())
    return counts


def _exact_counts(elements: list) -> dict:
    """(role, name) -> how many role-located elements carry exactly that pair."""
    counts = {}
    for el in elements or []:
        if el.get("locatorType") != "role":
            continue
        key = (el.get("role"), (el.get("name") or "").strip())
        counts[key] = counts.get(key, 0) + 1
    return counts


def snapshot_to_cards(snapshot: dict, screen_name: str, *,
                      source: str = "crawled", when: str = None) -> list:
    """Converts one DOM snapshot into screen + element cards.

    Elements with no derivable locator are skipped rather than recorded with
    an empty one: a card that cannot ground anything is not knowledge, it is
    noise that later fuzzy-matches against a real request and wins.
    """
    when = when or utc_iso()
    elements = snapshot.get("elements") or []
    out = []

    route = ""
    url = snapshot.get("url") or ""
    if url:
        # Path only — the host belongs to the environment, not the app's
        # structure, and baking it in would make the ledger environment-
        # specific for no benefit.
        route = "/" + url.split("://", 1)[-1].split("/", 1)[-1] if "://" in url else url

    out.append(card_builders.screen(
        screen_name, source,
        route=route,
        purpose=snapshot.get("title") or "",
        reached_by=list(snapshot.get("navigation_path") or []),
        evidence=url,
        when=when,
    ))

    counts = _ambiguity_counts(elements)
    exact_counts = _exact_counts(elements)
    seen = set()
    for el in elements:
        use_exact = False
        if el.get("locatorType") == "role":
            nm = (el.get("name") or "").strip()
            key = (el.get("role"), nm)
            # Several elements contain this name but exactly one IS it: pin the
            # locator with exact:true instead of recording an ambiguous one.
            use_exact = (counts.get(key, 0) > 1 and exact_counts.get(key, 0) == 1
                         and len(nm) < _NAME_TRUNCATED_AT)
        expression = locator_expression(el, exact=use_exact)
        if not expression:
            continue
        if _is_volatile(expression):
            # Recording it would write a fact that expires on the next render.
            continue
        name = (el.get("name") or "").strip()
        if not name:
            # An unnamed element cannot be looked up by a test case's
            # wording later, so it would never be retrieved anyway.
            continue
        if name in seen:
            continue
        seen.add(name)

        unique = True
        if el.get("locatorType") == "role":
            unique = use_exact or counts.get((el.get("role"), name), 0) <= 1

        out.append(card_builders.element(
            screen=screen_name,
            logical_name=name,
            locator=expression,
            source=source,
            role=el.get("role") or "",
            accessible_name=name,
            unique=unique,
            evidence=url,
            when=when,
        ))

    # ONE edge: app root -> this screen, the whole path as its action. The steps
    # are ACTIONS ("reset to app home", "click 'X'", "goto ..."), not screens, so
    # chaining them as from->to pairs recorded edges like `click 'A' -> Screen`
    # whose "from" is not a screen at all. Only the first of those was valid.
    path = [str(step) for step in (snapshot.get("navigation_path") or [])]
    if path:
        out.append(card_builders.nav_edge("app root", screen_name, " > ".join(path), source,
                                          evidence=url, when=when))

    return out


def _fingerprint(cards: list) -> frozenset:
    """What this snapshot actually saw, as an identity.

    Two crawls that come back with the same element set saw the same screen,
    whatever names they were asked for. In a single-page app the URL cannot
    tell them apart — every screen in this project's target app is served
    from `/dashboard/home/?customer_code=demo` — so the elements are the
    only available evidence.
    """
    return frozenset(
        ((c.get("value") or {}).get("logical_name", ""),
         (c.get("value") or {}).get("locator", ""))
        for c in cards if c.get("type") == "element")


def crawl(ledger, targets, *, source: str = "crawled", verbose: bool = True) -> dict:
    """Visits each target and folds what it finds into the ledger.

    `targets` is a list of `{"screen": ..., "context": ...}` — `context` is
    the free-text flow description `dom_inspector.inspect` navigates by, and
    should use the app's REAL vocabulary (retrieve a glossary block first if
    the test case's wording differs, which is the usual case).

    Returns a per-target report. Never raises: an app that cannot be reached
    is a normal, expected outcome that must be reported and escalated, not an
    exception that kills the run — the same judgment `executor.run` makes by
    returning its failures rather than raising them.
    """
    when = utc_iso()
    report = {"visited": [], "failed": [], "cards": 0, "ambiguous": 0}
    seen_fingerprints = {}

    # The screens THIS caller declared. Only these are authoritative evidence
    # for the "never left that screen" check below — see
    # `Ledger.screen_matching`. A targeted `discover()` re-crawl passes a
    # single ad-hoc name here, so the cross-run half of the guard stands down
    # for it; its result is validated by `resolve_element` immediately after,
    # which is the stronger check for a one-element lookup anyway.
    declared_screens = {(t.get("screen") or t.get("context") or "unknown")
                        for t in (targets or [])}

    for target in targets or []:
        screen = target.get("screen") or target.get("context") or "unknown"
        context = target.get("context") or screen

        try:
            snapshot = dom_inspector.inspect(context)
        except Exception as exc:
            report["failed"].append({"screen": screen, "reason": f"{type(exc).__name__}: {exc}"})
            if verbose:
                print(f"      ⚠️  Explorer could not reach {screen!r}: {exc}")
            continue

        if not snapshot.get("elements"):
            reason = snapshot.get("note") or snapshot.get("launch_error") or "no elements captured"
            report["failed"].append({"screen": screen, "reason": reason})
            if verbose:
                print(f"      ⚠️  Explorer reached nothing usable for {screen!r}: {reason}")
            continue

        # A SNAPSHOT OF THE LOGIN PAGE IS NOT A FACT ABOUT THE APP.
        # `inspect` reports this case explicitly rather than raising, because
        # for an inspector "here is the login screen" is a truthful answer.
        # For the ledger it is a falsehood waiting to happen: recorded under
        # the requested screen's name it replaces that screen's real elements
        # with the identity provider's email and password fields, and every
        # later run then "grounds" the app's own controls on them.
        if snapshot.get("logged_in") is False:
            # `note` is only set on some paths (the declared-route branch sets
            # none), so requiring it let a failed login with a route that had
            # no `expect` file the IDP's login form under the screen's name.
            reason = snapshot.get("note") or "the session is not logged in"
            report["failed"].append({"screen": screen, "reason": reason})
            if verbose:
                print(f"      ⚠️  Refusing to record {screen!r}: not logged in — {reason}")
            continue

        # A DECLARED ROUTE THAT DID NOT LAND IS A FAILED CRAWL, NOT A SNAPSHOT.
        # The keyword fallback after a failed route reaches "something", and
        # filing that under this screen's name is how dashboard elements ended
        # up recorded as "Image Editing Tool" cards.
        broken = snapshot.get("route_failed") or snapshot.get("route_unverified")
        if broken:
            reason = (f"declared route {broken!r} "
                      + ("failed and navigation fell back to keyword guessing"
                         if snapshot.get("route_failed")
                         else "ran but its expected element never appeared"))
            report["failed"].append({"screen": screen, "reason": reason})
            if verbose:
                print(f"      ⚠️  Refusing to record {screen!r}: {reason}.")
            continue

        new_cards = snapshot_to_cards(snapshot, screen, source=source, when=when)

        # A screen with no groundable element is not a visit. Every element was
        # volatile or unnamed, so there is nothing to record — and an empty
        # fingerprint disarmed the identical-snapshot guard below, after which a
        # second such screen was refused as a "copy" of the first.
        if not any(c.get("type") == "element" for c in new_cards):
            reason = "the snapshot had no groundable (named, stable) elements"
            report["failed"].append({"screen": screen, "reason": reason})
            if verbose:
                print(f"      ⚠️  Refusing to record {screen!r}: {reason}.")
            continue

        # A CRAWL MUST SHOW IT ACTUALLY MOVED.
        #
        # `dom_inspector.inspect` returns a snapshot of whatever page is on
        # screen even when it could not navigate anywhere — the right contract
        # for an inspector, but folding that into the ledger under the
        # REQUESTED screen's name records the page it never left as facts
        # about a screen nobody reached. Observed exactly that way here:
        # "Context Information", "Capture Device", "Profile Details" and
        # "Review" were each stored as a verbatim copy of the dashboard, and
        # the Planner then "grounded" the registration flow's Register button on the
        # dashboard's Registration side-menu item. A locator that reaches the
        # wrong element is worse than none: no locator escalates to a
        # demonstration, a wrong one compiles, runs, and fails in Playwright.
        #
        # The URL cannot detect this — every screen in a single-page app is
        # served from one address — so the element set is the evidence. An
        # identical set under a different name means the inspector did not
        # move, whatever it reports about clicks it made along the way. Two
        # genuinely distinct screens that snapshot identically are
        # indistinguishable to every consumer downstream too, so refusing the
        # second costs nothing real.
        fingerprint = _fingerprint(new_cards)
        already = seen_fingerprints.get(fingerprint) or ledger.screen_matching(
            fingerprint, exclude=screen, restrict_to=declared_screens)
        if already:
            reason = (f"the snapshot is identical to {already!r} — the inspector "
                      f"never left that screen")
            report["failed"].append({"screen": screen, "reason": reason})
            if verbose:
                print(f"      ⚠️  Refusing to record {screen!r}: {reason}.")
            continue
        seen_fingerprints[fingerprint] = screen

        tally = ledger.upsert_many(new_cards)
        ambiguous = sum(1 for c in new_cards
                        if c.get("type") == "element"
                        and (c.get("value") or {}).get("unique") is False)

        report["visited"].append({
            "screen": screen,
            "url": snapshot.get("url", ""),
            "elements": sum(1 for c in new_cards if c.get("type") == "element"),
            "ambiguous": ambiguous,
            **tally,
        })
        report["cards"] += len(new_cards)
        report["ambiguous"] += ambiguous

        if verbose:
            print(f"      Explored {screen!r}: {tally['added']} new, "
                  f"{tally['updated']} updated, {tally['kept_existing']} already known"
                  + (f", {ambiguous} AMBIGUOUS (unusable)" if ambiguous else ""))

    return report


def discover(ledger, logical_name: str, screen: str = "", context: str = "") -> dict:
    """Targeted re-crawl for ONE element the pipeline could not resolve.

    This is the second rung of the escalation ladder, called by
    `verifiers.repair_loop` the moment a plan or a generated file references
    something the ledger does not know. It is what keeps an unknown locator
    from becoming a human's problem.

    Returns `{"found": bool, "card": ..., "reason": ...}`. A miss here is not
    a failure — it is the signal to climb one more rung (ask for a recorded
    demonstration), and `reason` is what gets shown to the human when it does.
    """
    context = context or (f"{screen} {logical_name}".strip())
    before = ledger.resolve_element(logical_name, screen, groundable_only=True)
    if before is not None:
        return {"found": True, "card": before, "reason": "already in the ledger"}

    report = crawl(ledger, [{"screen": screen or logical_name, "context": context}],
                   verbose=False)
    if report["failed"]:
        return {"found": False, "card": None,
                "reason": f"crawl could not reach it: {report['failed'][0]['reason']}"}

    after = ledger.resolve_element(logical_name, screen, groundable_only=True)
    if after is not None:
        return {"found": True, "card": after, "reason": "discovered by targeted crawl"}

    near = ledger.resolve_element(logical_name, screen, groundable_only=False)
    if near is not None:
        from . import trust
        return {"found": False, "card": near,
                "reason": f"crawl reached the screen but the element is not usable: "
                          f"{trust.decay_note(near)}"}
    return {"found": False, "card": None,
            "reason": (f"crawl reached the screen but found nothing named "
                       f"{logical_name!r} — the screen may need a demonstration "
                       f"recording, or the name in the test case may not match the UI")}


def close():
    """Releases the browser session. The orchestrator calls this once at the
    end of a run; `dom_inspector` caches a logged-in page across calls on
    purpose, so closing per-crawl would pay the login cost every time."""
    with contextlib.suppress(Exception):
        dom_inspector.close()
