"""The hard gate: no locator reaches generated code unless it was observed.

`utils/locator_verifier.py` already does the hard part, and its docstring
states the case better than a summary could: it is "the highest-value gate in
the pipeline", because "the model writes competent Playwright structure, and
the thing it cannot know is the real selector". It catches both failure
shapes — a locator nothing corroborates, and a locator that corroborates
against MORE THAN ONE element, which "visibly works until Playwright's strict
mode refuses to click at RUN time".

What that module deliberately does NOT do is act. It reports, and its own
docstring explains why: a snapshot holds ~60 elements of ONE screen, so an
element behind a tab or further down a flow is legitimately absent from it.
Against a snapshot alone, "unverified" honestly means "this snapshot does not
corroborate it", not "this locator is wrong". Failing a build on that would
reject correct code constantly.

This module is what makes acting on it safe, by adding the thing that was
missing: a LEDGER. Against the ledger, absence means something much stronger —
no crawl, no recording and no passing run has ever seen this element — and
that is a defensible reason to refuse the code. And when the ledger cannot
answer, the escalation is not a decline but a targeted crawl
(`brain.explore.discover`), so the gate stays hard without becoming a wall.

The three tiers, in order:
  1. ledger hit          -> verified, free.
  2. targeted crawl      -> verified, cheap, and permanently learned.
  3. snapshot near-miss  -> a repair instruction carrying the element's REAL
                            accessible name, so the fix costs one model call
                            and no further inspection.
Anything that survives all three is reported with a reason a human can act on.
"""

from __future__ import annotations

from qa_pipeline.brain import explore, trust
from utils import locator_verifier

from .result import VerifyResult

#: Locator kinds we do not attempt to ground against the ledger. A CSS chain
#: like `.card > div:nth-child(2)` is often perfectly valid without any
#: element carrying that exact selector — `locator_verifier`'s docstring
#: flags this as one of its two deliberate limits, and grounding it would
#: manufacture false failures rather than catch real ones.
_UNGROUNDABLE_KINDS = {"css"}


#: Locator kinds whose argument IS the accessible name Playwright matches on
#: at run time. For these, resolving a ledger card is not enough — the name
#: written in the code has to be one the real element would actually answer to.
_NAME_MATCHED_KINDS = {"role", "label", "text", "placeholder", "title", "altText", "alttext"}


def _normalize(text: str) -> str:
    return " ".join((text or "").split()).strip().lower()


def _name_would_match(written: str, card: dict) -> bool:
    """Whether Playwright would actually match `written` against this element.

    `getByRole(name=...)` — and the getByLabel/getByText/getByPlaceholder
    family — match the element's accessible name as a CASE-INSENSITIVE,
    WHITESPACE-NORMALIZED SUBSTRING unless `exact: true` is passed. So this
    is not a heuristic about similarity; it is the runtime matcher's own rule,
    applied one stage earlier.

    It exists because ledger resolution alone let an invented locator through
    the hard gate. A generated `getByRole('button', { name: /recapture/i })`,
    carrying its own TODO admitting nothing had inspected that screen,
    resolved to a card whose real accessible name is "Start Capture button.
    Click to capture the Face and Iris of the candidate". The names have a
    word in common and nothing else — at run time Playwright finds no element,
    and the gate that exists to prevent exactly this had called it grounded.
    """
    written = _normalize(written)
    if not written:
        return True  # nothing name-based to check
    value = card.get("value") or {}
    # The REAL accessible name when the card has one; the human `logical_name`
    # only as a fallback. Playwright matches `written` as a substring of the
    # real name, so a written name LONGER than the real one (`known in
    # written`) matches nothing at run time and must not pass the gate.
    known = _normalize(value.get("accessible_name")) or _normalize(value.get("logical_name"))
    return bool(known) and written in known


#: Marker in `explore.discover`'s reason when the crawl itself failed (login,
#: navigation, browser), as opposed to the element being absent. Asking the
#: model to repair that buys an identical failure at full price.
_INFRA_REASON = "crawl could not reach it"


