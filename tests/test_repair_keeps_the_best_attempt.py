"""A repair loop must never return an artifact worse than one it already had.

`repair_loop.run` returned whatever the LAST attempt produced. When a later
attempt came back worse than an earlier one, the better artifact was silently
discarded — so retrying could leave the stage with less than not retrying at
all, which inverts the entire point of the loop.

Found by running the pipeline. Generating VENDOR-TC-001:

    attempt 1  -> 2 files rejected, 0 written
    attempt 2  -> spec ACCEPTED and written (warnings only)
    attempt 3  -> 1 file rejected, 0 written   <- this is what was returned

The run then failed at the Executor's "planned but produced no spec" guard,
holding nothing, while a compiling spec had existed one attempt earlier and
had been thrown away.

`coding_agent._repair_unverified_locators` already states this rule for its
own inner pass — "a repair must not make things worse: it is only kept if it
still compiles AND corroborates strictly more locators than the version
already on disk". It just was not applied at the stage level.

Run with:  python -m pytest tests/test_repair_keeps_the_best_attempt.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import repair_loop  # noqa: E402
from qa_pipeline.verifiers.result import VerifyResult  # noqa: E402


def _failed(*errors):
    return VerifyResult.failed("gate_code", list(errors),
                               repairs=[f"fix: {e}" for e in errors])


def _sequence(*results):
    """A `verify` that returns each result in turn, one per attempt."""
    box = list(results)

    def verify(_artifact):
        return box.pop(0)
    return verify


def test_a_worse_final_attempt_does_not_discard_a_better_earlier_one():
    """The observed run, in miniature."""
    artifacts = [{"files": []}, {"files": ["spec.ts"]}, {"files": []}]
    produced = list(artifacts)

    outcome = repair_loop.run(
        lambda _repair: produced.pop(0),
        _sequence(_failed("a", "b"), _failed("c"), _failed("d", "e", "f")),
        max_repairs=2, label="gate_code", verbose=False)

    # The middle attempt had the fewest problems — it is what survives.
    assert outcome.artifact == {"files": ["spec.ts"]}


def test_the_best_result_is_reported_alongside_the_best_artifact():
    produced = [{"n": 1}, {"n": 2}, {"n": 3}]
    outcome = repair_loop.run(
        lambda _repair: produced.pop(0),
        _sequence(_failed("a", "b", "c"), _failed("c"), _failed("d", "e")),
        max_repairs=2, label="gate_code", verbose=False)

    assert outcome.artifact == {"n": 2}
    assert len(outcome.result.errors) == 1


def test_a_passing_attempt_still_returns_immediately():
    """The happy path must not change: a clean gate ends the loop there."""
    produced = [{"n": 1}, {"n": 2}]
    outcome = repair_loop.run(
        lambda _repair: produced.pop(0),
        _sequence(_failed("a"), VerifyResult.passed("gate_code")),
        max_repairs=2, label="gate_code", verbose=False)

    assert outcome.ok is True
    assert outcome.artifact == {"n": 2}


def test_a_steadily_improving_run_keeps_the_last():
    """When the final attempt IS the best, nothing changes."""
    produced = [{"n": 1}, {"n": 2}, {"n": 3}]
    outcome = repair_loop.run(
        lambda _repair: produced.pop(0),
        _sequence(_failed("a", "b", "c"), _failed("a", "b"), _failed("a")),
        max_repairs=2, label="gate_code", verbose=False)

    assert outcome.artifact == {"n": 3}


def test_an_equal_attempt_does_not_displace_the_earlier_one():
    """No improvement means no reason to swap — and the earlier artifact is
    the one already proven to reach this score."""
    produced = [{"n": 1}, {"n": 2}]
    outcome = repair_loop.run(
        lambda _repair: produced.pop(0),
        _sequence(_failed("a"), _failed("b")),
        max_repairs=1, label="gate_code", verbose=False)

    assert outcome.artifact == {"n": 1}
