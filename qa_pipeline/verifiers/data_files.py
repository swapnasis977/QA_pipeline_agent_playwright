"""Test data a spec loads must exist in the workspace.

A generated upload spec did `path.resolve(__dirname, '../../src/data/test-portrait.jpg')`
and nothing in the workspace held that file, so the test failed on a fresh clone
(and for anyone but the machine that generated it) at a step that has nothing to do
with what it tests. Nothing noticed: the compile gate sees a string, the locator gate
sees no locator.

A WARNING and a report, not a hard failure, on purpose. The Coding Agent cannot
produce a face image, so rejecting the spec would block every upload test until a
person adds the files - and a made-up 1x1 placeholder would pass the gate while being
useless to a biometric app. What matters is that the gap is named, early, and listed
once, rather than discovered as N unrelated-looking failures.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

#: `.../src/data/<name>` inside a string literal, any number of `../` before it.
_DATA_REF = re.compile(r"""['"`](?:\.{1,2}/)*(?:\./)?src/data/([\w./ -]+?)['"`]""")


def referenced(source: str) -> list:
    """File names under src/data/ that `source` refers to, in order, de-duplicated."""
    seen = []
    for match in _DATA_REF.finditer(source or ""):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.append(name)
    return seen


def missing(files: list[dict[str, Any]], repo_root: str | Path) -> dict[str, list[str]]:
    """{data file: [specs that need it]} for references with no file behind them."""
    root = Path(repo_root) / "src" / "data"
    gaps: dict[str, list[str]] = {}
    for generated in files or []:
        for name in referenced(generated.get("content", "")):
            if not (root / name).is_file():
                gaps.setdefault(name, []).append(generated.get("path", "?"))
    return gaps


def warnings_for(generated_file: dict[str, Any], repo_root: str | Path) -> list[str]:
    return [f"loads src/data/{name}, which does not exist in the workspace - the test "
            f"will fail on any machine until that file is added."
            for name in missing([generated_file], repo_root)]