def _name_mismatch_messages(path: str, label: str, card: dict) -> tuple:
    value = card.get("value") or {}
    real_name = value.get("accessible_name") or value.get("logical_name") or ""
    real_locator = value.get("locator") or ""
    error = (f"{path}: {label!r} resolves to a known element, but that element's "
             f"real accessible name is {real_name!r}. Playwright matches this "
             f"locator's name as a substring at run time, so it will find "
             f"nothing.")
    repair = (f"In {path}, {label!r} will not match anything at run time. The "
              f"element it is meant to reach is named {real_name!r}. Use this "
              f"exact, verified expression instead: "
              f"{real_locator or 'see the ledger card for ' + real_name!r}. "
              f"Do not invent a different name for it.")
    return error, repair


def _discover_once(ledger, label: str, screen: str) -> tuple:
    """`explore.discover` with a per-run negative cache.

    Returns (result, failed_crawl). An element that could not be found on a
    screen is not re-crawled for every later locator or repair attempt that
    names it again; the cache lives on the ledger object, which is per run.
    """
    key = ((screen or "").strip().lower(), (label or "").strip().lower())
    cache = getattr(ledger, "_crawl_misses", None)
    if cache is None:
        cache = {}
        try:
            ledger._crawl_misses = cache
        except AttributeError:
            pass
    if key in cache:
        return cache[key], False
    found = explore.discover(ledger, label, screen)
    if found.get("found"):
        return found, False
    cache[key] = found
    return found, True


def _locator_label(locator: dict) -> str:
    return (locator.get("name") or locator.get("selector")
            or locator.get("raw") or "<unnamed>")


def _resolve_in_screens(ledger, label: str, screens: list, *,
                        groundable_only: bool = True):
    """Resolves an element name against EVERY screen the flow visits.

    An end-to-end spec walks a dozen screens, but this gate was handed one —
    the first screen of the case's first element — and resolved every locator
    in the file against it. `resolve_element` falls back to a global fuzzy
    search only when the scoped pool misses entirely, so a name that fuzzily
    hits SOMETHING on the wrong screen never got that far.

    Observed on a real VENDOR run: the spec's `Search, button` was
    checked against 'Dashboard', fuzzily matched that screen's 'Click here to
    Search' — a different control — and the gate reported "its real
    accessible name is 'Click here to Search'". The card it actually meant,
    on 'Manage Identity Search', is named exactly 'Search, button' and was
    never consulted. A false failure, and one that recurs every run, since
    the plan lists the same screens each time.

    Order matters, cheapest and most certain first:
      1. an EXACT keyed hit on any visited screen — no ambiguity to resolve;
      2. a fuzzy hit on a visited screen whose name the code could actually
         match (`_name_would_match`), screen order preserved;
      3. the unscoped fuzzy search, unchanged, so a locator for a screen the
         plan never named still resolves as it always did.

    Step 2's filter is not the gate going soft: a card that fails
    `_name_would_match` is by definition not the element this locator
    reaches, so preferring it over a later exact match only misnames the
    failure. Whatever step 3 returns is still handed to the caller's own
    `_name_would_match` check, which fails it there if it genuinely cannot
    match.
    """
    pool = [s for s in (screens or []) if s]
    if not pool:
        return ledger.resolve_element(label, "", groundable_only=groundable_only)

    for screen in pool:
        card = ledger.find_element(screen, label)
        if card is not None and (not groundable_only or trust.can_ground_locator(card)):
            return card

    for screen in pool:
        card = ledger.resolve_element(label, screen, groundable_only=groundable_only)
        if card is not None and _name_would_match(label, card):
            return card

    return ledger.resolve_element(label, "", groundable_only=groundable_only)


