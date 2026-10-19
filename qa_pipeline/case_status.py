"""`qa-pipeline status`: per-case progress, from the run's own artifacts.

Refreshes each run's pipeline_state.yml first (deterministic, no model, no
network), then prints one table per run. See orchestrator/case_state.py.
"""

from __future__ import annotations

from qa_pipeline.orchestrator import case_state
from utils.run_layout import artifact_path


def cmd_status(args, profile) -> int:
    root = profile.context_root
    if not root.is_dir():
        print(f"      No runs under {root}.")
        return 1
    folders = [root / args.run] if args.run else sorted(
        d for d in root.iterdir() if d.is_dir() and not d.name.startswith("_"))
    shown = 0
    for run_dir in folders:
        if not artifact_path(run_dir, "test_cases.json").is_file():
            continue
        path = case_state.refresh(run_dir, profile.workspace)
        state = case_state.derive(run_dir, profile.workspace,
                                  {"built_from": case_state.load_built_from(path)})
        print(f"\n{run_dir.name}  ({path})")
        print(case_state.render_table(state))
        shown += 1
    if not shown:
        print("      No run with test cases found.")
    return 0 if shown else 1
