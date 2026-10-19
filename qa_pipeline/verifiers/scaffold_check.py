"""Does this land where the framework says it should?

The scaffold is deterministic and identical on every project (see
`agents/framework_scaffolder.py`). That is what makes the generated suite
ownable by a human after the agents stop running — you can open any project
this pipeline has touched and find page objects in the same place.

This gate is what keeps it that way. Without it the model drifts: `pages/`
one run and `page-objects/` the next, a spec at the repo root, a page object
written into `tests/e2e/`. Each individual instance looks harmless. The
cumulative effect is a repository nobody can navigate and a Coding Agent that
can no longer find the page object it wrote last week to reuse it.

It also enforces the path rule that caused real, silent breakage. Playwright's
CLI treats each positional file argument as a REGEX matched against test file
paths, so an absolute Windows path (drive letter, backslashes) breaks that
regex match entirely — the run yields "No tests found" with 0 tests collected,
and exits 0. A `target_file` that is absolute or backslashed is therefore not a
style problem: it is a run that executes nothing and reports success.
"""

from __future__ import annotations

import posixpath
import re

from .result import VerifyResult

#: Where each kind of generated file belongs, relative to the repo root.
#: Mirrors `framework/templates/`. A file's `kind` decides which prefix it
#: must sit under.
LAYOUT = {
    "spec": ("tests/",),
    "page": ("src/pages/",),
    "component": ("src/components/",),
    "fixture": ("src/fixtures/",),
    "data": ("src/data/",),
    "util": ("src/utils/",),
}

#: Specs are further split by kind so the Executor can shard by directory and
#: a human can run "just the API tests" without a tag convention.
SPEC_SUBDIRS = {"ui": "tests/ui/", "api": "tests/api/", "e2e": "tests/e2e/"}

_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|/|\\\\)")


def check_path(path: str, kind: str = "spec", test_kind: str = "") -> VerifyResult:
    errors, repairs, warnings = [], [], []
    raw = str(path or "")

    if not raw:
        return VerifyResult.failed(
            "scaffold_check", ["a generated file has an empty path"],
            repairs=["Every file needs a repo-relative path, e.g. 'tests/e2e/login.spec.ts'."])

    if _ABSOLUTE.match(raw):
        errors.append(
            f"{raw!r} is an ABSOLUTE path. Playwright's CLI treats a positional "
            f"file argument as a regex, so an absolute path matches nothing and "
            f"the run silently collects 0 tests.")
        repairs.append(
            f"Change {raw!r} to a repo-relative, forward-slash path "
            f"(for example 'tests/e2e/my-case.spec.ts').")

    if "\\" in raw:
        errors.append(f"{raw!r} contains backslashes; paths must use forward slashes.")
        repairs.append(f"Rewrite {raw!r} using forward slashes only.")

    normalized = re.sub(r"^(?:\./)+", "", raw.replace("\\", "/")).lstrip("/")
    if ".." in normalized.split("/"):
        errors.append(f"{raw!r} climbs out of the repository with '..'.")
        repairs.append(f"Keep {raw!r} inside the repository; no '..' segments.")

    if errors:
        return VerifyResult.failed("scaffold_check", errors, repairs=repairs,
                                   meta={"path": raw})

    allowed = LAYOUT.get(kind)
    if allowed is None:
        warnings.append(f"{normalized}: unknown file kind {kind!r}; placement not checked.")
        return VerifyResult.passed("scaffold_check", warnings=warnings, meta={"path": normalized})

    if kind == "spec" and test_kind in SPEC_SUBDIRS:
        expected = SPEC_SUBDIRS[test_kind]
        if not normalized.startswith(expected):
            return VerifyResult.failed(
                "scaffold_check",
                [f"{normalized}: a {test_kind} spec must live under {expected}"],
                repairs=[f"Move {normalized!r} to "
                         f"{posixpath.join(expected, posixpath.basename(normalized))!r}."],
                meta={"path": normalized})
        return VerifyResult.passed("scaffold_check", meta={"path": normalized})

    if not any(normalized.startswith(prefix) for prefix in allowed):
        return VerifyResult.failed(
            "scaffold_check",
            [f"{normalized}: a '{kind}' file must live under {' or '.join(allowed)}"],
            repairs=[f"Move {normalized!r} under {allowed[0]} — the framework scaffold "
                     f"keeps every '{kind}' file there so it can be found and reused."],
            meta={"path": normalized})

    if kind == "spec" and not normalized.endswith((".spec.ts", ".spec.js")):
        warnings.append(
            f"{normalized}: specs are conventionally named '*.spec.ts'; Playwright's "
            f"default testMatch may not collect this file.")

    return VerifyResult.passed("scaffold_check", warnings=warnings, meta={"path": normalized})


