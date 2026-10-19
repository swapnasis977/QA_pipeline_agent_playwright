"""One-time move of an existing checkout to the input/ + runs/ layout.

    py scripts/migrate_layout.py                 # DRY RUN: prints what would move
    py scripts/migrate_layout.py --apply         # does it
    py scripts/migrate_layout.py --apply --profile "Example App"

What it moves (never copies, never overwrites, never deletes a non-empty folder):

  context/<run>/<files>              -> runs/<profile>/<run>/<routed folder>/<file>
                                        (routing: utils/run_layout.py)
  profiles/<p>/knowledge/            -> profiles/<p>/input/docs/
  profiles/<p>/recordings/           -> profiles/<p>/input/recordings/

`context/` was shared by every application, so which profile owns a run is not
recorded anywhere. With exactly one profile that is unambiguous; with several,
pass --profile, or the script stops rather than guess.

Idempotent: a second run finds nothing left to move. A destination that already
exists is reported and SKIPPED — the script will not decide which copy wins.
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from utils.run_layout import artifact_path  # noqa: E402


def _profiles(root: Path) -> list:
    base = root / "profiles"
    if not base.is_dir():
        return []
    return sorted(p for p in base.iterdir() if p.is_dir() and any(
        (p / n).is_file() for n in ("project.json", "project.yaml", "project.yml")))


def _plan_runs(root: Path, profile: Path) -> list:
    """(src, dst) for every file under context/<run>/."""
    moves = []
    context = root / "context"
    if not context.is_dir():
        return moves
    for run in sorted(p for p in context.iterdir() if p.is_dir()):
        dest_run = root / "runs" / profile.name / run.name
        # Top-level entries only: a directory (figma_images/, evidence/) is
        # routed as one unit by its own name, keeping its contents together.
        for entry in sorted(run.iterdir()):
            moves.append((entry, artifact_path(dest_run, entry.name)))
    return moves


def _plan_inputs(profile: Path) -> list:
    moves = []
    for old, new in (("knowledge", "docs"), ("recordings", "recordings")):
        src = profile / old
        dst = profile / "input" / new
        if src.is_dir() and not dst.exists():
            moves.append((src, dst))
    return moves


def _tidy_empty(root: Path) -> None:
    """Removes now-empty run folders and an empty context/. rmdir only
    succeeds on an empty directory, so nothing with content is ever lost."""
    context = root / "context"
    if not context.is_dir():
        return
    for run in sorted(context.iterdir()):
        if run.is_dir():
            with contextlib.suppress(OSError):
                run.rmdir()
    with contextlib.suppress(OSError):
        context.rmdir()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--apply", action="store_true", help="actually move files (default: dry run)")
    ap.add_argument("--profile", default="", help="profile that owns the runs under context/")
    ap.add_argument("--root", default=str(ROOT), help="repository root (default: this checkout)")
    args = ap.parse_args()

    root = Path(args.root)
    profiles = _profiles(root)
    if not profiles:
        print("No profiles found under profiles/ — nothing to migrate.")
        return 0

    if args.profile:
        owner = next((p for p in profiles if p.name == args.profile), None)
        if owner is None:
            print(f"No profile named {args.profile!r}. Have: {', '.join(p.name for p in profiles)}")
            return 1
    elif len(profiles) == 1:
        owner = profiles[0]
    else:
        owner = None

    moves = []
    if (root / "context").is_dir():
        if owner is None:
            print("context/ is shared, and there is more than one profile "
                  f"({', '.join(p.name for p in profiles)}). Re-run with --profile <name> "
                  "to say which application owns those runs.")
            return 1
        moves += _plan_runs(root, owner)
    for p in profiles:
        moves += _plan_inputs(p)

    if not moves:
        print("Already on the new layout — nothing to move.")
        return 0

    skipped = moved = 0
    for src, dst in moves:
        rel_src, rel_dst = src.relative_to(root), dst.relative_to(root)
        if dst.exists():
            print(f"  SKIP (destination exists)  {rel_src}  ->  {rel_dst}")
            skipped += 1
            continue
        print(f"  {'MOVE' if args.apply else 'would move'}  {rel_src}  ->  {rel_dst}")
        if args.apply:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            moved += 1

    if args.apply:
        _tidy_empty(root)
        print(f"\nMoved {moved}, skipped {skipped}.")
    else:
        print(f"\nDry run: {len(moves) - skipped} to move, {skipped} to skip. "
              "Re-run with --apply to do it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