def check_file(generated_file: dict, ledger, *, screen: str = "",
               screens: list = None, snapshot_elements: list = None,
               allow_crawl: bool = True) -> VerifyResult:
    """Verifies every locator in one generated file.

    `screens` is every screen the file's flow visits; `screen` is the
    single-screen form kept for callers that have only one. An end-to-end
    spec needs the former — see `_resolve_in_screens` for what checking a
    twelve-screen journey against one screen did.

    `allow_crawl=False` turns off tier 2 — used when the app is known to be
    unreachable (no APP_BASE_URL, --skip-execution), so the gate degrades to
    "ledger only" instead of paying a failed browser launch per locator.
    """
    visited = list(screens) if screens else ([screen] if screen else [])
    path = generated_file.get("path", "<unknown>")
    source = generated_file.get("content", "") or ""
    locators = locator_verifier.extract_locators(source)

    if not locators:
        return VerifyResult.skipped(
            "locator_check", f"no locators could be extracted from {path}",
            meta={"file": path})

    errors, warnings, repairs = [], [], []
    grounded = crawled = crawl_failed = 0

    # Snapshot cross-check, when one is available. This is the ONLY tier that
    # can detect ambiguity, because ambiguity is a property of one screen's
    # DOM rather than of the ledger — see the module docstring.
    ambiguous_by_name = {}
    if snapshot_elements:
        snap = locator_verifier.verify_against_snapshot(locators, snapshot_elements)
        for entry in snap.get("unverified", []):
            matches = entry.get("ambiguous_matches")
            if matches:
                ambiguous_by_name[_locator_label(entry)] = matches

    for locator in locators:
        label = _locator_label(locator)
        if locator.get("kind") in _UNGROUNDABLE_KINDS:
            warnings.append(
                f"{path}: {label!r} is a raw CSS selector — not ledger-checkable. "
                f"Prefer a role or test-id locator so it can be verified.")
            continue

        # A NAME BUILT AT RUN TIME IS NOT A CLAIM ABOUT THE APP. Asserting on
        # data the test itself just minted — `getByText(surname)`, where
        # `surname` is this run's unique value — is the correct way to prove
        # an registration actually landed, and no crawl can ever have recorded
        # that string. The extractor leaves `name` empty for these, so
        # `_locator_label` fell back to the WHOLE expression and looked
        # "getByText(surname, { exact: false })" up in the ledger, which
        # failed the gate and bought a full extra generation round — on every
        # run, since the next attempt writes the same necessary assertion.
        # Un-checkable, like a raw CSS chain, not wrong.
        if locator.get("dynamic_name"):
            warnings.append(
                f"{path}: {label!r} names a value computed at run time — not "
                f"ledger-checkable, since nothing crawled can contain it.")
            continue

        # NO NAME TO GROUND. `getByRole('row')` constrains the role and
        # nothing else. The ledger is keyed by element NAME, so there is
        # simply no question to ask it — but `_locator_label` falls back to
        # the raw expression, and "getByRole('row')" was looked up as though
        # it were an accessible name, missed, and failed the gate.
        # `verify_against_snapshot` already takes this position ("a locator
        # with no name to match on can't be corroborated by name"); this is
        # the same rule on the ledger side, which disagreed with it.
        if locator.get("kind") in _NAME_MATCHED_KINDS and not locator.get("name"):
            warnings.append(
                f"{path}: {label!r} constrains only the role — no name to check "
                f"against the ledger. Scope it to a container or add a name so it "
                f"can be verified.")
            continue

        if label in ambiguous_by_name:
            names = ambiguous_by_name[label]
            errors.append(
                f"{path}: {label!r} matches {len(names)} elements ({', '.join(names[:4])}). "
                f"Playwright strict mode will refuse to act on it at run time.")
            repairs.append(
                f"The locator {label!r} in {path} is AMBIGUOUS — it matches "
                f"{len(names)} different elements: {names[:4]}. Narrow it so it "
                f"resolves to exactly one: add a container scope (a parent "
                f"getByRole/locator), use an exact name match instead of a "
                f"substring/regex, or use a test id if one exists.")
            continue

        card = _resolve_in_screens(ledger, label, visited, groundable_only=True)
        if card is not None:
            # Resolving a card says the ledger KNOWS this element. It does not
            # say the code as written can reach it — see `_name_would_match`.
            if (locator.get("kind") in _NAME_MATCHED_KINDS
                    and not _name_would_match(locator.get("name") or label, card)):
                error, repair = _name_mismatch_messages(path, label, card)
                errors.append(error)
                repairs.append(repair)
                continue
            grounded += 1
            continue

        if allow_crawl:
            found, failed_crawl = _discover_once(ledger, label, screen)
            crawl_failed += int(failed_crawl)
            if found.get("found"):
                # Fuzzy resolution can hand back a card whose real name the
                # written locator cannot match: same check as the ledger hit.
                crawl_card = found.get("card")
                if (crawl_card is not None and locator.get("kind") in _NAME_MATCHED_KINDS
                        and not _name_would_match(locator.get("name") or label, crawl_card)):
                    error, repair = _name_mismatch_messages(path, label, crawl_card)
                    errors.append(error)
                    repairs.append(repair)
                    continue
                crawled += 1
                continue
            crawl_reason = found.get("reason", "")
            near = found.get("card")
        else:
            crawl_reason = "crawling disabled for this run"
            near = _resolve_in_screens(ledger, label, visited, groundable_only=False)

        # Tier 3 — a near miss is the most repairable failure there is. The
        # ledger knows an element whose real name is close but not equal
        # (the classic 'Save' against a real 'Save changes'), so the repair
        # can carry the true name and needs no further inspection.
        if near is not None:
            real = (near.get("value") or {}).get("accessible_name") or \
                   (near.get("value") or {}).get("logical_name") or ""
            real_locator = (near.get("value") or {}).get("locator") or ""
            errors.append(
                f"{path}: {label!r} is not confirmed on the app. Closest known "
                f"element is {real!r} ({trust.decay_note(near)}).")
            if real_locator and trust.can_ground_locator(near):
                repairs.append(
                    f"In {path}, replace the locator for {label!r} with this exact, "
                    f"verified expression: {real_locator}")
            else:
                repairs.append(
                    f"In {path}, the locator {label!r} does not exist on the app. "
                    f"The closest real element is named {real!r}. Use that name "
                    f"exactly, or remove the step if it does not apply.")
            continue

        errors.append(
            f"{path}: {label!r} is not in the ledger and {crawl_reason}.")
        if _INFRA_REASON in (crawl_reason or ""):
            continue  # infrastructure failure: no model repair can fix it
        repairs.append(
            f"In {path}, the locator {label!r} could not be confirmed against the "
            f"running application ({crawl_reason}). Do NOT guess a replacement. "
            f"If this element is essential to the test, decline the case with "
            f"decline_reason naming this element and the screen it should be on; "
            f"if it is incidental, remove the step that uses it.")

    meta = {"file": path, "total": len(locators), "grounded": grounded,
            "crawled": crawled, "crawl_failed": crawl_failed, "failed": len(errors)}

    if errors:
        return VerifyResult.failed("locator_check", errors, repairs=repairs,
                                   warnings=warnings, meta=meta)
    return VerifyResult.passed("locator_check", warnings=warnings, meta=meta)


