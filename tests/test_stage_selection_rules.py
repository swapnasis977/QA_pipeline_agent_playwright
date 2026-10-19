"""`--until`, `--only` and the review gate must mean what they say.

THE FAILURES, found by reading `Pipeline.run`:
  * `--until X` was checked only after a stage RAN. With X already done, the
    skip branch `continue`d past it and every later pending stage ran — spending
    tokens on a "just re-check the sheet" command.
  * `review_gate` was remembered as "done", so `--force testcase_writer` (which
    rebuilds the sheet with blank Status) let later stages proceed on cases no
    one had reviewed.
  * `--only` marked the run finished, so the next resume treated an interrupted
    run as new and archived its summary; a misspelled stage name ran nothing and
    exited 0.
  * the token cap was checked BEFORE a finished stage was marked done, so a stage
    that tipped the cap was paid for again on the next resume.

Run with:  python -m pytest tests/test_stage_selection_rules.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from qa_pipeline.orchestrator import budget  # noqa: E402
from qa_pipeline.orchestrator.runner import Pipeline, Stage  # noqa: E402


class _Ctx:
    def __init__(self, tmp, done=(), force=()):
        self.feature_dir = Path(tmp)
        self._done = set(done)
        self.force_stages = set(force)
        self.run_id = "r"
        self.state = {}
        self.max_run_tokens = 0
        self.profile = type("P", (), {"get": lambda _s, _k, d=None: d})()
        self.started = "1970-01-01T00:00:00Z"
        self.interactive = False
        self.finished_marked = False

    def starts_new_run(self):
        return False

    def mark_run_finished(self):
        self.finished_marked = True

    def stage_done(self, stage):
        return stage in self._done

    def should_run(self, stage):
        return stage in self.force_stages or stage not in self._done

    def mark_done(self, stage, summary=""):
        self._done.add(stage)

    def artifact(self, filename):
        return self.feature_dir / filename


def _graph(ran):
    def make(name, **kw):
        return Stage(name, name, lambda ctx, _n=name: ran.append(_n) or {}, **kw)
    return [make("a"), make("gate", always_run=True), make("c"), make("d")]


def test_until_stops_at_a_stage_that_is_already_done(tmp_path):
    ran = []
    Pipeline(_graph(ran)).run(_Ctx(tmp_path, done=["a", "gate", "c"]), until="c")
    assert ran == [] or "d" not in ran, f"ran past --until c: {ran}"


def test_the_gate_is_evaluated_even_when_it_was_approved_before(tmp_path):
    ran = []
    Pipeline(_graph(ran)).run(_Ctx(tmp_path, done=["a", "gate", "c", "d"]))
    assert ran == ["gate"]


def test_a_stage_that_is_done_and_not_always_run_is_still_skipped(tmp_path):
    ran = []
    Pipeline(_graph(ran)).run(_Ctx(tmp_path, done=["a", "c", "d"]))
    assert "a" not in ran and "c" not in ran


def test_a_misspelled_stage_name_is_an_error_not_a_green_no_op(tmp_path):
    with pytest.raises(ValueError, match="Unknown stage name"):
        Pipeline(_graph([])).run(_Ctx(tmp_path), only=["coding_agnt"])
    with pytest.raises(ValueError, match="Unknown stage name"):
        Pipeline(_graph([])).run(_Ctx(tmp_path), until="nope")


def test_only_does_not_close_the_run(tmp_path):
    ctx = _Ctx(tmp_path)
    Pipeline(_graph([])).run(ctx, only=["c"])
    assert ctx.finished_marked is False


def test_a_full_pass_still_closes_the_run(tmp_path):
    ctx = _Ctx(tmp_path)
    Pipeline(_graph([])).run(ctx)
    assert ctx.finished_marked is True


def test_a_stage_that_tips_the_budget_is_marked_done_first(tmp_path, monkeypatch):
    def over_budget(ctx, stage):
        raise budget.BudgetExceeded("cap")

    monkeypatch.setattr(budget, "check_tokens", over_budget)
    ctx = _Ctx(tmp_path)
    with pytest.raises(budget.BudgetExceeded):
        Pipeline(_graph([])).run(ctx)
    assert ctx.stage_done("a"), "a finished stage must not be paid for again on resume"


def test_forcing_a_stage_warns_about_downstream_stages_built_from_its_old_output(
        tmp_path, capsys):
    ctx = _Ctx(tmp_path, done=["a", "gate", "c", "d"], force=["a"])
    Pipeline(_graph([])).run(ctx, only=["a"])
    assert "still marked done" in capsys.readouterr().out
