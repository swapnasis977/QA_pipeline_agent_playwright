"""Where each file of a run folder lives — the ONE place that decides.

A run folder used to be flat: 30-70 files side by side, deliverables mixed with
bookkeeping, so nobody could tell which file to open. Every agent wrote
`feature_dir / "<name>"` directly, which meant the layout was spread over ~100
call sites and could not be changed without touching all of them.

Now every call site asks `artifact_path(feature_dir, name)` and this module
answers. The layout:

    <run dir>/
      summary.md          the one file to open first
      1_testcases/        the final test cases: one .xlsx (+ test_cases.json)
      2_automation/       what was planned and generated, and the review of it
      3_results/          execution results, triage, heals, bugs, evidence
      _internal/          bookkeeping: manifests, token logs, caches, raw LLM
                          responses, diagnostics. Safe to ignore, never to edit.

DEFAULT IS `_internal/`. A filename this table does not know lands there, so a
new agent that forgets to register its file adds clutter to `_internal/` and
cannot pollute the folder a person reads. Promote a file to a visible folder
deliberately, by adding it below.

Read and write both go through here, so they cannot disagree about where a
file is. Nothing falls back to the old flat location on purpose: two places
for one file is how a stale copy gets read. Old run folders are moved once, by
`scripts/migrate_layout.py`.
"""

from __future__ import annotations

import fnmatch
from pathlib import Path

INTERNAL = "_internal"
TESTCASES = "1_testcases"
AUTOMATION = "2_automation"
RESULTS = "3_results"

#: Files that stay at the top of the run folder.
_TOP = ("summary.md", "README.md", "TRACEABILITY.md", "EFFORT_REPORT.md",
        "pipeline_state.yml")

#: filename (fnmatch pattern) -> folder. First match wins; patterns are matched
#: against the filename only, never a path. Anything unmatched is _INTERNAL.
_ROUTES = (
    # -- manual test cases a human reviews --------------------------------
    ("*_TestCases.xlsx", TESTCASES),
    ("*_Automation_Suite_Consolidated.xlsx", TESTCASES),
    ("test_cases.json", TESTCASES),   # machine-readable twin of the sheet
    ("SCREEN_MODEL.md", TESTCASES),   # the Figma screen object model, readable
    # -- what was planned / generated / reviewed --------------------------
    ("automation_plan.json", AUTOMATION),
    ("planner_doc.json", AUTOMATION),
    ("generated_files.json", AUTOMATION),
    ("review_report.json", AUTOMATION),
    ("needs_demonstration.json", AUTOMATION),
    ("testdata_template.json", AUTOMATION),   # per-field valid/negative/boundary values to review
    ("scripts", AUTOMATION),     # read-only copies of the story's specs (qa_pipeline/story_bundle.py)
    # -- what happened when it ran ----------------------------------------
    ("run_results*.json", RESULTS),
    ("run_result.json", RESULTS),
    ("classifications.json", RESULTS),
    ("heal_outcomes.json", RESULTS),
    ("BUG_LIST.md", RESULTS),
    ("bug_screenshots", RESULTS),
    ("evidence", RESULTS),
    ("heals", RESULTS),          # one diff per heal attempt: what a heal changed
    ("jira_upload_result.json", RESULTS),
)


def folder_for(filename: str) -> str:
    """The subfolder (possibly "" for the top) a filename belongs in."""
    name = Path(str(filename)).name
    if name in _TOP:
        return ""
    for pattern, folder in _ROUTES:
        if fnmatch.fnmatchcase(name, pattern):
            return folder
    return INTERNAL


def artifact_path(feature_dir: str | Path, filename: str | Path) -> Path:
    """`feature_dir/<routed folder>/<filename>`.

    Does not create the folder: every writer goes through
    `agents.artifact_io.write_text_file`, which makes parents, and a reader
    must not create directories as a side effect of looking.

    A `filename` that already carries a directory part (`"evidence/x.png"`) is
    routed by its first component, so a caller can pass a sub-path.
    """
    parts = Path(str(filename)).parts
    head = parts[0] if parts else ""
    folder = folder_for(head)
    return Path(feature_dir).joinpath(*([folder] if folder else []), *parts)
