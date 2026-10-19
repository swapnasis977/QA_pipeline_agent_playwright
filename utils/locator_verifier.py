"""
Checks that the locators in a GENERATED test file actually exist on the
screen the test targets — before the file is accepted, not at Executor time.

Why this is the highest-value gate in the pipeline: the model writes
competent Playwright structure, and the thing it cannot know is the real
selector. `coding_agent._check_syntax` proves a file COMPILES, which says
nothing about whether `getByRole('button', { name: 'Save' })` resolves to
anything. So the dominant first-run failure was a file that compiled
cleanly, ran for 30 seconds, and timed out on a plausible-but-absent
locator — discovered only after a full suite run, then handed to the
Reviewer and Self-Healer to re-derive from a stack trace.

This turns that into a deterministic, zero-LLM check at generation time,
using the DOM snapshot the pipeline ALREADY has for the case (the Planner's
inspection, or the Coding Agent's own re-inspection).

Checks two independent failure shapes, both against the SAME snapshot:
  - UNVERIFIED: no element in the snapshot corroborates the locator at all
    (the classic hallucinated-name case).
  - AMBIGUOUS: MORE THAN ONE element corroborates it. A `getByRole`/getByText
    locator that matches more than one node is not a false positive to a
    naive "does at least one element match" check — it visibly "works" until
    Playwright's strict mode refuses to click at RUN time ("resolved to N
    elements"). Confirmed against a real app: `getByRole('button', { name:
    /Identification/i })` matched both a sidebar nav button ("Identification
    SideMenu.") and a dashboard tile ("Identification. Click to access
    Identification module") — the substring pattern that made the case's own
    domain wording match at all is exactly what let it match a SECOND,
    unrelated element too. This was the single root cause behind every one of
    three real Executor failures in one run, and the original version of
    this module reported all three locators "verified" because it only
    checked for at least one match.

Two deliberate limits, both of which make this a WARNING signal rather than
grounds for rejection on its own:

  - A snapshot holds the first ~60 visible interactive elements of ONE
    screen (see dom_inspector._snapshot's max_elements). A locator for an
    element further down, behind a tab, or on a later screen in a multi-step
    flow is legitimately absent from it.
  - A `locator('.card > div:nth-child(2)')` style CSS chain can be perfectly
    valid without any snapshot element carrying that exact selector.

So "unverified" here means "this snapshot does not corroborate it", NOT
"this locator is wrong". What makes it useful anyway is the inverse: when a
locator's role+name pair is CLOSE to a real element's but not equal (the
classic "Save" vs "Save changes" hallucination), the near-miss is strong
evidence, and the suggestion carries the real name so a repair pass can fix
it without another round of inspection.
"""

import difflib
import re

#: Playwright's getBy* locator factories, with the argument list captured.
#: No nested parens in the argument (an object-literal option uses braces),
#: which covers real generated code; anything more exotic simply isn't
#: extracted, and an un-extracted locator is never reported as unverified.
_GET_BY_RE = re.compile(
    r"getBy(Role|Label|Placeholder|Text|TestId|Title|AltText)\(([^()]*)\)")

#: `page.locator('...')` / `.locator("...")` with a plain string selector.
_LOCATOR_RE = re.compile(r"""\.locator\(\s*['"]([^'"]+)['"]""")

#: First string argument of a getBy* call, and the `name:` option (string or
#: regex form) of a getByRole call.
_FIRST_STRING_RE = re.compile(r"""\s*['"]([^'"]*)['"]""")
_NAME_OPT_RE = re.compile(r"""name:\s*(?:['"]([^'"]*)['"]|/([^/]*)/[a-z]*)""")

#: A `name:` option is present, whatever its value's shape. Distinguishes
#: "this locator constrains the name, but not with a literal" from "this
#: locator has no name constraint at all" — two different things that both
#: leave `_NAME_OPT_RE` empty.
_NAME_OPT_PRESENT_RE = re.compile(r"""\bname:\s*\S""")

#: A role+name pair at or above this similarity to a real element's name is
#: reported as a NEAR MISS with the real name attached — the shape of a
#: hallucinated locator that is almost right ("Save" for "Save changes").
#: Below it, the locator is simply unverified with no suggestion.
_NEAR_MISS_RATIO = 0.6


