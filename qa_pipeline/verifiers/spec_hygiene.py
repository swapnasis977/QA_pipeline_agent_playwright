"""Defects in a generated spec that are about LOGIC, not locators.

`locator_check` answers "does this selector exist on the real app?" — and a
spec can pass that completely while still being unable to succeed. Three ways
it did, all from one real VENDOR-TC-001 generation that the locator gates
waved through:

  1. A dropped `{ exact: true }`. `getByText` matches by SUBSTRING unless told
     otherwise, and "NO MATCH FOUND" contains "MATCH FOUND", so a check meant
     to detect the failure outcome fired on the SUCCESS outcome and skipped
     the test. Every locator involved was real and in the ledger.
  2. Two const names bound to the identical locator, combined with `.or()` —
     a no-op that silently halved a retry loop's coverage.
  3. `if` / `.catch(() => false)` branching, which conventions.md forbids: a
     test that branches can pass without having done anything.

The first two are unambiguous and rejected. The third is reported as a
WARNING, because this project's own test cases say things like "if a stale
confirmation dialog appears, click Yes" — a blanket rejection would fight the
requirement rather than the defect.
"""

from __future__ import annotations

import re
from typing import Any

from .result import VerifyResult

#: `const <name> = <expression>;` — the binding form the Coding Agent uses for
#: every locator it names.
_CONST_BINDING = re.compile(r"\bconst\s+([A-Za-z_$][\w$]*)\s*=\s*(.+?);", re.S)

#: A `getByText('...')` / `getByRole(..., { name: '...' })` argument, so a
#: locator in the code can be compared with the one the Planner grounded.
_TEXT_ARG = re.compile(r"""getByText\(\s*(['"])(.+?)\1""")

#: Branching constructs conventions.md rules out of a test body.
_CONDITIONALS = (
    (re.compile(r"^\s*if\s*\(", re.M), "an `if` branch"),
    (re.compile(r"^\s*try\s*\{", re.M), "a `try`/`catch`"),
    (re.compile(r"\.catch\(\s*\(\)\s*=>", re.S), "a swallowed `.catch(() => ...)`"),
    (re.compile(r"\.count\(\)\s*[><=]", re.S), "a `.count()` comparison"),
)


def _texts_with_exact(locator: str) -> set:
    """The `getByText` arguments a locator pins with `{ exact: true }`."""
    if "exact" not in (locator or ""):
        return set()
    return {m.group(2) for m in _TEXT_ARG.finditer(locator)}


def _dropped_exact(source: str, plans: list[dict[str, Any]] | None) -> tuple:
    """`getByText` calls whose missing `exact: true` makes them ambiguous.

    Only flagged where the option actually MATTERS — where the text is a
    substring of, or contains, another text in play. That is the collision
    the rule exists for ("MATCH FOUND" inside "NO MATCH FOUND"); elsewhere a
    missing `exact` changes nothing, and flagging it would only teach people
    to ignore the gate.
    """
    grounded = [e.get("locator", "") for plan in plans or []
                for e in (plan.get("elements") or [])]

    # THE COLLISION IS THE EVIDENCE, not the plan's permission.
    #
    # This first required the text to be pinned with `exact: true` by a
    # grounded plan locator — and so missed the actual defect. The real spec
    # wrote `getByText('MATCH FOUND')` at a line where the plan grounded only
    # "NO MATCH FOUND"; the dangerous string was one the model introduced
    # itself, exactly where no plan could vouch for it. Both texts were right
    # there in the file, one containing the other, which is all the proof the
    # ambiguity needs.
    all_texts = {m.group(2) for m in _TEXT_ARG.finditer(source)}
    for locator in grounded:
        all_texts |= {m.group(2) for m in _TEXT_ARG.finditer(locator)}

    errors, repairs = [], []
    for match in _TEXT_ARG.finditer(source):
        text = match.group(2)
        tail = source[match.end():match.end() + 80]
        if "exact" in tail:
            continue  # the option is there
        collides = any(other != text and (text in other or other in text)
                       for other in all_texts)
        if not collides:
            continue
        errors.append(
            f"getByText({text!r}) without `{{ exact: true }}` "
            f"is ambiguous. getByText matches by SUBSTRING by default, and "
            f"another text in play contains this one — so this will match the "
            f"wrong outcome.")
        repairs.append(
            f"Write page.getByText({text!r}, {{ exact: true }}) — copy the "
            f"grounded locator verbatim rather than dropping its options.")
    return errors, repairs