def _check_card_collisions(case_id: str, elements: list) -> tuple:
    """Catches one card being handed to several DIFFERENT controls in a plan.

    THE BUG THIS EXISTS FOR. A real plan resolved "Submit button to create and
    submit the registration", "Registration successful modal" and "Continue (after
    registration success)" — three distinct controls on two different screens —
    onto the single ledger card for "Register", and the Coding Agent duly emitted
    `getByText('Register')` for all three. Every individual element passed its own
    resolution check, because nothing compared the resolutions to each other.

    WHY SCREEN COUNT IS THE TEST rather than "any duplicate". One DOM element
    legitimately serves two names on ONE screen — this application relabels the
    same `#startCapture` button to "Recapture" after a non-compliant image, and
    a plan naming both is correct. But a single element cannot exist on two
    different screens at once, so a card claimed by controls declared on 2+
    screens means at least one of those resolutions is wrong. That split
    cleanly separates the real defect above from the legitimate relabelling.
    """
    by_card = {}
    for element in elements:
        card_id = element.get("card_id")
        if not card_id:
            continue
        name = (element.get("logical_name") or "").strip()
        screen = (element.get("screen") or "").strip()
        entry = by_card.setdefault(card_id, {"names": [], "screens": set(),
                                             "locator": element.get("locator", "")})
        if name not in entry["names"]:
            entry["names"].append(name)
        if screen:
            entry["screens"].add(screen)

    errors, repairs, warnings = [], [], []
    for card_id, entry in by_card.items():
        names = entry["names"]
        if len(names) < 2:
            continue
        listed = ", ".join(repr(n) for n in names)
        if len(entry["screens"]) > 1:
            screens = ", ".join(sorted(repr(s) for s in entry["screens"]))
            errors.append(
                f"{case_id}: {len(names)} differently-named controls ({listed}) all "
                f"resolved to the SAME element {card_id!r}, across {len(entry['screens'])} "
                f"screens ({screens}). One element cannot be on two screens — at least "
                f"one of these resolutions is wrong.")
            repairs.append(
                f"In the plan for {case_id}, these controls all resolved to the one "
                f"element {card_id!r} (locator {entry['locator']!r}): {listed}. They are "
                f"declared on different screens, so they are not the same control. "
                f"Re-name each one the way its OWN screen labels it — do not reuse a "
                f"neighbouring control's name. If a control genuinely is not in the "
                f"ledger yet, name it accurately anyway and let it come back unresolved "
                f"so it can be crawled or demonstrated; never borrow a name that "
                f"resolves to something else.")
        else:
            warnings.append(
                f"{case_id}: {listed} all resolve to the same element {card_id!r} on one "
                f"screen. Legitimate if the control is relabelled in place (this app "
                f"relabels #startCapture to 'Recapture'); otherwise re-name them.")
    return errors, repairs, warnings


