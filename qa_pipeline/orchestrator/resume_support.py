"""Resume: rebuilding a skipped stage's state from the artifacts it wrote.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

from agents.artifact_io import read_json
from qa_pipeline.agents import adapters
from qa_pipeline.contracts import write_validated
from utils import case_index as _case_index
from utils.run_layout import artifact_path


def _rehydrate_test_cases(ctx) -> dict:
    """Reloads the approved cases for a resumed run.

    THE SHEET WINS. `test_cases.json` is a derived cache; the spreadsheet is
    the artifact a human actually reviewed and marked `Reviewed`, and it is
    the one they edit. When the two disagree the cache is stale, and trusting
    it silently re-runs work the human has already changed their mind about:
    a sheet cut back to one approved case was resumed against a cached set of
    thirteen, and the pipeline planned and costed twelve cases nobody had
    asked for — including screens outside the feature entirely. Nothing in the
    output said which source it had used.
    """
    cases = read_json(ctx.artifact("test_cases.json"), default=[]) or []
    if ctx.sheet_path.exists():
        from_sheet = adapters.read_test_cases(ctx.sheet_path)
        cached_ids = {c.get("test_case_id") for c in cases}
        sheet_ids = {c.get("test_case_id") for c in from_sheet}
        if from_sheet and cached_ids != sheet_ids:
            only_cached = sorted(cached_ids - sheet_ids)
            print(f"      Cached test_cases.json disagrees with {ctx.sheet_path.name} "
                  f"({len(cases)} cached vs {len(from_sheet)} in the sheet) — using the "
                  f"sheet, which is what was reviewed.")
            if only_cached:
                print(f"      Dropping {len(only_cached)} stale cached case(s): "
                      f"{', '.join(only_cached[:8])}"
                      + (" ..." if len(only_cached) > 8 else ""))
            cases = from_sheet
            write_validated(ctx.artifact("test_cases.json"), "test_case", cases, many=True)
        elif not cases:
            cases = from_sheet
    if not cases:
        return None
    return {"cases": cases, "verify": None, "sheet": str(ctx.sheet_path)}


def _rehydrate_plan(ctx) -> dict:
    plans = read_json(ctx.artifact("automation_plan.json"), default=[]) or []
    if not plans:
        plans = [adapters.normalize_plan(p) for p in
                 (read_json(artifact_path(ctx.feature_dir, "planner_doc.json"), default=[]) or [])
                 if isinstance(p, dict)]
    if not plans:
        return None
    cases = (ctx.state.get("testcase_writer", {}) or {}).get("cases") or []
    return {
        "plans": plans,
        "unresolved": read_json(ctx.artifact("needs_demonstration.json"), default=[]) or [],
        "cases": cases,
        "case_index": _case_index.read(ctx.feature_dir),
        "planner_doc": str(artifact_path(ctx.feature_dir, "planner_doc.json")),
    }


def _rehydrate_code(ctx) -> dict:
    files = read_json(ctx.artifact("generated_files.json"), default=[]) or []
    if not files:
        return None
    return {"files": files, "verify": None, "declined": False}


def _rehydrate_run(ctx) -> dict:
    result = read_json(ctx.artifact("run_result.json"), default={}) or {}
    return result or None


def _rehydrate_review(ctx) -> dict:
    classifications = read_json(ctx.artifact("classifications.json"), default=[]) or []
    review_path = artifact_path(ctx.feature_dir, "review_report.json")
    if not classifications and not review_path.is_file():
        return None
    return {"classifications": classifications, "review_path": str(review_path)}


def _rehydrate_heal(ctx) -> dict:
    outcomes = read_json(ctx.artifact("heal_outcomes.json"), default=[]) or []
    if not outcomes:
        return None
    return {
        "outcomes": outcomes,
        "healed": [o.get("test_case_id") for o in outcomes if o.get("healed")],
        "still_failing": [o.get("test_case_id") for o in outcomes if not o.get("healed")],
    }
