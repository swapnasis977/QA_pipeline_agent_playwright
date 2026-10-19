"""The Coding Agent must never write into `src/pages/recorded/`.

That directory is not shared territory. `utils.recording_parser.generate` owns
it: every file in it is derived deterministically from a human `playwright
codegen` recording, which `qa_pipeline/orchestrator/stages.py` calls "the
highest-confidence source in the system — a person demonstrated the flow, so
the locators are ground truth by construction rather than by inference".

Found by running the pipeline. Generating VENDOR-TC-001 emitted
`src/pages/recorded/RecordedSampleIdentManagePage.ts` as one of its files and
overwrote it with a version holding only what THAT case needed:

    from the recording : 104 locator properties
    left on disk       :  11

93 human-demonstrated locators destroyed in one write, and 36 `tsc` errors in
other specs that referenced them. The model is not doing anything unreasonable
— it was asked for a page object and it wrote a complete one. The rule it
cannot infer is that this particular file has another author.

Enforced in code, not only in the skill, for the same reason path confinement
is: an instruction is not enforcement.

Run with:  python -m pytest tests/test_recorded_poms_are_read_only.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from agents.coding_agent import is_recording_owned  # noqa: E402


@pytest.mark.parametrize("path", [
    "src/pages/recorded/RecordedSampleIdentManagePage.ts",
    "src/pages/recorded/RecordedRegisterTc001Page.ts",
    "src/pages/recorded/anything.ts",
    # The model's own spelling before normalization.
    "src\\pages\\recorded\\RecordedRegisterTc001Page.ts",
    "./src/pages/recorded/RecordedRegisterTc001Page.ts",
])
def test_recorded_page_objects_are_owned_by_the_recording_parser(path):
    assert is_recording_owned(path) is True


@pytest.mark.parametrize("path", [
    "src/pages/RegistrationPage.ts",
    "src/pages/RecordedLookalikePage.ts",     # name alone means nothing
    "tests/e2e/vendor-registration.spec.ts",
    "src/components/DataGrid.ts",
    "src/utils/waits.ts",
    "src/pages/recordings/Foo.ts",            # a different directory
    "",
])
def test_everything_else_stays_writable(path):
    assert is_recording_owned(path) is False


def test_the_guard_matches_where_the_parser_actually_writes():
    """The two must not drift: `recording_parser.generate` builds its output
    directory as repo_dir / "src" / "pages" / "recorded"."""
    from agents.coding_agent import _RECORDING_OWNED_DIR

    source = (_ROOT / "utils" / "recording_parser.py").read_text(encoding="utf-8")
    assert 'repo_dir / "src" / "pages" / "recorded"' in source
    assert _RECORDING_OWNED_DIR == "src/pages/recorded/"