def _planned_elements_for(generated_file: dict, plans: list) -> list:
    """The grounded elements belonging to the cases this file implements."""
    served = {c for c in (generated_file.get("test_case_ids") or []) if c}
    return [e for plan in plans or []
            if plan.get("test_case_id") in served
            for e in (plan.get("elements") or [])
            if e.get("locator")]


#: A written locator's name must cover at least this much of a planned
#: element's name before a partial match counts as "an attempt to reach that
#: element". The defect this check exists to catch is a TRUNCATION of the
#: real name ("Registration. Click to access Registration" for "Registration. Click
#: to access Registration module" — 0.84), so the bar sits well below that and
#: well above an incidental word or two.
_MIN_NAME_COVERAGE = 0.6


def _attributed_element(label: str, planned: list):
    """The one planned element a written locator's name is an attempt at, or
    None.

    THE ACCUSATION MUST NAME ONE ELEMENT. This was a plain `label in name`
    substring test, which is not a similarity measure at all — it fires on
    any element whose name happens to contain the label anywhere. Observed on
    a real run, every one of these a false positive that cost a full extra
    generation round:

      - `getByRole('link', { name: '1' })`, a paginator, charged against BOTH
        'CheckOIMS_1' and 'addressInfoLine1';
      - `getByText('Register', { exact: true })` — which is itself a planned
        element, grounded to exactly that expression, and correctly used —
        charged against 'Registration. Click to access Registration module' and
        'To continue with the registration';
      - `getByText('MATCH FOUND')` charged against 'NO MATCH FOUND', an
        element with the opposite meaning.

    Two rules replace the substring test, both derived from what the real
    defect looks like rather than from string containment:

      1. An EXACT name match settles it outright. If the label is some
         element's whole name, that is the element it addresses and no other
         — which alone resolves the 'Register' family above.
      2. Otherwise the label must be a WORD-ALIGNED PREFIX of the name,
         covering `_MIN_NAME_COVERAGE` of it. A truncated name is the shape
         the real defect takes ('...access Registration' for '...access
         Registration module'). A suffix is not: 'MATCH FOUND' inside 'NO MATCH
         FOUND' and '1' inside 'CheckOIMS_1' are different elements that
         happen to end alike, and a leading word can invert the meaning
         entirely.

    Ties go to the highest coverage, so a label that truncates two similar
    names is charged against the closer one only.
    """
    label_n = _normalize(label)
    if not label_n:
        return None

    best, best_coverage = None, 0.0
    for element in planned:
        name_n = _normalize(element.get("logical_name") or "")
        if not name_n:
            continue
        if label_n == name_n:
            return element
        if not name_n.startswith(label_n):
            continue
        # Word-aligned: the prefix must end where a word ends, or 'Register'
        # would count as a truncation of 'Registration'.
        if not name_n[len(label_n):].startswith(" "):
            continue
        coverage = len(label_n) / len(name_n)
        if coverage >= _MIN_NAME_COVERAGE and coverage > best_coverage:
            best, best_coverage = element, coverage
    return best


