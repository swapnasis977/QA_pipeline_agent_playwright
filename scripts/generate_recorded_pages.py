"""
Standalone entry point: mechanically turn recordings/*.ts (human-recorded
`npx playwright codegen` sessions) into real Page Object files + an updated
POM_SUMMARY.md. See utils/recording_parser.py's module docstring for the
full workflow this is one step of.

    py scripts/generate_recorded_pages.py --profile myapp

No LLM call happens in this script — same "pure code/templating" category
as agents/executor.py and agents/bug_reporter.py.
"""
import argparse
import sys
from pathlib import Path

# Runnable from any directory: the repo root (one level up) holds `utils` and `qa_pipeline`.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.recording_parser import generate  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile",
                        help="Use this profile's input/recordings/ folder and its workspace. "
                             "Replaces --recordings-dir / --repo-dir.")
    parser.add_argument("--recordings-dir",
                        help="Directory of *.ts codegen recordings (default with --profile: "
                             "the profile's input/recordings/)")
    parser.add_argument("--repo-dir",
                        help="Target Playwright repo — writes into <repo-dir>/tests/pages/recorded/ "
                             "and rewrites <repo-dir>/POM_SUMMARY.md's auto-generated section.")
    args = parser.parse_args()

    recordings_arg, repo_arg = args.recordings_dir, args.repo_dir
    if args.profile:
        from qa_pipeline import profiles
        profile = profiles.load(args.profile)
        recordings_arg = recordings_arg or str(profile.recordings_dir)
        repo_arg = repo_arg or str(profile.workspace)
    if not repo_arg:
        parser.error("give --profile <name>, or --repo-dir <workspace>.")

    recordings_dir = Path(recordings_arg or "./recordings")
    if not recordings_dir.exists():
        print(f"No recordings directory at '{recordings_dir}' — nothing to do.")
        return

    result = generate(recordings_dir, Path(repo_arg))

    for g in result["generated"]:
        skipped_note = f" ({len(g['skipped_login'])} login step(s) skipped)" if g["skipped_login"] else ""
        print(f"[OK] {g['test_case_id']} -> {g['file']} "
              f"({len(g['properties'])} locator(s)){skipped_note}")
    for s in result["skipped_files"]:
        print(f"[WARN] {s['file']}: {s['reason']}")

    if result["generated"]:
        print(f"\nPOM_SUMMARY.md updated with {len(result['generated'])} recorded page object(s).")
    else:
        print("\nNo usable recordings found — any stale auto-generated section in "
              "POM_SUMMARY.md was cleared.")


if __name__ == "__main__":
    main()
