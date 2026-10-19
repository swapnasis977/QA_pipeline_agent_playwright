"""Builds the FINAL automation sheet: one representative case per distinct tool behaviour.

    py scripts/build_final_automation_sheet.py --profile "Example App" --feature PROJ-68294 --run DOC_DOC-1

Starts from every case (the master sheet's rows), drops manual-only and engineering-only cases,
groups the rest by feature + control + behaviour, keeps the best case of each group (an
already-scripted case is always kept) and writes one worksheet in the pipeline's own column
layout, so the Coding Agent can read it. Prints which tool controls have NO case at all.
See utils/final_suite.py for the rules.
"""

from __future__ import annotations

import argparse
import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_pipeline import profiles  # noqa: E402
from utils import final_suite as fs  # noqa: E402
from utils import master_sheet as ms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--feature", required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--out", help="output path (default: <feature run>/1_testcases/)")
    args = parser.parse_args()

    profile = profiles.load(args.profile)
    rows = ms.collect_rows(profile.context_root, profile.workspace, args.feature, args.run)
    if rows is None:
        print("No per-story sheets to build from."
              "\n  The source sheets are not in the working folders. If they were archived, extract "
              "backup/superseded_sheets_*.zip into the project root (it keeps the original "
              "paths) and re-run.")
        return 1
    scope = fs.load_scope(profile.root / "automation_scope.json")
    chosen = fs.select(rows, scope)
    suite = chosen["suite"]
    out = Path(args.out) if args.out else (
        profile.context_root / f"{args.feature}_FEATURE" / "1_testcases"
        / f"{args.feature}_L2Tool_Final_Automation_Suite.xlsx")
    fs.write_final_sheet(suite, out)

    print(f"Wrote {out}\n")
    print(f"  all cases considered                 {len(rows)}")
    print(f"  excluded: manual-only                {chosen['excluded_manual']}")
    print(f"  excluded: engineering / back-end     {chosen['excluded_engineering']}")
    if scope is not None:
        print(f"  excluded: not on this screen         {chosen['excluded_not_on_screen']}")
        print(f"  already covered by an existing script {chosen['covered']}")
        print(f"  excluded by decision                 {chosen['excluded_by_decision']}")
    print(f"  merged into a similar case           {chosen['merged']}")
    print(f"  FINAL AUTOMATION SUITE               {len(suite)}\n")
    by_decision = collections.Counter(r.get("Scope Decision") or r["Automation Status"] for r in suite)
    for decision, n in by_decision.most_common():
        print(f"    {decision:52s} {n}")
    print("\n  cases per tool feature:")
    for feature, n in collections.Counter(r["Feature"] for r in suite).most_common():
        print(f"    {n:4d}  {feature}")
    gaps = [label for label, n in fs.coverage(suite, (scope or {}).get("screen_controls")) if n == 0]
    print(f"\n  tool controls with NO case in the suite ({len(gaps)}): "
          + (", ".join(gaps) if gaps else "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
