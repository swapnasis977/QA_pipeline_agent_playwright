"""Builds ONE sheet of every test case, with its automation status and last run result.

    py scripts/build_master_sheet.py --profile "Example App" --feature PROJ-68294 --run DOC_DOC-1

Reads (never changes) the per-story sheets, the consolidated sheet, the run's sheet, the
generated spec files and the last run's results. Writes one workbook with one worksheet to the
feature's `1_testcases/` folder. See utils/master_sheet.py for how the pieces are joined.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from qa_pipeline import profiles  # noqa: E402
from utils import final_suite as fs  # noqa: E402
from utils import master_sheet as ms  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--profile", required=True)
    parser.add_argument("--feature", required=True, help="the JIRA Feature key, e.g. PROJ-68294")
    parser.add_argument("--run", required=True,
                        help="the run folder whose sheet, results and scripts to use, e.g. DOC_DOC-1")
    parser.add_argument("--out", help="output path (default: <feature run>/1_testcases/)")
    args = parser.parse_args()

    profile = profiles.load(args.profile)
    rows = ms.collect_rows(profile.context_root, profile.workspace, args.feature, args.run)
    if rows is None:
        print(f"No per-story sheets under {profile.context_root}/{args.feature}_FEATURE/"
              f"_internal/per_story/1_testcases/."
              "\n  The source sheets are not in the working folders. If they were archived, extract "
              "backup/superseded_sheets_*.zip into the project root (it keeps the original "
              "paths) and re-run.")
        return 1
    # A decision for every case (covered / new / not on this screen ...), from the profile's rules.
    fs.annotate(rows, fs.load_scope(profile.root / "automation_scope.json"))
    out = Path(args.out) if args.out else (
        profile.context_root / f"{args.feature}_FEATURE" / "1_testcases"
        / f"{args.feature}_L2Tool_Master_TestCases.xlsx")
    ms.write_sheet(rows, out)

    print(f"Wrote {out}\n")
    for label, count in ms.summarize(rows).items():
        print(f"  {label:50s} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
