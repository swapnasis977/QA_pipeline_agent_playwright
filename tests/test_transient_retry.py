"""A transient fault must be retried where it happens, not where the run is.

WHY. Retry existed only around whole STAGES, and that altitude produced two
opposite failures for the same two-second rate limit:

  - The Coding Agent catches every chunk exception and logs those cases as
    `unresolved`, so the stage retry never saw the fault at all. The cases
    silently lost their code and the run reported success.
  - The Automation Planner did not catch, so the stage retry re-planned and
    re-billed every batch that had already succeeded.

These tests pin the per-call policy that fixes both, and the isolation that
keeps one bad planning batch from discarding the good ones.

Run with:  python -m pytest tests/test_transient_retry.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import automation_planner  # noqa: E402
from utils import transient  # noqa: E402


class RateLimitError(Exception):
    """Named to match the real SDK class — classification is by class NAME,
    deliberately, so an exception whose MESSAGE quotes model output that
    happens to mention a rate limit is not retried. See utils/transient."""


class ValidationError(Exception):
    """A content failure. Waiting does not improve it."""


# --------------------------------------------------------------------------
# call_with_retry
# --------------------------------------------------------------------------

def test_retries_a_transient_fault_and_returns_the_eventual_success():
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError("429")
        return "generated"

    slept = []
    result = transient.call_with_retry(
        flaky, label="test", max_retries=4, base_delay=1, sleep=slept.append)

    assert result == "generated"
    assert len(attempts) == 3
    assert len(slept) == 2


def test_does_not_retry_a_content_failure():
    """The distinction that makes the helper safe to put on EVERY call. A
    retry wrapper that caught `Exception` would turn every real bug into a
    slow real bug and multiply the bill by max_retries."""
    attempts = []

    def broken():
        attempts.append(1)
        raise ValidationError("expected a JSON array, got str")

    with pytest.raises(ValidationError):
        transient.call_with_retry(broken, label="test", max_retries=4,
                                  base_delay=1, sleep=lambda _: None)

    assert len(attempts) == 1, "a content failure must fail on the first attempt"


def test_gives_up_after_max_retries_and_reraises_the_original():
    """The original exception TYPE must survive, because the stage-level
    policy in orchestrator.runner classifies on it to decide whether to
    auto-retry or ask the operator."""
    attempts = []

    def always_limited():
        attempts.append(1)
        raise RateLimitError("429")

    with pytest.raises(RateLimitError):
        transient.call_with_retry(always_limited, label="test", max_retries=2,
                                  base_delay=1, sleep=lambda _: None)

    assert len(attempts) == 3, "1 initial attempt + 2 retries"


def test_backoff_is_jittered_within_an_exponential_window():
    """Full jitter, not a fixed delay. The Coding Agent fires several chunk
    calls concurrently on ONE key, so a rate limit hits them together — a
    fixed delay marches all of them back into the provider in the same
    instant that earned the 429."""
    slept = []

    def always_limited():
        raise RateLimitError("429")

    with pytest.raises(RateLimitError):
        transient.call_with_retry(always_limited, label="test", max_retries=3,
                                  base_delay=2, sleep=slept.append)

    assert len(slept) == 3
    for attempt, delay in enumerate(slept):
        assert 0 <= delay <= 2 * (2 ** attempt)
    assert len(set(slept)) > 1, "identical delays every time means no jitter"


def test_the_orchestrator_and_the_call_site_agree_on_what_is_transient():
    """One definition, two altitudes. A class retried at one and not the
    other means a fault is handled inconsistently depending on which agent
    raised it — which is how a rate limit came to look like an unresolved
    test case in the first place."""
    from qa_pipeline.orchestrator import runner

    assert runner.TRANSIENT_MARKERS is transient.TRANSIENT_MARKERS
    assert runner.is_transient(RateLimitError("429")) is True
    assert runner.is_transient(ValidationError("bad json")) is False


# --------------------------------------------------------------------------
# Planner batch isolation
# --------------------------------------------------------------------------

def _cases(n):
    return [{"Test Case ID": f"TC-{i:03d}", "Name": f"case {i}"} for i in range(1, n + 1)]


def test_one_failing_batch_does_not_discard_the_batches_that_succeeded(monkeypatch, tmp_path):
    """The whole point of isolating a batch. Previously batch 2 raising threw
    away batch 1's completed, already-paid-for plans and sent run_stage off to
    re-plan everything."""
    cases = _cases(20)  # > _BATCH_SIZE, so this splits into 2 batches

    def fake_call_agent(*, user_prompt, **kwargs):
        if "TC-016" in user_prompt:
            raise RateLimitError("429")
        return "[]"

    def fake_parse(raw, feature_dir, agent_name):
        return [{"test_case_id": "TC-001"}]

    monkeypatch.setattr(automation_planner, "call_agent", fake_call_agent)
    monkeypatch.setattr(automation_planner, "parse_json_response", fake_parse)

    plans = automation_planner.plan_cases(cases, "", tmp_path, prior_context="")

    assert plans, "the surviving batch's plans must be kept, not discarded"


def test_every_batch_failing_raises_so_the_stage_policy_can_act(monkeypatch, tmp_path):
    """An outage is not a coverage gap. Returning [] here would hand the
    repair pass an impossible job and report a dead API key as forty
    individually unplanned cases."""
    def always_limited(**kwargs):
        raise RateLimitError("429")

    monkeypatch.setattr(automation_planner, "call_agent", always_limited)

    with pytest.raises(RateLimitError):
        automation_planner.plan_cases(_cases(20), "", tmp_path, prior_context="")


def test_parallel_batches_still_come_back_in_input_order(monkeypatch, tmp_path):
    """Order is load-bearing: the Coding Agent groups by target_file, and a
    reshuffled plan list changes how cases group into generation calls."""
    cases = _cases(45)  # 3 batches at _BATCH_SIZE = 15

    def fake_call_agent(*, user_prompt, **kwargs):
        return user_prompt

    def fake_parse(raw, feature_dir, agent_name):
        # Echo back the ids this batch was actually asked about, so the
        # concatenated result is checkable against the input order.
        return [{"test_case_id": c["Test Case ID"]}
                for c in cases if f'"{c["Test Case ID"]}"' in raw]

    monkeypatch.setattr(automation_planner, "call_agent", fake_call_agent)
    monkeypatch.setattr(automation_planner, "parse_json_response", fake_parse)

    plans = automation_planner.plan_cases(cases, "", tmp_path, prior_context="")

    assert [p["test_case_id"] for p in plans] == [c["Test Case ID"] for c in cases]