def _norm(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def extract_locators(source: str) -> list:
    """Pulls every locator expression out of generated TypeScript.

    Returns [{"kind", "selector"|"role"+"name", "raw", "line",
    "dynamic_name"}]. Deduped on the raw expression, since one locator used
    five times is one thing to verify, not five.

    `dynamic_name` marks a getBy* whose name argument is a RUNTIME VALUE
    rather than a string literal — `getByText(surname)`, `getByRole('row',
    { name: new RegExp(surname, 'i') })`. The name is empty for those either
    way, so callers previously fell back to the whole raw expression as the
    element's "name" and looked it up in the ledger; the flag is what lets
    them tell an unparsed name from an absent one. A value minted during the
    run cannot appear in anything a crawl recorded, so such a locator is
    un-checkable, not wrong.
    """
    found, seen = [], set()

    for match in _GET_BY_RE.finditer(source):
        kind, args = match.group(1), match.group(2)
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        line = source[:match.start()].count("\n") + 1

        if kind == "Role":
            role_match = _FIRST_STRING_RE.match(args)
            name_match = _NAME_OPT_RE.search(args)
            name = None
            if name_match:
                name = name_match.group(1) if name_match.group(1) is not None else name_match.group(2)
            found.append({"kind": "role", "role": role_match.group(1) if role_match else "",
                          "name": name or "", "raw": raw, "line": line,
                          # `name: /^Save$/i` is a pattern, not the text "^Save$".
                          "name_is_regex": bool(name_match and name_match.group(1) is None
                                                and name_match.group(2) is not None),
                          # A name built at run time (`name: surname`,
                          # `name: new RegExp(x)`) constrains the match but
                          # says nothing a crawl could ever have recorded.
                          "dynamic_name": bool(name_match is None
                                               and _NAME_OPT_PRESENT_RE.search(args))})
        else:
            value_match = _FIRST_STRING_RE.match(args)
            found.append({"kind": kind.lower(),
                          "name": value_match.group(1) if value_match else "",
                          "raw": raw, "line": line,
                          "dynamic_name": bool(value_match is None and args.strip())})

    for match in _LOCATOR_RE.finditer(source):
        raw = match.group(0)
        if raw in seen:
            continue
        seen.add(raw)
        found.append({"kind": "css", "selector": match.group(1), "raw": raw,
                      "line": source[:match.start()].count("\n") + 1})

    return found


def _element_names(elements: list) -> list:
    return [_norm(el.get("name")) for el in elements if el.get("name")]


def _matches_css(selector: str, elements: list) -> bool:
    """Only the two selector shapes a snapshot can actually corroborate:
    an id (`#foo`) and a data-testid attribute. Any other CSS chain is
    treated as un-checkable rather than as a failure — see the module
    docstring's second limit."""
    id_match = re.fullmatch(r"#([A-Za-z0-9_\-:.]+)", selector.strip())
    if id_match:
        return any(el.get("id") == id_match.group(1) for el in elements)

    testid_match = re.fullmatch(
        r"""\[data-test-?id=['"]?([^'"\]]+)['"]?\]""", selector.strip())
    if testid_match:
        return any(el.get("testId") == testid_match.group(1) for el in elements)

    return True  # not corroborable either way — do not report as unverified


#: Playwright's REAL rule — not "quoted vs regex" — is what decides exact vs
#: containment matching: a `name` (getByRole) or a getByText argument is
#: case-insensitive SUBSTRING matching by default, whether it's written as a
#: quoted string or a regex; only an explicit `exact: true` switches it to
#: exact equality. (First guess here assumed regex=containment,
#: quoted=exact — that is not what Playwright actually does, and would have
#: made a plain quoted name look falsely "safe" from the exact ambiguity this
#: module exists to catch.)
_EXACT_OPTION_RE = re.compile(r"""exact:\s*true""")


def _is_exact_name(locator: dict) -> bool:
    return bool(_EXACT_OPTION_RE.search(locator.get("raw", "")))


def verify_against_snapshot(locators: list, elements: list) -> dict:
    """Checks each extracted locator against a DOM snapshot's element list.

    Returns {"verified": [...], "unverified": [...]} where an unverified
    entry may carry:
      - "suggestion" — the real accessible name of the closest element,
        for a locator that matched NOTHING. Lets a repair pass fix the
        locator without another live inspection.
      - "ambiguous_matches" — every element name a locator matched, for a
        locator that matched MORE THAN ONE. This is reported even though
        the locator technically "worked" against the snapshot, because
        Playwright's strict mode refuses to act on more than one match at
        RUN time — see this module's docstring for the real failure this
        traces to.
    """
    # Matches are COUNTED to decide "ambiguous", so one element arriving twice
    # (two cases sharing a screen, or a pooled snapshot) made every unique
    # locator look like it matched two.
    seen_elements, unique_elements = set(), []
    for el in elements or []:
        key = (el.get("role"), el.get("name"), el.get("locator"), el.get("id"))
        if key not in seen_elements:
            seen_elements.add(key)
            unique_elements.append(el)
    elements = unique_elements

    if not elements:
        # No snapshot means nothing is corroborated OR contradicted. Claiming
        # every locator is unverified here would flood the report with noise
        # on any case the pipeline never inspected.
        return {"verified": [], "unverified": []}

    names = _element_names(elements)
    verified, unverified = [], []

    for locator in locators:
        kind = locator.get("kind")

        if kind == "css":
            (verified if _matches_css(locator["selector"], elements) else unverified).append(locator)
            continue

        if kind == "testid":
            wanted = locator.get("name", "")
            ok = any(el.get("testId") == wanted for el in elements)
            (verified if ok else unverified).append(locator)
            continue

        wanted = _norm(locator.get("name"))
        name_pattern = None
        if locator.get("name_is_regex") and locator.get("name"):
            try:
                name_pattern = re.compile(locator["name"], re.I)
            except re.error:
                verified.append(locator)   # cannot be judged, so not reported as wrong
                continue
        if not wanted:
            # A locator with no name to match on (e.g. getByRole('button')
            # alone) can't be corroborated by name; the role alone is too
            # weak to call it verified or not.
            verified.append(locator)
            continue

        if kind == "role":
            role = _norm(locator.get("role"))
            # Same ROLE only. Falling back to every element let a button named
            # "Save" corroborate `getByRole('link', { name: 'Save' })`, which
            # Playwright would never resolve.
            candidates = [el for el in elements if _norm(el.get("role")) == role]
        else:
            candidates = elements

        # Playwright's OWN matching rule, checked exactly as it would apply
        # at runtime: substring + case-insensitive UNLESS `exact: true` is
        # present. This is what makes a 2-match result here mean the same
        # thing as a 2-match strict-mode violation at Executor time — it was
        # exactly this containment rule (applied with no exceptions in the
        # PREVIOUS version) that let `getByRole('button', { name:
        # /Identification/i })` "verify" against both "Identification
        # SideMenu." and "Identification. Click to access Identification
        # module": the substring test that makes a case's own wording match
        # the right element at all is the same test that lets it match a
        # second, unrelated one.
        exact = _is_exact_name(locator)
        # Containment is ONE-WAY: the wanted text must be inside the element's
        # name. The reverse (`name in wanted`) is not Playwright's rule, and made
        # a real "Save" corroborate an invented "Save changes".
        if name_pattern is not None:
            matches = [el for el in candidates
                       if el.get("name") and name_pattern.search(str(el["name"]))]
        else:
            matches = [
                el for el in candidates
                if el.get("name") and (
                    _norm(el["name"]) == wanted if exact else wanted in _norm(el["name"]))
            ]

        if len(matches) == 1:
            verified.append(locator)
            continue

        if len(matches) > 1:
            entry = dict(locator)
            entry["ambiguous_matches"] = sorted({el["name"] for el in matches})
            unverified.append(entry)
            continue

        entry = dict(locator)
        candidate_names = [_norm(el.get("name")) for el in candidates if el.get("name")]
        close = difflib.get_close_matches(wanted, candidate_names or names, n=1,
                                           cutoff=_NEAR_MISS_RATIO)
        if close:
            # The real name, spelled the way the app spells it.
            for el in (candidates or elements):
                if _norm(el.get("name")) == close[0]:
                    entry["suggestion"] = el.get("name")
                    entry["suggested_locator"] = el.get("locator")
                    break
        unverified.append(entry)

    return {"verified": verified, "unverified": unverified}


def verify_file(source: str, elements: list) -> dict:
    """extract_locators + verify_against_snapshot in one call, plus a
    human-readable summary line."""
    locators = extract_locators(source)
    result = verify_against_snapshot(locators, elements)
    result["total"] = len(locators)
    return result


def describe_unverified(unverified: list) -> str:
    """Renders unverified locators as repair instructions for the model —
    each with the real element name where one was found nearby, or every
    name an AMBIGUOUS locator matched (see ambiguous_matches)."""
    lines = []
    for entry in unverified:
        target = entry.get("raw", "")
        hint = ""
        if entry.get("ambiguous_matches"):
            names = "; ".join(repr(n) for n in entry["ambiguous_matches"])
            hint = (f"  -> AMBIGUOUS: matches {len(entry['ambiguous_matches'])} elements on "
                    f"this screen ({names}) — Playwright's strict mode will refuse to act on "
                    f"this at runtime. Narrow it to the ONE you mean (an exact, non-regex name; "
                    f"a testid/id; or a role scoped to a specific container)")
        elif entry.get("suggestion"):
            hint = (f"  -> the closest REAL element on this screen is named "
                    f"{entry['suggestion']!r}")
            if entry.get("suggested_locator"):
                hint += f" (verified locator: {entry['suggested_locator']})"
        lines.append(f"- line {entry.get('line', '?')}: {target}{hint}")
    return "\n".join(lines)


def summarize(result: dict) -> str:
    return (f"{len(result.get('verified', []))}/{result.get('total', 0)} locator(s) "
            f"corroborated by the live DOM snapshot")