def _duplicate_bindings(source: str) -> tuple:
    """Two differently-named consts holding byte-identical locators."""
    seen: dict[str, str] = {}
    errors: list[str] = []
    repairs: list[str] = []
    for match in _CONST_BINDING.finditer(source):
        name, expression = match.group(1), " ".join(match.group(2).split())
        if "page." not in expression and "getBy" not in expression:
            continue
        first = seen.get(expression)
        if first is None:
            seen[expression] = name
            continue
        if first == name:
            # The SAME name bound to the same locator again is a per-test
            # `const blurSlider = ...` in a second test(), not "two names for one
            # element". Reporting it told the model to fix something that was
            # correct and cost a whole-stage regeneration.
            continue
        errors.append(
            f"`{name}` is bound to exactly the same locator as `{first}` "
            f"({expression[:70]}). Two names for one element is a copy-paste "
            f"slip, and combining them (e.g. with `.or()`) is a no-op that "
            f"silently drops the case the second name was meant to cover.")
        repairs.append(
            f"Give `{name}` the locator it was actually meant to have, or "
            f"delete it and use `{first}` directly.")
    return errors, repairs


#: Evidence a spec CREATES something in the application: a unique value minted
#: for this run. Every data-creating spec in this project mints one.
_MINTS_UNIQUE_DATA = re.compile(r"\bDate\.now\(\)")

#: Teardown that still runs when a step fails. Playwright runs these hooks
#: after a failing test; steps in the body simply never execute.
_TEARDOWN_HOOK = re.compile(r"\btest\.(afterEach|afterAll)\s*\(")

#: `.fill(name)` / `.pressSequentially(name)` where the argument is a bare
#: identifier — a value the test typed and can therefore be asked about later.
_FILLED_VARIABLE = re.compile(
    r"\.(?:fill|pressSequentially)\(\s*([A-Za-z_$][\w$]*)\s*\)")


def _missing_teardown(source: str) -> tuple:
    """A spec that creates data but cleans up only in the test body.

    Observed on VENDOR-TC-001: it registers a real identity at step 9 and
    deletes it at steps 16-20 of the MAIN BODY, so any failure in between
    leaks the identity permanently. On this project that compounds — the R100
    biometric mock is single-shot, so a leaked registration makes the next run
    return MATCH FOUND and skip. One mid-test failure disables the test for
    every run afterwards.
    """
    if not _MINTS_UNIQUE_DATA.search(source) or _TEARDOWN_HOOK.search(source):
        return [], []
    return (
        ["creates data (a unique value is minted for this run) but has no "
         "`test.afterEach`/`test.afterAll` teardown, so anything it creates "
         "survives a mid-test failure."],
        ["Move the cleanup into `test.afterEach` (an API call is preferable to "
         "driving the UI). Steps in the test body do not run once an earlier "
         "step has failed, which is exactly when cleanup matters most."],
    )


def _unasserted_data(source: str) -> list:
    """Values the test typed and never read back.

    A WARNING, not an error. "Filled but never asserted" is usually a missing
    verification — VENDOR-TC-001 fills Surname, First Name, Middle Name
    and DOB, then checks only that a Submit button is visible, while its own
    case says to verify those values on the Review screen. But some filled
    values legitimately go unasserted (a deletion comment, a password), so
    this names them and leaves the judgement to a person.
    """
    filled = {m.group(1) for m in _FILLED_VARIABLE.finditer(source)}
    if not filled:
        return []
    asserted = set()
    for match in re.finditer(r"expect\(", source):
        # The assertion and its chained matcher, e.g.
        # `expect(page.getByText(surname)).toBeVisible()`.
        window = source[match.start():match.start() + 240]
        asserted |= {name for name in filled if re.search(rf"\b{re.escape(name)}\b", window)}
    missing = sorted(filled - asserted)
    if not missing:
        return []
    return [f"values typed but never asserted: {', '.join(missing)}. A test that "
            f"enters data and never reads it back cannot fail for the reason it "
            f"exists — check the case's Expected Results for each one."]


def _conditional_warnings(source: str) -> list:
    found = [label for pattern, label in _CONDITIONALS if pattern.search(source)]
    if not found:
        return []
    return [f"conditional test logic: {', '.join(found)}. conventions.md rules "
            f"these out — a test that branches can report success without "
            f"having exercised anything. Keep it only where the test case "
            f"itself describes an optional step."]


#: A hook declaration, with the offset so its nesting can be judged.
_HOOK_DECL = re.compile(r"\btest\.(afterEach|afterAll|beforeEach|beforeAll)\s*\(")

#: `test(` / `test.only(` / `test.skip(` — a TEST body, as opposed to a
#: `test.describe(` block, which is where hooks legally live.
_TEST_DECL = re.compile(r"\btest(?:\.(?:only|skip|fixme))?\s*\(")


