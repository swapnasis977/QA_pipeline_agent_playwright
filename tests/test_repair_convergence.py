"""The repair loop must stop when a repair stops making progress.

WHY. The Coding Agent once spent 77,438 tokens across three generation cycles
on a single test case, had every attempt rejected for the same reason, and
wrote no file at all. The second attempt already carried the information that
the third was pointless: it came back with exactly the errors it had just been
asked to fix. Paying for a third identical call is the definition of waste.

Run with:  python -m pytest tests/test_repair_convergence.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import repair_loop  # noqa: E402
from qa_pipeline.verifiers.result import VerifyResult  # noqa: E402


def _failure(*errors):
    return VerifyResult.failed("test_gate", list(errors),
                               repairs=[f"fix: {e}" for e in errors])


def _pass():
    """A real passing result. NOTE: returning None from `verify` is not the
    same thing — `RepairOutcome.ok` requires an actual result object, so None
    means "nothing was verified", not "it passed"."""
    return VerifyResult.passed("test_gate")


def test_stops_when_a_repair_reproduces_the_same_errors():
    """The 77k-token scenario, in miniature."""
    calls = []

    def produce(repair):
        calls.append(repair)
        return {"content": "same every time"}

    def verify(_artifact):
        return _failure("locator 'Register' does not exist on Review")

    outcome = repair_loop.run(produce, verify, max_repairs=5,
                              label="coding_agent", verbose=False)

    assert not outcome.ok
    assert len(calls) == 2, (
        f"expected the loop to stop after the first unchanged repair, but it "
        f"called produce {len(calls)} times")


def test_keeps_going_while_the_errors_are_actually_changing():
    """Different errors mean the repair IS landing — that must not be cut off.

    Stopping on any failure would make the loop useless; the signal is
    specifically 'the same findings came back', not 'it failed again'.
    """
    responses = [
        _failure("error A"),
        _failure("error B"),
        _failure("error C"),
        _pass(),  # verified on the 4th attempt
    ]
    calls = []

    def produce(repair):
        calls.append(repair)
        return {"content": f"attempt {len(calls)}"}

    def verify(_artifact):
        return responses[len(calls) - 1]

    outcome = repair_loop.run(produce, verify, max_repairs=5,
                              label="coding_agent", verbose=False)

    assert outcome.ok
    assert len(calls) == 4


def test_error_order_does_not_defeat_the_convergence_check():
    """Gates aggregate from several checks, so error order is not stable.

    The same two problems reported in a different order is still no progress.
    """
    responses = [
        _failure("error A", "error B"),
        _failure("error B", "error A"),
    ]
    calls = []

    def produce(repair):
        calls.append(repair)
        return {"content": "x"}

    def verify(_artifact):
        return responses[min(len(calls) - 1, len(responses) - 1)]

    outcome = repair_loop.run(produce, verify, max_repairs=5,
                              label="planner", verbose=False)

    assert not outcome.ok
    assert len(calls) == 2, "reordered identical errors are still no progress"


def test_a_first_attempt_that_passes_never_repairs():
    calls = []

    def produce(repair):
        calls.append(repair)
        return {"content": "good"}

    outcome = repair_loop.run(produce, lambda _a: _pass(), max_repairs=3,
                              label="stage", verbose=False)

    assert outcome.ok
    assert len(calls) == 1
    assert calls[0] is None, "the first attempt is not a repair"
