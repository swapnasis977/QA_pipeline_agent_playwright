"""`qa-pipeline coverage`: which controls of the application do the scripts actually exercise?

The question "is every control tested?" used to be answered by matching words in test NAMES, which
says a case talks about a control, not that its script touches it. This answers it from the code:
every locator a page object declares is a control; a control is covered by each test whose body
uses it. A control used by no test is untested; used by exactly one is thin.

Pure text analysis of `src/pages/*.ts` and `tests/**/*.ts`: no browser, no model.
"""

from __future__ import annotations

import re
from pathlib import Path

_MEMBER = re.compile(r"^\s*readonly\s+(\w+)\s*:\s*Locator\s*;", re.M)
_TEST_START = re.compile(r"^\s*test\(|^\s*for \(", re.M)
_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+-(?:AUTO-)?TC-\d+|TC-\d+")


def controls(workspace: Path) -> dict[str, str]:
    """{member name: the page object that declares it} for every Locator member."""
    found: dict[str, str] = {}
    for page in sorted((workspace / "src" / "pages").glob("*.ts")):
        for name in _MEMBER.findall(page.read_text(encoding="utf-8")):
            found.setdefault(name, page.stem)
    return found


def _chunks(text: str) -> list[str]:
    """The file split at each top-level test, so a reference counts for the test it is in."""
    starts = [m.start() for m in _TEST_START.finditer(text)]
    if not starts:
        return [text]
    return [text[a:b] for a, b in zip(starts, starts[1:] + [len(text)], strict=True)]


def usage(workspace: Path, names) -> dict[str, set[str]]:
    """{member: the set of tests (file::first id or title) that use it}."""
    used: dict[str, set[str]] = {n: set() for n in names}
    patterns = {n: re.compile(r"\." + re.escape(n) + r"\b") for n in names}
    for spec in sorted((workspace / "tests").rglob("*.ts")):
        text = spec.read_text(encoding="utf-8")
        for number, chunk in enumerate(_chunks(text)):
            ids = _ID.findall(chunk[:400])
            label = f"{spec.name}::{ids[0] if ids else number}"
            for name, pattern in patterns.items():
                if pattern.search(chunk):
                    used[name].add(label)
    return used


def report(workspace) -> dict:
    workspace = Path(workspace)
    declared = controls(workspace)
    used = usage(workspace, declared)
    rows = [{"control": n, "page": declared[n], "tests": len(used[n])} for n in declared]
    rows.sort(key=lambda r: (r["tests"], r["control"]))
    return {
        "controls": rows,
        "total": len(rows),
        "untested": [r["control"] for r in rows if r["tests"] == 0],
        "thin": [r["control"] for r in rows if r["tests"] == 1],
    }


def cmd_coverage(args, profile) -> int:
    result = report(profile.workspace)
    if not result["total"]:
        print(f"      No page-object controls found under {profile.workspace / 'src' / 'pages'}.")
        return 1
    covered = result["total"] - len(result["untested"])
    print(f"      Controls declared in the page objects: {result['total']}")
    print(f"      Used by at least one test:             {covered} "
          f"({100 * covered / result['total']:.0f}%)")
    print(f"      Used by exactly one test (thin):       {len(result['thin'])}")
    print(f"      Used by NO test:                       {len(result['untested'])}")
    for row in result["controls"]:
        if row["tests"] <= 1:
            print(f"        {row['tests']} test(s)  {row['control']}  ({row['page']})")
    print("      A control counts when a test's code uses it; whether the test ASSERTS enough about "
          "it is a review question, not something this can tell.")
    return 0