def _call_span(source: str, open_paren: int) -> int:
    """Index just past the `)` that closes the call opening at `open_paren`.

    Depth-counts parentheses while SKIPPING string literals, template
    literals and comments — a Playwright test title routinely contains
    brackets ("... biometric capture (No Match Found)"), and a counter that
    cannot tell code from text closes the call in the middle of the title and
    reads the rest of the file as though it were outside.
    """
    i, depth, end = open_paren, 0, len(source)
    while i < end:
        char = source[i]
        if char in "'\"`":
            quote, i = char, i + 1
            while i < end:
                if source[i] == "\\":
                    i += 2
                    continue
                if source[i] == quote:
                    break
                i += 1
        elif char == "/" and i + 1 < end and source[i + 1] == "/":
            i = source.find("\n", i)
            if i == -1:
                return end
        elif char == "/" and i + 1 < end and source[i + 1] == "*":
            closed = source.find("*/", i + 2)
            i = end if closed == -1 else closed + 1
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return end


def _hook_inside_a_test(source: str) -> tuple:
    """A teardown hook declared INSIDE a `test()` body.

    Playwright registers hooks while COLLECTING a file; by the time a test
    body runs, collection is over, so it rejects the call outright:

        Error: Playwright Test did not expect test.afterEach() to be called
        here.

    The spec still type-checks — `test.afterEach` is a real function with the
    right signature wherever you call it — so `compile_check` passes it, and
    `_missing_teardown` is satisfied too, because that check only asks
    whether the file CONTAINS a teardown hook. A spec with its cleanup nested
    one level too deep therefore cleared every gate and failed on the very
    first line the Executor ran, having tested nothing.

    The whole question is nesting, so this scans the test call's own span
    rather than pattern-matching: `test.describe` is the legal home for a
    hook and `test`/`test.only`/`test.skip` are not, and the two are
    indistinguishable to a regex that cannot see depth.
    """
    for test_match in _TEST_DECL.finditer(source):
        open_paren = source.find("(", test_match.end() - 1)
        if open_paren == -1:
            continue
        body = source[open_paren:_call_span(source, open_paren)]
        hook = _HOOK_DECL.search(body)
        if hook:
            return (
                [f"`test.{hook.group(1)}()` is declared INSIDE a `test()` body. "
                 f"Playwright registers hooks at collection time and refuses "
                 f"this at run time (\"did not expect test.{hook.group(1)}() to "
                 f"be called here\"), so the test fails before its first step."],
                [f"Move `test.{hook.group(1)}(...)` OUT of the test body to the "
                 f"top level of the file, or into a `test.describe(...)` block "
                 f"that wraps the test — a describe callback must be sync, not "
                 f"`async`. State the hook and the test share (a flag saying "
                 f"whether cleanup already happened, the id of what was created) "
                 f"goes in a module-level `let` that the test assigns."],
            )
    return [], []


#: `test.describe(` whose callback is declared `async`. Playwright only
#: supports SYNC describe callbacks; hooks registered inside an async one are
#: rejected the same way as the nested case above.
_ASYNC_DESCRIBE = re.compile(r"\btest\.describe\s*\(\s*(?:[^,]*,\s*)?async\b")


def _async_describe(source: str) -> tuple:
    """A `test.describe` with an async callback.

    Playwright names this one itself in the same error message: "You are
    calling test.afterEach() from an async test.describe() block. Only sync
    ones are supported." Cheap to check, and it fails the run at collection.
    """
    if not _ASYNC_DESCRIBE.search(source) or not _HOOK_DECL.search(source):
        return [], []
    return (
        ["`test.describe(...)` takes an `async` callback and the file registers "
         "hooks inside it. Playwright supports only SYNC describe callbacks and "
         "rejects the hooks at collection time."],
        ["Make the `test.describe(...)` callback synchronous — drop the `async` "
         "keyword. Only the individual `test(...)` and hook callbacks are async."],
    )



# ---------------------------------------------------------------------------
# Rules the skill states and nothing enforced
#
# skills/coding_agent.skill.md forbids each of these, and a skill is a request.
# A generated suite shipped with all of them: `waitForTimeout(800)`,
# `expect(Number(await input.inputValue()))` (a snapshot that never retries),
# `click({ force: true })` on an element that was MEANT to be disabled, and a spec
# with no tag at all. They are errors now, so the repair loop fixes them.
# ---------------------------------------------------------------------------

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$|(?<=\s)//[^\n'\"`]*$")