def check_against_plan(generated_file: dict, plans: list) -> VerifyResult:
    """Does the code use the locators the Planner actually grounded?

    THE MISSING LINK. `gate_code` verifies a generated locator against the
    ledger and against a DOM snapshot, but was never handed the PLAN — so
    nothing ever asked the one question the whole grounding chain exists to
    answer: did you use the selector we resolved for this element?

    Observed end to end. The ledger held the Registration tile as a crawled CSS
    selector, the Planner resolved it and put it in `elements[].locator`, and
    the Coding Agent wrote `getByRole('button', { name: <logical_name> })` —
    composing a selector out of the element's NAME while the verified one sat
    in the next field. Every run failed on that first click, and no gate
    objected.

    Deliberately narrow, so it accuses only when it is sure:
      - only elements the Planner actually grounded (a locator present),
      - only files serving those cases,
      - and only when the file spells out a locator of its own. A spec that
        delegates to a page object contains no locator to contradict, which
        is the convention this project wants anyway.
    """
    planned = _planned_elements_for(generated_file, plans)
    if not planned:
        return VerifyResult.skipped(
            "planned_locators", "no grounded elements for this file's cases",
            meta={"file": generated_file.get("path", "<unknown>")})

    source = generated_file.get("content", "") or ""
    written = locator_verifier.extract_locators(source)
    if not written:
        return VerifyResult.passed("planned_locators")

    path = generated_file.get("path", "<unknown>")
    approved = {_normalize(e["locator"]) for e in planned}
    errors, repairs = [], []

    # Driven from the WRITTEN locators, not from the planned elements. The
    # loop ran the other way and broke out of the inner scan after its first
    # hit, so one stray label could be charged against several elements while
    # a genuinely wrong locator later in the file went unexamined.
    accused = set()
    for locator in written:
        label = locator.get("name") or ""
        if not label:
            continue
        raw = locator.get("raw", "") or label
        if _normalize(raw) in approved:
            continue
        element = _attributed_element(label, planned)
        if element is None:
            continue
        name = element.get("logical_name") or ""
        if _normalize(element["locator"]) in _normalize(source):
            continue  # the grounded locator IS used elsewhere in the file
        if (raw, name) in accused:
            continue
        accused.add((raw, name))
        errors.append(
            f"{path}: {raw!r} was composed for element "
            f"{name!r}, but the Planner grounded that element to a verified "
            f"locator which the file does not use.")
        repairs.append(
            f"Replace {raw!r} with the grounded locator "
            f"for {name!r}: {element['locator']} — it was resolved against the "
            f"ledger and confirmed on the running application. Never build a "
            f"selector out of an element's name when its locator is supplied.")

    if errors:
        return VerifyResult.failed("planned_locators", errors, repairs=repairs)
    return VerifyResult.passed("planned_locators",
                               meta={"file": path, "planned": len(planned)})


