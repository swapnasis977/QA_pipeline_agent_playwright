"""`summary.md` must have a per-run boundary — and the pipeline must be able
to tell a run from a resume in order to draw one.

WHY IT MATTERS. `append_context_summary` only ever grows that file, and
`automation_planner.plan_cases` re-sends the whole of it as `prior_context` on
EVERY planning call of every run. `utils.llm_client.start_run_summary` was
written to archive it and start fresh — and nothing ever called it. One real
feature directory's summary.md had reached 28KB of accumulated history, all of
it re-sent, every run, forever.

WHY IT WAS HARD. The feature directory doubled as the run, so "resume an
interrupted run" and "start a new run over the same feature" were
indistinguishable: both are the operator typing the same command again against
a manifest with the same completed stages. Rotating on a resume is worse than
not rotating at all — the run archives its own notes halfway through and every
later stage plans against "(no prior context yet)".

So the manifest now records the run itself: `mark_done` claims it for the
current run and marks it unfinished, `mark_run_finished` closes it when a pass
reaches the end of the graph. A stage that raises never closes it, which is
exactly what keeps an interrupted run resumable.

Run with:  python -m pytest tests/test_run_summary_rotation.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from qa_pipeline.orchestrator.runner import (  # noqa: E402
    Pipeline,
    Stage,
    StageFailed,
    starts_fresh_pass,
)
from utils.llm_client import (  # noqa: E402
    append_context_summary,
    read_context_summary,
    start_run_summary,
)


class _Ctx:
    """The surface `Pipeline.run` and `starts_fresh_pass` actually touch.

    Deliberately not a real RunContext: that opens a ledger and a profile,
    neither of which has anything to do with the run boundary.
    """

    def __init__(self, feature_dir, done=(), run=None):
        self.feature_dir = Path(feature_dir)
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        self._done = set(done)
        self._run = run
        self.run_id = "test-run-1"
        self.state = {}
        self.max_run_tokens = 0  # explicitly uncapped: skips the budget read
        self.profile = type("P", (), {"get": lambda _s, _k, d=None: d})()
        self.started = "1970-01-01T00:00:00Z"
        self.interactive = False

    # -- the bits under test ------------------------------------------------

    def starts_new_run(self):
        if not self._done:
            return True
        if not isinstance(self._run, dict):
            return True          # legacy manifest: unattributable history
        return bool(self._run.get("finished"))

    def mark_run_finished(self):
        if self._done:
            self._run = {"id": self.run_id, "finished": True}

    def should_run(self, stage):
        return stage not in self._done

    def mark_done(self, stage, summary=""):
        self._done.add(stage)
        self._run = {"id": self.run_id, "finished": False}

    def artifact(self, filename):
        return self.feature_dir / filename


_GRAPH = [Stage("doc_ingestor", "Ingest", lambda ctx: {}),
          Stage("automation_planner", "Plan", lambda ctx: {})]


# --- when the boundary fires ----------------------------------------------

def test_a_first_ever_run_is_fresh(tmp_path):
    assert starts_fresh_pass(_Ctx(tmp_path), _GRAPH, only=None) is True


def test_a_run_after_a_finished_one_is_fresh(tmp_path):
    """The case the old first-stage heuristic got wrong: every stage is
    complete, so it looked like a resume forever and never rotated."""
    ctx = _Ctx(tmp_path, done=["doc_ingestor", "automation_planner"],
               run={"id": "older", "finished": True})
    assert starts_fresh_pass(ctx, _GRAPH, only=None) is True


def test_an_interrupted_run_is_a_resume(tmp_path):
    ctx = _Ctx(tmp_path, done=["doc_ingestor"], run={"id": "older", "finished": False})
    assert starts_fresh_pass(ctx, _GRAPH, only=None) is False


def test_a_legacy_manifest_counts_as_history(tmp_path):
    """No run block at all: those completions cannot be attributed to a live
    run, so they are treated as a previous one."""
    ctx = _Ctx(tmp_path, done=["doc_ingestor", "automation_planner"], run=None)
    assert starts_fresh_pass(ctx, _GRAPH, only=None) is True


def test_only_never_rotates(tmp_path):
    """`--only coding_agent` is surgery inside an existing run."""
    assert starts_fresh_pass(_Ctx(tmp_path), _GRAPH, only={"coding_agent"}) is False


# --- what rotating actually does ------------------------------------------

def test_rotation_archives_the_previous_run_and_starts_empty(tmp_path):
    append_context_summary(tmp_path, "automation_planner", "run 1 planned 40 cases")
    assert "run 1 planned 40 cases" in read_context_summary(tmp_path)

    start_run_summary(tmp_path)

    assert read_context_summary(tmp_path) == "(no prior context yet)"
    # Archived, not deleted: the flat file store is a deliberate audit trail.
    archived = list((tmp_path / "_internal").glob("summary.*.md"))
    assert len(archived) == 1
    assert "run 1 planned 40 cases" in archived[0].read_text(encoding="utf-8")


def test_rotation_on_a_first_ever_run_is_a_no_op(tmp_path):
    start_run_summary(tmp_path)
    assert list((tmp_path / "_internal").glob("summary*.md")) == []


# --- the wiring, driven through the real Pipeline -------------------------
#
# A correct predicate that never gets called is the exact failure
# `start_run_summary` already had for its whole life.

def test_pipeline_run_rotates_on_a_fresh_run(tmp_path):
    append_context_summary(tmp_path, "automation_planner", "LAST run's notes")

    Pipeline(_GRAPH).run(_Ctx(tmp_path))

    assert read_context_summary(tmp_path) == "(no prior context yet)"
    assert len(list((tmp_path / "_internal").glob("summary.*.md"))) == 1


def test_pipeline_run_leaves_a_resumed_runs_notes_alone(tmp_path):
    """The regression that would hurt most."""
    append_context_summary(tmp_path, "doc_ingestor", "THIS run's notes")

    ctx = _Ctx(tmp_path, done=["doc_ingestor"], run={"id": "older", "finished": False})
    Pipeline(_GRAPH).run(ctx)

    assert "THIS run's notes" in read_context_summary(tmp_path)
    assert list((tmp_path / "_internal").glob("summary.*.md")) == []


def test_a_completed_pass_closes_the_run(tmp_path):
    ctx = _Ctx(tmp_path)
    Pipeline(_GRAPH).run(ctx)
    assert ctx.starts_new_run() is True  # the next invocation is a new run


def test_a_raising_stage_leaves_the_run_open(tmp_path):
    """An interrupted run must stay resumable — and keep its context."""
    def explode(_ctx):
        raise RuntimeError("stage blew up")

    graph = [Stage("doc_ingestor", "Ingest", lambda ctx: {}),
             Stage("automation_planner", "Plan", explode)]
    ctx = _Ctx(tmp_path)

    # Non-interactive, so `run_stage` cannot offer a retry and re-raises as
    # StageFailed — which is what the CLI turns into exit code 1.
    with pytest.raises(StageFailed, match="stage blew up"):
        Pipeline(graph).run(ctx)

    assert ctx.starts_new_run() is False  # the next invocation resumes


# --- the same contract, against the REAL RunContext bookkeeping -----------

def test_the_real_manifest_records_and_closes_a_run(tmp_path, monkeypatch):
    """The fake above must not drift from what RunContext actually writes."""
    from qa_pipeline.orchestrator import context as context_mod

    ctx = object.__new__(context_mod.RunContext)
    ctx.feature_dir = tmp_path
    ctx.run_id = "run-abc"

    assert ctx.starts_new_run() is True          # nothing recorded yet

    ctx.mark_done("doc_ingestor", "2 cards")
    manifest = json.loads((tmp_path / "_internal" / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run"] == {"id": "run-abc", "finished": False}
    assert ctx.starts_new_run() is False         # mid-run -> a resume

    ctx.mark_run_finished()
    manifest = json.loads((tmp_path / "_internal" / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["run"]["finished"] is True
    assert ctx.starts_new_run() is True          # closed -> next one is new


def test_finishing_a_run_that_did_nothing_is_a_no_op(tmp_path):
    from qa_pipeline.orchestrator import context as context_mod

    ctx = object.__new__(context_mod.RunContext)
    ctx.feature_dir = tmp_path
    ctx.run_id = "run-abc"

    ctx.mark_run_finished()
    assert not (tmp_path / "_internal" / "run_manifest.json").exists()