def check_file(generated_file: dict, test_kind: str = "") -> VerifyResult:
    kind = generated_file.get("kind", "spec")
    result = check_path(generated_file.get("path", ""), kind=kind, test_kind=test_kind)
    if result.status != "pass" or kind != "spec":
        return result

    # A spec that declares no case ids is a spec whose failures are attributed
    # to nothing: the Reviewer cannot map them, the Self-Healer cannot find the
    # file to patch, and the Bug Reporter files them with no screenshot. This
    # is checked here rather than in the contract because it is only true for
    # a spec — a page object implements no case of its own.
    if not [i for i in (generated_file.get("test_case_ids") or []) if str(i).strip()]:
        path = generated_file.get("path", "<unknown>")
        return VerifyResult.failed(
            "scaffold_check",
            [f"{path}: a spec must declare the case ids it implements in "
             f"'test_case_ids'; it declared none."],
            repairs=[f"Add \"test_case_ids\" to {path}, listing the full "
                     f"story-prefixed id of every test case implemented in that "
                     f"file (e.g. [\"PROJ-1234-TC-001\"]). It is the only link "
                     f"between a Playwright failure and a test case."],
            meta=result.meta)

    content = generated_file.get("content") or ""
    path = generated_file.get("path", "<unknown>")

    vacuous = _vacuous_assertions(content)
    if vacuous:
        return VerifyResult.failed(
            "scaffold_check",
            [f"{path}: {len(vacuous)} assertion(s) that cannot fail: "
             + "; ".join(vacuous[:5])],
            repairs=[
                f"Replace the always-true assertion(s) in {path} with one that "
                f"waits for a real element of the screen under test "
                f"(e.g. `await expect(pageObject.someControl).toBeVisible()`). "
                f"An assertion like `expect(page).toHaveURL(/.*/)` matches every "
                f"possible URL, so it neither verifies anything nor waits for "
                f"anything — and the step after it then runs against a page that "
                f"has not rendered yet."],
            meta=result.meta)

    fake_waits = _non_waiting_polls(content)
    if fake_waits:
        return VerifyResult.failed(
            "scaffold_check",
            [f"{path}: {len(fake_waits)} call(s) pass a timeout to a method that "
             f"does not wait: " + "; ".join(fake_waits[:5])],
            repairs=[
                f"In {path}, replace `isVisible({{ timeout: N }})` with a call "
                f"that actually waits: "
                f"`await locator.waitFor({{ state: 'visible', timeout: N }})"
                f".then(() => true).catch(() => false)` when you need a boolean, "
                f"or `await expect(locator).toBeVisible({{ timeout: N }})` when "
                f"the element is required. Playwright's `isVisible()` returns "
                f"the CURRENT state immediately and ignores `timeout` entirely, "
                f"so the wait you wrote does not happen."],
            meta=result.meta)
    return result


#: Playwright predicates that check state RIGHT NOW and ignore a `timeout`
#: option if one is passed.
#:
#: `locator.isVisible({ timeout: 10000 })` reads like "wait up to ten seconds
#: for this to appear". It does not: it returns immediately, and the option is
#: silently ignored. Generated code used it to decide whether this app's
#: "Non compliant image" banner had appeared after a capture — the banner takes
#: a few seconds to render, the check ran 17 ms after the click, saw nothing,
#: and skipped the recapture the test existed to exercise. The step reported
#: success and the test then failed a minute later waiting for a result that a
#: recapture would have produced.
_NON_WAITING_POLL_RE = re.compile(
    r"\.\s*(isVisible|isHidden|isEnabled|isDisabled|isChecked|isEditable)\s*"
    r"\(\s*\{[^}]*\btimeout\b[^}]*\}\s*\)"
)


def _non_waiting_polls(content: str) -> list:
    return [m.group(0).strip() for m in _NON_WAITING_POLL_RE.finditer(content or "")]


#: Assertions whose argument matches literally anything. They pass on every
#: page, including a blank one.
#:
#: These are worse than useless in a Playwright spec, because an assertion is
#: also the synchronisation primitive: `await expect(...).toBeVisible()` is how
#: a step waits for the screen it needs. A generated
#: `await expect(page).toHaveURL(/.*/)` as the "verify the dashboard loaded"
#: step therefore removed the wait as well as the check — the next step clicked
#: the dashboard tile 600 ms in, before the tile existed, and the test failed
#: two steps later on a popup that had never been opened. The failure surfaced
#: nowhere near the line that caused it.
_VACUOUS_ASSERTION_RES = (
    # toHaveURL / toHaveTitle given a match-anything regex: /.*/ , /.+/ , /./ ,
    # /(.*)/ and the same with flags.
    re.compile(r"\.\s*(toHaveURL|toHaveTitle)\s*\(\s*/\(?\.[*+]?\)?/[a-z]*\s*\)"),
    # expect(<literal>).toBe(<same literal>) — a tautology, not a check.
    re.compile(r"expect\s*\(\s*(true|false|1|0)\s*\)\s*\.\s*toBe\s*\(\s*\1\s*\)"),
)


def _vacuous_assertions(content: str) -> list:
    found = []
    for pattern in _VACUOUS_ASSERTION_RES:
        for m in pattern.finditer(content or ""):
            found.append(m.group(0).strip())
    return found


def check_plan(plan: dict) -> VerifyResult:
    """A plan's `target_file` is checked BEFORE the Coding Agent runs.

    Catching a bad target here costs nothing; catching it after generation
    means the file was written to the wrong place and the Executor's scoped
    run silently misses it.
    """
    result = check_path(plan.get("target_file", ""), kind="spec",
                        test_kind=plan.get("test_kind", ""))
    if result.status != "pass":
        return result

    errors, repairs = [], []
    for field, prefix in (("page_objects", "src/pages/"),
                          ("components", "src/components/"),
                          ("fixtures", "src/fixtures/")):
        for entry in plan.get(field) or []:
            normalized = re.sub(r"^(?:\./)+", "", str(entry).replace("\\", "/"))
            if normalized and not normalized.startswith(prefix):
                errors.append(f"{plan.get('test_case_id', '?')}: {field} entry "
                              f"{entry!r} must live under {prefix}")
                repairs.append(f"Change the {field} entry {entry!r} to a path under {prefix}.")

    if errors:
        return VerifyResult.failed("scaffold_check", errors, repairs=repairs,
                                   meta={"test_case_id": plan.get("test_case_id")})
    return VerifyResult.passed("scaffold_check", warnings=result.warnings,
                               meta=result.meta)
