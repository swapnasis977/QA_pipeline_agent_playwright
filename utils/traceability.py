"""Requirement -> test case -> script -> last result, in one table.

Each link already exists somewhere: the sheet names the story and criterion a case covers, a spec's
test title carries the case id, and the Playwright JSON names each test's status. This only joins them,
so a reviewer can answer "is this requirement automated, and did it pass?" without opening four files.

Deterministic: no model, no network. A case with no script says so; a script id that matches no case is
listed as orphaned rather than dropped.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

_ID = re.compile(r"[A-Z][A-Z0-9]*-\d+-(?:AUTO-)?TC-\d+")
_STATUS = {"expected": "passed", "unexpected": "failed", "flaky": "flaky", "skipped": "skipped"}


def _col(header: list, *names: str) -> int | None:
    lowered = [str(h or "").strip().lower() for h in header]
    for name in names:
        if name.lower() in lowered:
            return lowered.index(name.lower())
    return None


def _cases_from_sheet(sheet: Path) -> list[dict]:
    import openpyxl

    wb = openpyxl.load_workbook(sheet, read_only=True, data_only=True)
    rows = list(wb.worksheets[0].iter_rows(values_only=True))
    if not rows:
        return []
    header = list(rows[0])
    i_id = _col(header, "Test Case ID")
    i_name = _col(header, "Name", "Title")
    i_story = _col(header, "Source story", "Story", "JIRA Ref (Epic/Story)")
    i_type = _col(header, "Testing Type")
    i_ac = _col(header, "Control / behaviour", "Feature")
    i_text = [i for i in (_col(header, "Test Step"), _col(header, "Expected Result"),
                          _col(header, "Prerequisite"), _col(header, "Description")) if i is not None]
    if i_id is None:
        return []

    def cell(row, i):
        return "" if i is None or i >= len(row) or row[i] is None else str(row[i]).strip()

    return [{"id": cell(r, i_id), "title": cell(r, i_name), "story": cell(r, i_story),
             "type": cell(r, i_type), "criterion": cell(r, i_ac),
             "text": " ".join(cell(r, i) for i in i_text)} for r in rows[1:] if cell(r, i_id)]


def load_cases(run_dir: Path) -> list[dict]:
    """The run's cases: test_cases.json when present, else the best sheet in 1_testcases/."""
    folder = run_dir / "1_testcases"
    as_json = folder / "test_cases.json"
    if as_json.is_file():
        try:
            rows = json.loads(as_json.read_text(encoding="utf-8"))
        except ValueError:
            rows = []
        if rows:
            return [{"id": str(r.get("test_case_id", "")), "title": str(r.get("title", "")),
                     "story": run_dir.name.split("_", 1)[-1],
                     "type": ", ".join(r.get("tags") or []) if isinstance(r.get("tags"), list)
                     else str(r.get("tags", "")),
                     "criterion": str(r.get("acceptance_criterion", "")),
                     "text": " ".join([str(r.get("title", "")), str(r.get("preconditions", "")),
                                       *(str(x) for x in r.get("steps") or []),
                                       str(r.get("expected_result", ""))])}
                    for r in rows if isinstance(r, dict) and r.get("test_case_id") and not r.get("declined")]
    sheets = sorted(folder.glob("*.xlsx")) if folder.is_dir() else []
    # The automation suite carries the ids the scripts use; the master list carries the others.
    sheets.sort(key=lambda p: 0 if "automation_suite" in p.name.lower() else 1)
    for sheet in sheets:
        cases = _cases_from_sheet(sheet)
        if cases:
            return cases
    return []


def scripts_by_case(workspace: Path) -> dict[str, list[str]]:
    """{case id: [spec path relative to the workspace]} for every id a spec mentions."""
    found: dict[str, list[str]] = defaultdict(list)
    for spec in sorted((workspace / "tests").rglob("*.spec.ts")):
        rel = spec.relative_to(workspace).as_posix()
        for case_id in sorted(set(_ID.findall(spec.read_text(encoding="utf-8", errors="replace")))):
            found[case_id].append(rel)
    return found


def _walk_specs(suites: list):
    for suite in suites or []:
        yield from suite.get("specs") or []
        yield from _walk_specs(suite.get("suites") or [])


def results_by_case(run_dir: Path) -> dict[str, str]:
    """{case id: passed|failed|flaky|skipped} from the run's Playwright JSON."""
    path = run_dir / "3_results" / "run_results.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    out: dict[str, str] = {}
    for spec in _walk_specs(data.get("suites") or []):
        tests = spec.get("tests") or []
        status = _STATUS.get(str(tests[0].get("status")), "") if tests else ""
        for case_id in _ID.findall(str(spec.get("title", ""))):
            out[case_id] = status or ("passed" if spec.get("ok") else "failed")
    return out


def build(run_dir: Path, workspace: Path) -> dict:
    """{"rows": [...], "orphans": [...], "totals": {...}}; rows is empty when the run has no cases."""
    cases = load_cases(run_dir)
    scripts = scripts_by_case(workspace)
    results = results_by_case(run_dir)
    rows = []
    for c in cases:
        files = scripts.get(c["id"], [])
        rows.append({**c, "scripts": files, "result": results.get(c["id"], "not run") if files else ""})
    known = {c["id"] for c in cases}
    orphans = sorted(i for i in scripts if i not in known and i in results)
    totals = {
        "cases": len(rows),
        "with_script": sum(1 for r in rows if r["scripts"]),
        "passed": sum(1 for r in rows if r["result"] == "passed"),
        "failed": sum(1 for r in rows if r["result"] == "failed"),
    }
    return {"rows": rows, "orphans": orphans, "totals": totals}


def _short(text: str, n: int = 70) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def render(run_name: str, matrix: dict) -> str:
    t = matrix["totals"]
    lines = [
        f"# Traceability: {run_name}", "",
        f"{t['cases']} cases · {t['with_script']} with an automation script · "
        f"{t['passed']} passed · {t['failed']} failed · "
        f"{t['cases'] - t['with_script']} with no script (manual, declined or not yet automated).", "",
        "Requirement = the story or criterion the sheet records for the case. Script = the spec file "
        "whose test title carries the case id. Result = the last run in `3_results/`.", "",
        "| Requirement | Case ID | Title | Type | Script | Result |", "|---|---|---|---|---|---|",
    ]
    for r in sorted(matrix["rows"], key=lambda r: (r["story"], r["id"])):
        requirement = r["story"] + (f" · {_short(r['criterion'], 40)}" if r["criterion"] else "")
        scripts = ", ".join(f"`{Path(s).name}`" for s in r["scripts"]) or "none"
        lines.append(f"| {_short(requirement, 60)} | {r['id']} | {_short(r['title'])} | "
                     f"{_short(r['type'], 28)} | {scripts} | {r['result'] or '-'} |")
    if matrix["orphans"]:
        lines += ["", "## Scripts with no case in this story's sheet", ""]
        lines += [f"- {i}" for i in matrix["orphans"]]
    return "\n".join(lines) + "\n"
