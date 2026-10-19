"""A generation that produced NO SPEC must fail its gate, not pass it.

`stage_code` verifies each generated file and combines the results. With zero
files there is nothing to verify, and `combine("gate_code", [])` returns PASS —
so the repair loop saw a clean gate, never engaged, and `stage_code` returned
`files: []` as a verified artifact. The failure surfaced one stage later, at
the Executor's "planned but produced no spec" guard, by which point the
generation context is gone and the only recourse is failing the whole run.

Found by running the pipeline. The Coding Agent tried to add a property to a
recorded page object; the read-only guard rejected that write; the spec still
referenced the property, failed `tsc`, and was rolled back with its chunk.
Two rejections, zero files, gate PASSED, no repair attempted — and every
reason it happened was already sitting in coding_agent_rejected_files.json,
unread. One informed retry was available and was never taken.

"Nothing was produced" and "everything produced was fine" must never be the
same verdict — the same confusion `executor.failure_kind` exists to prevent.

Run with:  python -m pytest tests/test_empty_generation_fails_the_gate.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.orchestrator.stages import empty_generation_result  # noqa: E402

_REJECTED = [
    {"path": "src/pages/recorded/RecordedSanity2Tc001VendorPage.ts",
     "reason": "'src/pages/recorded/' is generated from human recordings and is "
               "read-only to this agent — import from it instead."},
    {"path": "tests/e2e/vendor-registration.spec.ts",
     "reason": "syntax check failed: TS2339: Property 'connected' does not exist "
               "on type 'RecordedSanity2Tc001VendorPage'."},
]


def test_no_spec_for_planned_cases_is_a_failed_gate():
    result = empty_generation_result(["VENDOR-TC-001"], _REJECTED)
    assert result.ok is False


def test_the_repair_prompt_carries_the_actual_rejection_reasons():
    """The whole point: the retry must know WHY, or it re-issues the same
    call and earns the same rejection."""
    prompt = empty_generation_result(["VENDOR-TC-001"], _REJECTED).repair_prompt()

    assert "read-only" in prompt
    assert "TS2339" in prompt
    assert "recorded" in prompt
    # And it must name the case that went unserved.
    assert "VENDOR-TC-001" in prompt


def test_it_still_fails_when_nothing_was_even_rejected():
    """The model can return an empty `files` list with no rejections at all —
    it simply declined to write anything. Still a failed gate, and the prompt
    must still be actionable or the loop gives up with no diagnosis."""
    result = empty_generation_result(["VENDOR-TC-001"], [])
    assert result.ok is False
    assert result.repair_prompt()


def test_a_supporting_file_without_a_spec_is_still_empty():
    """A page object serves no case on its own. Producing only helpers means
    the planned case still has no test."""
    result = empty_generation_result(["VENDOR-TC-001"], _REJECTED)
    assert result.ok is False
