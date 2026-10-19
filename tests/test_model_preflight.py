"""A dead model API must be found in a second, not after fifteen minutes.

THE FAILURE. A run crawled, planned and generated, and only when a chunk finally
called the model did it learn the workspace's usage limit was exhausted — then
spent each stage's whole retry budget confirming it. The Agent 3 stage even
"repaired successfully" a run whose repair calls had all been refused.

Run with:  python -m pytest tests/test_model_preflight.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from qa_pipeline.orchestrator.runner import ModelUnavailable, Pipeline, Stage  # noqa: E402
from utils import llm_client  # noqa: E402


class _Ctx:
    def __init__(self, tmp):
        self.feature_dir = Path(tmp)
        self._done = set()
        self.force_stages = set()
        self.state = {}
        self.max_run_tokens = 0
        self.profile = type("P", (), {"get": lambda _s, _k, d=None: d})()
        self.started = "1970-01-01T00:00:00Z"
        self.run_id = "r"
        self.interactive = False

    def starts_new_run(self):
        return False

    def mark_run_finished(self):
        pass

    def stage_done(self, stage):
        return stage in self._done

    def should_run(self, stage):
        return stage not in self._done

    def mark_done(self, stage, summary=""):
        self._done.add(stage)

    def artifact(self, name):
        return self.feature_dir / name


@pytest.fixture
def real_preflight(monkeypatch):
    monkeypatch.delenv("QA_SKIP_MODEL_PREFLIGHT", raising=False)


def _graph(ran):
    def make(name):
        return Stage(name, name, lambda ctx, _n=name: ran.append(_n) or {})
    return [make("explore_like"), make("coding_agent"), make("reviewer")]


def test_a_dead_model_stops_the_run_before_the_stage_that_needs_it(
        tmp_path, monkeypatch, real_preflight):
    monkeypatch.setattr(llm_client, "preflight",
                        lambda: (False, "anthropic (m): usage limit reached", "infra"))
    ran = []
    with pytest.raises(ModelUnavailable, match="Nothing was spent"):
        Pipeline(_graph(ran)).run(_Ctx(tmp_path))
    assert ran == ["explore_like"], "stages that need no model still ran; the model one did not"


def test_the_reason_reaches_the_operator(tmp_path, monkeypatch, real_preflight):
    monkeypatch.setattr(llm_client, "preflight", lambda: (False, "ANTHROPIC_API_KEY is not set", "infra"))
    with pytest.raises(ModelUnavailable, match="ANTHROPIC_API_KEY is not set"):
        Pipeline(_graph([])).run(_Ctx(tmp_path))


def test_a_working_model_is_checked_once_not_per_stage(tmp_path, monkeypatch, real_preflight):
    calls = []
    monkeypatch.setattr(llm_client, "preflight", lambda: calls.append(1) or (True, "", ""))
    ran = []
    Pipeline(_graph(ran)).run(_Ctx(tmp_path))
    assert ran == ["explore_like", "coding_agent", "reviewer"] and len(calls) == 1


def test_a_run_of_model_free_stages_never_probes_the_api(tmp_path, monkeypatch, real_preflight):
    def boom():
        raise AssertionError("probed the API for a run that needs no model")
    monkeypatch.setattr(llm_client, "preflight", boom)
    ran = []
    Pipeline([Stage("executor", "x", lambda ctx: ran.append(1) or {})]).run(_Ctx(tmp_path))
    assert ran == [1]


def test_the_probe_itself_reports_a_missing_key(monkeypatch):
    monkeypatch.setattr(llm_client, "AI_PROVIDER", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    ok, reason, kind = llm_client.preflight()
    assert ok is False and "ANTHROPIC_API_KEY" in reason and kind in ("infra", "content")


def test_an_unknown_provider_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(llm_client, "AI_PROVIDER", "nonsense")
    ok, reason, _kind = llm_client.preflight()
    assert ok is False and "nonsense" in reason
