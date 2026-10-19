"""L4 — control. Stage graph, retries, resume, budgets, gates.

This replaced `run_pipeline.py`, which has been retired — there is one
pipeline and one entry point now. The retry policy, the review gate and the
token ceiling here are ports of that file's, because its judgments were
right: transient failures auto-retry with backoff while non-transient ones
ask, a blank spreadsheet row is not a test case, and a header-only sheet
halts rather than sailing through the gate.

    from qa_pipeline import profiles
    from qa_pipeline.orchestrator import RunContext, build_pipeline

    profile = profiles.load("myapp")
    profile.apply_environment()
    ctx = RunContext(profile, story_key="PROJ-1234", epic_key="PROJ-1000")
    build_pipeline().run(ctx)
"""

from . import budget, gates, stages  # noqa: F401
from .context import RunContext, RunMetrics  # noqa: F401
from .gates import DemonstrationRequired, ReviewRequired  # noqa: F401
from .runner import Pipeline, Stage, StageFailed, run_stage  # noqa: F401

__all__ = ["RunContext", "RunMetrics", "Pipeline", "Stage", "StageFailed",
           "run_stage", "budget", "gates", "stages", "build_pipeline",
           "ReviewRequired", "DemonstrationRequired"]


def build_pipeline() -> Pipeline:
    """The thirteen stages, in order."""
    return Pipeline(stages.build())
