"""Writes the framework skeleton. Deterministic — no model call, ever.

This stage is the one the design explicitly took AWAY from the Automation
Planner. The original plan had the Planner own POM structure, component
objects, test data management, utilities, configuration and locator strategy.
Letting a model author those per run means the architecture drifts every run:
`pages/` one run and `page-objects/` the next, a different base-class
signature, a different fixture convention. That loses precisely the property
that makes generated code worth having — that a human can own it afterwards.

So the split is: this stage owns STRUCTURE and is deterministic; the Planner
owns PLACEMENT within that structure and is where the model earns its keep.

NEVER OVERWRITES. A file that already exists is left exactly as it is. The
scaffold is a seed, not a template that reasserts itself — someone who has
tuned their `playwright.config.ts` must not find it reverted by a later run.
This makes the stage safe to run on every invocation, which in turn means a
workspace can never be half-scaffolded.
"""

from __future__ import annotations

from pathlib import Path

from agents.artifact_io import read_text_file, write_text_file
from qa_pipeline.framework import templates


def run(workspace, *, verbose: bool = True) -> dict:
    """Creates the scaffold under `workspace`. Idempotent."""
    root = Path(workspace)
    root.mkdir(parents=True, exist_ok=True)

    created, kept = [], []

    for relative in templates.DIRECTORIES:
        (root / relative).mkdir(parents=True, exist_ok=True)

    for relative, content in templates.FILES.items():
        target = root / relative
        if target.exists():
            kept.append(relative)
            continue
        write_text_file(target, content)
        created.append(relative)

    # `.gitkeep` in the spec directories so an empty `tests/api/` survives a
    # git clone. Without it a fresh checkout has no `tests/api/`, and the
    # scaffold check then rejects the first API spec for landing in a
    # directory that "should not exist".
    for relative in templates.DIRECTORIES:
        keeper = root / relative / ".gitkeep"
        if not any((root / relative).iterdir()):
            keeper.touch()

    drifted = _drifted(root, kept)

    result = {"workspace": str(root), "created": created, "kept": kept,
              "drifted": drifted, "scaffolded": bool(created)}

    if verbose:
        if created:
            print(f"      Scaffolded {len(created)} file(s) into {root}")
            for relative in created:
                print(f"        + {relative}")
        if kept:
            print(f"      {len(kept)} existing file(s) left untouched.")
        if drifted:
            print(f"      ⚠️  {len(drifted)} scaffold file(s) differ from the current "
                  f"template. They are NOT overwritten — review and merge by hand if "
                  f"you want the newer version:")
            for relative in drifted:
                print(f"        ~ {relative}")
        if not created and not kept:
            print("      Nothing to scaffold.")

    return result


#: Files a project is EXPECTED to edit. Reporting these as drifted would
#: train the reader to ignore the whole report, which is how a real
#: regression in BasePage goes unnoticed.
_EXPECTED_TO_DIVERGE = frozenset({
    "README.md", "POM_SUMMARY.md", "src/data/README.md",
    "package.json", ".gitignore", "playwright.config.ts",
    ".env.example", "bitbucket-pipelines.yml",
})


def _drifted(root: Path, kept: list) -> list:
    """Scaffold files on disk whose content no longer matches the template.

    NEVER OVERWRITES is the right rule — someone who tuned their config must
    not find it reverted — but on its own it leaves no upgrade path and no
    signal. A workspace scaffolded once keeps its `BasePage.ts` forever, so a
    fix made in the template reaches only NEW projects, and the maintainer of
    an existing one has no way to discover they are behind.

    That is not hypothetical: this scaffold shipped a `src/fixtures/index.ts`
    whose regex did not compile, and an `auth.setup.ts` that probed an
    interstitial with `isVisible()` — which does not wait — so every run
    failed at sign-in. Both were fixed in the template. Without this report,
    every workspace already on disk would silently keep the broken copies.

    Reporting, not merging: a three-way merge of hand-tuned code is not
    something this stage should attempt, and a diff a human reads is enough
    to decide. Files a project is meant to own are excluded — see
    _EXPECTED_TO_DIVERGE.
    """
    drifted = []
    for relative in kept:
        if relative in _EXPECTED_TO_DIVERGE:
            continue
        expected = templates.FILES.get(relative)
        if expected is None:
            continue
        actual = read_text_file(root / relative, default=None)
        if actual is None:
            continue
        if actual.replace("\r\n", "\n") != expected.replace("\r\n", "\n"):
            drifted.append(relative)
    return drifted


def is_scaffolded(workspace) -> bool:
    """True when the essential structural files are present.

    Checks the files whose ABSENCE would break generated code, not every file
    in the template — a project that has deleted `README.md` is still a
    working scaffold, while one missing `BasePage.ts` is not.
    """
    root = Path(workspace)
    essentials = ("playwright.config.ts", "src/pages/BasePage.ts",
                  "src/fixtures/index.ts", "src/config/env.ts")
    return all((root / relative).is_file() for relative in essentials)


def describe(workspace) -> str:
    root = Path(workspace)
    if not is_scaffolded(root):
        return f"{root} is not scaffolded yet."
    counts = {
        "page objects": len(list((root / "src/pages").glob("*.ts"))) - 1,
        "components": len(list((root / "src/components").glob("*.ts"))) - 1,
        "specs": len(list((root / "tests").rglob("*.spec.ts"))),
    }
    return (f"{root}: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v >= 0))
