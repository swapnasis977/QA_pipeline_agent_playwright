"""A repair must be shown the output it is repairing.

`repair_prompt` tells the Coding Agent "the following problems were found in
your previous output; fix EXACTLY these" — and the user prompt contained only
the test cases, the planner doc and the DOM snapshots. THE PREVIOUS OUTPUT WAS
NEVER INCLUDED. So every repair attempt regenerated ~200 lines from scratch,
blind to what it had just written.

That is why repairs oscillated instead of converging. Observed on
VENDOR-TC-001:

    attempt 1  -> 13 problems
    repair 1   ->  5
    repair 2   -> 10      <- fixing the 5 broke something else
    repair 3   ->  9
    repair 4   ->  3 files written

Each attempt is an independent 200-line rewrite, so a fix in one place
reintroduces variance everywhere else. Showing the model its own previous file
turns a rewrite into an edit, which is the whole point of the "don't regenerate
the wheel" principle.

Run with:  python -m pytest tests/test_repair_sees_prior_output.py -q
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import coding_agent  # noqa: E402

_SPEC = "tests/e2e/vendor-registration.spec.ts"
_POM = "src/pages/VendorRegistrationPage.ts"
_PRIOR = {
    _SPEC: "import { test } from '@fixtures/index';\n// 200 lines of spec\n",
    _POM: "export class VendorRegistrationPage {}\n",
    "tests/e2e/unrelated.spec.ts": "// belongs to a different case\n",
}


def _cases():
    return [{"Test Case ID": "VENDOR-TC-001"}]


# --- the seam exists ------------------------------------------------------

def test_run_accepts_prior_files():
    assert "prior_files" in inspect.signature(coding_agent.run).parameters


def test_the_adapter_forwards_it():
    from qa_pipeline.agents import adapters
    assert "prior_files" in inspect.signature(adapters.generate_code).parameters


# --- what the block contains ---------------------------------------------

def test_the_previous_content_is_included():
    block = coding_agent._prior_output_block(_PRIOR, _cases(), _SPEC)
    assert "200 lines of spec" in block
    assert _SPEC in block


def test_supporting_files_for_the_same_target_are_included():
    """A spec and the page object it imports were generated together and only
    make sense together — repairing one blind to the other is how a fix in the
    spec breaks its page object."""
    block = coding_agent._prior_output_block(_PRIOR, _cases(), _SPEC)
    assert "VendorRegistrationPage" in block


def test_another_cases_file_is_not_dragged_in():
    """The chunk is repaired, not the repo. Unrelated files would dilute the
    instruction and cost tokens for context this call cannot act on."""
    block = coding_agent._prior_output_block(_PRIOR, _cases(), _SPEC)
    assert "unrelated" not in block


def test_no_prior_output_yields_nothing():
    """First attempt: there is nothing to edit, so the prompt must be exactly
    what it always was."""
    assert coding_agent._prior_output_block({}, _cases(), _SPEC) == ""
    assert coding_agent._prior_output_block(None, _cases(), _SPEC) == ""


def test_it_tells_the_model_to_edit_rather_than_rewrite():
    """Without this the model treats the content as reference material and
    regenerates anyway — which is the behaviour being fixed."""
    block = coding_agent._prior_output_block(_PRIOR, _cases(), _SPEC).lower()
    assert "previous" in block
    assert "unchanged" in block or "only" in block