def apply_resolved_card(element: dict, card: dict) -> None:
    """Writes a resolved ledger card back onto the plan element it answers.

    `logical_name` is REPLACED with the card's own name, not just annotated
    with its locator. Resolution is fuzzy by design — it has to be, or a
    plausible paraphrase would never match — but the model's paraphrase then
    travelled downstream as if it were the element's real name, and the
    Coding Agent reads that field.

    Observed end to end: the card is "Registration. Click to access Registration
    module", the plan said "Registration. Click to access Registration", the
    resolver matched them and attached the correct locator, and the Coding
    Agent then wrote `getByRole('button', { name: <logical_name> })` from the
    truncated version. Right locator, wrong name, and the name is what got
    used. A fuzzy match means the CARD is the truth; carrying the near-miss
    forward is what produced a third spelling of one button.
    """
    element["card_id"] = card.get("id", "")
    value = card.get("value") or {}
    element["locator"] = value.get("locator", "")
    real_name = value.get("logical_name")
    if real_name:
        element["logical_name"] = real_name


def check_plan(plan: dict, ledger, *, allow_crawl: bool = True) -> VerifyResult:
    """Verifies a plan's elements BEFORE any code is written.

    This is the cheapest place in the whole pipeline to catch a missing
    element: nothing has been generated yet, so resolving it costs one crawl
    rather than a generation, a compile, an execution and a heal cycle.

    Resolved elements are written back into the plan's `locator` field, so
    the Coding Agent receives verified expressions as DATA and never has to
    author one. That is the mechanism by which "the model may not invent a
    locator" is enforced structurally rather than by instruction.
    """
    case_id = plan.get("test_case_id", "<unknown>")
    elements = plan.get("elements") or []
    if not elements:
        return VerifyResult.skipped(
            "locator_check", f"plan for {case_id} lists no elements",
            meta={"test_case_id": case_id})

    errors, repairs, warnings = [], [], []
    grounded = 0
    crawled = 0

    for element in elements:
        name = element.get("logical_name", "")
        screen = element.get("screen", "")
        if not name:
            errors.append(f"{case_id}: an element entry has no logical_name.")
            continue

        card = ledger.resolve_element(name, screen, groundable_only=True)
        if card is None and allow_crawl:
            found, failed_crawl = _discover_once(ledger, name, screen)
            crawl_failed += int(failed_crawl)
            card = found.get("card") if found.get("found") else None
            # `crawled` counts only crawls that RESOLVED the element (it feeds
            # the verification rate); failed crawls are charged to the crawl
            # allowance separately, as `crawl_failed`.
            if card is not None:
                crawled += 1
            else:
                reason = found.get("reason", "not found")
                errors.append(f"{case_id}: element {name!r} on {screen!r} — {reason}")
                # An infrastructure failure cannot be repaired by the model.
                if _INFRA_REASON not in (reason or ""):
                    repairs.append(
                        f"Element {name!r} on screen {screen!r} could not be confirmed "
                        f"({reason}). Either name it the way the UI actually labels it, "
                        f"or mark this case declined with that element named.")
                continue
        elif card is None:
            errors.append(
                f"{case_id}: element {name!r} on {screen!r} — "
                f"{ledger.unresolved_reason(name, screen)}")
            continue

        apply_resolved_card(element, card)
        grounded += 1

    collision_errors, collision_repairs, collision_warnings = _check_card_collisions(
        case_id, elements)
    errors.extend(collision_errors)
    repairs.extend(collision_repairs)
    warnings.extend(collision_warnings)

    # Same counter vocabulary as `check_file` above — total/grounded/crawled/
    # failed. It used to report `elements`/`resolved`, which no consumer reads:
    # `RunMetrics.absorb_verify` looks for "total", so every element the
    # PLANNER resolved was missing from the run's locator verification rate,
    # and only the coding stage's locators were ever counted.
    meta = {"test_case_id": case_id, "total": len(elements),
            "grounded": grounded - crawled, "crawled": crawled,
            "crawl_failed": crawl_failed, "failed": len(errors)}
    if errors:
        return VerifyResult.failed("locator_check", errors, repairs=repairs,
                                   warnings=warnings, meta=meta)
    return VerifyResult.passed("locator_check", warnings=warnings, meta=meta)