def _strip_comments(source: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", source))


_HARD_WAIT = re.compile(r"\b(?:waitForTimeout|sleep)\s*\(")
_SNAPSHOT_ASSERT = re.compile(r"\bexpect\s*\(\s*(?:await\b|Number\s*\(|parseInt\s*\(|parseFloat\s*\()")
_FORCE = re.compile(r"\bforce\s*:\s*true\b")
_FORCE_REASON = re.compile(r"qa-allow-force:\s*\S")
_TEST_CALL = re.compile(r"(?m)^\s*test(?:\.\w+)?\s*\(\s*['\"`]")
_TAG = re.compile(r"\btag\s*:")


def _hard_waits(code: str) -> tuple:
    if not _HARD_WAIT.search(code):
        return [], []
    return (["uses a hard wait (`waitForTimeout`/`sleep`). A fixed sleep is too short "
             "on a slow day and wasted time on a fast one, and hides the real "
             "condition the step needs."],
            ["Replace the sleep with a web-first wait on the actual condition, e.g. "
             "`await expect(locator).toBeVisible()` / `.toHaveValue(...)`, or "
             "`await locator.waitFor()`."])


def _snapshot_assertions(code: str) -> tuple:
    if not _SNAPSHOT_ASSERT.search(code):
        return [], []
    return (["asserts on a value read first (`expect(await ...)`, `expect(Number(...))`). "
             "That reads the page ONCE and never retries, so it passes or fails on "
             "timing instead of on the behaviour."],
            ["Use the retrying matcher on the locator itself: `await "
             "expect(locator).toHaveValue('5')`, `.toHaveText(...)`, `.toHaveCount(n)`, "
             "or `expect.poll(() => ...)` for a computed value."])


def _unjustified_force(source: str) -> tuple:
    """`force: true` with no `// qa-allow-force: <reason>` on that line or the line above.

    `force` skips Playwright's actionability checks - the very checks that say a user
    could not do this. It is right in a few cases (a real <select> hidden behind a
    styled widget, a sticky header over its own button) and wrong when it clicks
    something the app deliberately disabled. The written reason makes the exception
    visible in review. Same convention the Self-Healer's guard uses.
    """
    lines = source.splitlines()
    bad = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("//") or not _FORCE.search(line):
            continue
        previous = next((lines[j] for j in range(i - 1, -1, -1) if lines[j].strip()), "")
        if not (_FORCE_REASON.search(line) or _FORCE_REASON.search(previous)):
            bad.append(i + 1)
    if not bad:
        return [], []
    return ([f"`force: true` at line(s) {', '.join(map(str, bad[:5]))} has no stated reason. "
             f"It bypasses the checks that say a real user could not do this."],
            ["Remove `force: true` and make the element actionable (scroll it into view, "
             "dismiss the overlay, wait for it to be enabled). Only if it is genuinely "
             "needed (a real <select> hidden behind a styled widget, a sticky header "
             "over its own button) keep it and put `// qa-allow-force: <reason>` on the "
             "line above. Never force a click on an element that is meant to be disabled "
             "- assert it is disabled instead."])


def _untagged(code: str) -> tuple:
    if not _TEST_CALL.search(code) or _TAG.search(code):
        return [], []
    return (["has tests but no `tag`. Every test must be filterable by its Testing Type "
             "(`npx playwright test --grep @regression`)."],
            ["Add `{ tag: ['@<testingtype>'] }` as the second argument of each `test(...)`, "
             "or once on the enclosing `test.describe(...)`."])


def check(generated_file: dict[str, Any], plans: list[dict[str, Any]] | None = None) -> VerifyResult:
    """Logic-level hygiene for one generated spec."""
    if generated_file.get("kind") != "spec":
        return VerifyResult.skipped(
            "spec_hygiene", "only test specs carry test logic",
            meta={"file": generated_file.get("path", "<unknown>")})

    source = generated_file.get("content", "") or ""
    path = generated_file.get("path", "<unknown>")

    errors: list[str] = []
    repairs: list[str] = []
    for finder in (lambda: _dropped_exact(source, plans),
                   lambda: _duplicate_bindings(source),
                   lambda: _hook_inside_a_test(source),
                   lambda: _async_describe(source),
                   lambda: _missing_teardown(source),
                   lambda: _hard_waits(_strip_comments(source)),
                   lambda: _snapshot_assertions(_strip_comments(source)),
                   lambda: _unjustified_force(source),
                   lambda: _untagged(_strip_comments(source))):
        found_errors, found_repairs = finder()
        errors.extend(f"{path}: {e}" for e in found_errors)
        repairs.extend(found_repairs)

    warnings = _conditional_warnings(source) + _unasserted_data(source)
    if errors:
        return VerifyResult.failed("spec_hygiene", errors, repairs=repairs,
                                   warnings=warnings)
    return VerifyResult.passed("spec_hygiene", warnings=warnings)
