"""Stage execution: transient retry, resume, error logging, budget checks.

Ported from `run_pipeline._run_stage`, whose central judgment is kept intact
because it is correct: a TRANSIENT failure (rate limit, timeout, connection
reset) retries automatically with exponential backoff before ever bothering a
human, while a NON-transient failure (a bad prompt, a malformed response the
JSON repair could not fix, a real bug) asks rather than auto-retrying —
because auto-retrying those "would just burn tokens repeating the same
mistake".

Transient errors are matched by exception CLASS NAME rather than by
`isinstance` against imported types, and that is deliberate: `AI_PROVIDER`
decides which provider SDK is even installed at runtime, so importing every
provider's exception hierarchy here would require every provider's package
regardless of which one is active.

Two things are new relative to the original:

  - RESUME. Each stage records completion in the run manifest, so a run that
    dies at stage 8 restarts at stage 8. A forty-minute run that has to begin
    again from the Test Case Writer is a run people stop using in CI.
  - The retry prompt is skipped in non-interactive sessions. The original
    already did this (`sys.stdin.isatty()`); it is kept because CI is the
    main place this runs unattended.
"""

from __future__ import annotations

import os
import sys
import time

from agents.artifact_io import read_json, write_json
from utils import transient
from utils.llm_client import start_run_summary
from utils.timestamps import utc_iso

from . import budget, case_state
from .gates import DemonstrationRequired, ReviewRequired


#: The exceptions that are the pipeline WORKING, not failing: a budget
#: ceiling, the human review gate, a request for a demonstration recording.
#: They must travel straight out to the CLI, which reports each one with its
#: own exit code (4 / 2 / 3). Left to the generic handler below they were
#: logged to pipeline_errors.json as if something had broken, and then either
#: prompted the operator to "retry" a review gate that retrying cannot
#: satisfy, or — in CI — were reworded into StageFailed and exited 1, so the
#: distinct exit codes the CLI defines were unreachable.
def _refresh_case_state(ctx):
    """Rewrites pipeline_state.yml. A progress report must never fail a run."""
    try:
        case_state.refresh(ctx.feature_dir, ctx.repo_dir)
    except Exception as exc:  # noqa: BLE001 - bookkeeping only
        print(f"      (pipeline_state.yml not updated: {exc})")


class ModelUnavailable(Exception):
    """The configured model API cannot be used (missing/invalid key, exhausted
    quota, unreachable). Raised before any model-powered stage runs, so nothing
    is spent discovering it. Distinct exit code 5."""


#: Stages that call the model. Everything else (crawl, scaffold, execute, bug
#: report, feedback) runs fine without it.
MODEL_STAGES = frozenset({
    "doc_ingestor", "testcase_writer", "automation_planner", "coding_agent",
    "reviewer", "self_healer",
})

HALT_SIGNALS = (budget.BudgetExceeded, ReviewRequired, DemonstrationRequired, ModelUnavailable)

#: Failure modes that go away on their own with a short wait, as opposed to a
#: bad prompt or a malformed response, which retrying will not fix.
#:
#: Re-exported from utils/transient rather than defined twice. The list had to
#: agree with the per-call retry policy to be correct — a class the call site
#: retried but this one did not (or vice versa) means a fault is handled at one
#: altitude and not the other, which is exactly the inconsistency that made a
#: rate limit look like an unresolved test case.
TRANSIENT_MARKERS = transient.TRANSIENT_MARKERS
MAX_AUTO_RETRIES = 3
BASE_DELAY_SEC = 2


class StageFailed(Exception):
    """A stage failed and the operator chose not to retry (or could not be
    asked). Distinct from BudgetExceeded, which is a deliberate stop."""


def starts_fresh_pass(ctx, stages: list, only) -> bool:
    """Whether this invocation begins a NEW pass over the stage graph, as
    opposed to resuming one or re-running a single stage inside it.

    This is the per-run boundary for `summary.md` (see `Pipeline.run`), and
    the three cases have to stay distinct:

      - FRESH RUN. Every note in summary.md belongs to a PREVIOUS run and is
        re-sent to the Planner on every call until it is archived.
      - RESUME. The notes are this run's own. Archiving them would leave the
        remaining stages planning against "(no prior context yet)", which is
        worse than the unbounded growth the boundary exists to fix.
      - `--only`. A surgical re-run of named stages inside an existing run,
        never a new one.

    The run/resume question is answered by `RunContext.starts_new_run`, which
    reads the run block `mark_done`/`mark_run_finished` maintain in
    run_manifest.json — real bookkeeping rather than an inference from which
    stage happens to be pending. `stages` is no longer consulted and is kept
    only so callers (and the earlier signature) do not have to change.
    """
    if only:
        return False
    return bool(ctx.starts_new_run())


def is_transient(exc: Exception) -> bool:
    """Kept as a module-level name because callers and tests import it from
    here; the classification itself now lives in one place.

    NOTE this is now a BACKSTOP, not the first line of defence. `call_agent`
    retries transient faults per request, so an exception still transient by
    the time it reaches here has already survived that budget — meaning a real
    outage or an expired credential, not a blip.
    """
    return transient.is_transient(exc)


def log_error(ctx, stage: str, exc: Exception):
    """Appends to pipeline_errors.json.

    Uses `read_json`/`write_json` rather than `json.loads(read_text())`
    because this runs while ANOTHER error is being handled: a corrupt or
    truncated log from a previous run killed mid-write must not raise from in
    here, which would replace the real failure with a confusing one from the
    error logger itself.
    """
    path = ctx.artifact("pipeline_errors.json")
    errors = read_json(path, default=[])
    if not isinstance(errors, list):
        errors = []
    errors.append({"stage": stage, "error": str(exc),
                   "error_type": type(exc).__name__, "timestamp": utc_iso()})
    write_json(path, errors)


def run_stage(ctx, stage: str, fn, *args, **kwargs):
    """Runs one stage with the retry policy. Returns whatever `fn` returns."""
    auto_retries = 0

    while True:
        try:
            return fn(*args, **kwargs)

        except HALT_SIGNALS:
            raise

        except Exception as exc:
            log_error(ctx, stage, exc)

            if is_transient(exc) and auto_retries < MAX_AUTO_RETRIES:
                auto_retries += 1
                delay = BASE_DELAY_SEC * (2 ** (auto_retries - 1))
                print(f"\n[TRANSIENT] Stage '{stage}' failed "
                      f"({type(exc).__name__}: {exc}) — auto-retry "
                      f"{auto_retries}/{MAX_AUTO_RETRIES} in {delay}s.")
                time.sleep(delay)
                continue

            print(f"\n[ERROR] Stage '{stage}' failed: {type(exc).__name__}: {exc}")

            if not ctx.interactive or sys.stdin is None or not sys.stdin.isatty():
                raise StageFailed(
                    f"Stage '{stage}' failed and this session cannot prompt for a "
                    f"retry: {exc}") from exc

            try:
                choice = input(f"Retry '{stage}'? [r]etry / [a]bort: ").strip().lower()
            except EOFError:
                raise StageFailed(f"Stage '{stage}' failed: {exc}") from exc

            if choice != "r":
                raise StageFailed(f"Stage '{stage}' aborted by operator: {exc}") from exc

            # A human-approved retry gets a fresh auto-retry budget: they may
            # have just fixed the underlying problem in another window.
            auto_retries = 0


def _require_model(ctx, stage_name: str) -> None:
    """Checks the model API once per run, before the first stage that needs it.
    QA_SKIP_MODEL_PREFLIGHT=1 turns it off (offline tests, a flaky health probe)."""
    if os.environ.get("QA_SKIP_MODEL_PREFLIGHT") == "1":
        ctx.model_checked = True
        return
    from utils import llm_client
    ok, reason, kind = llm_client.preflight()
    if ok:
        ctx.model_checked = True
        return
    raise ModelUnavailable(
        f"The model API is not usable, so '{stage_name}' (and every other model-powered "
        f"stage) cannot run: {reason}\n"
        f"  Nothing was spent. Fix the provider (key, billing/quota, network) or switch "
        f"AI_PROVIDER in .env, then re-run the same command — it resumes from here. "
        f"Stages that need no model (explore, scaffold, execute, bug report) still work: "
        f"use --only for those.")


class Pipeline:
    """Runs an ordered list of stages against a context.

    A fixed, deterministic order — never a model deciding what to do next.
    Predictability is the feature: an operator can say what will happen, a
    resume knows where it stopped, and a failure is attributable to one named
    stage rather than to an emergent plan.
    """

    def __init__(self, stages: list):
        self.stages = list(stages)

    def _warn_if_downstream_is_stale(self, ctx, name, forced_here: bool):
        """`--force X` re-runs X only. Everything after it that is still marked
        done was built from X's OLD output; say so instead of letting a stale
        plan flow into a fresh run silently."""
        if not forced_here:
            return
        if not hasattr(ctx, "stage_done"):
            return
        forced = getattr(ctx, "force_stages", None) or ()
        names = [st.name for st in self.stages]
        later = [n for n in names[names.index(name) + 1:]
                 if ctx.stage_done(n) and n not in forced]
        if later:
            print(f"      ⚠️  '{name}' was re-run, but these later stage(s) are still marked "
                  f"done from its previous output: {', '.join(later)}. "
                  f"Pass --force for any that should be rebuilt.")

    def run(self, ctx, *, only=None, until=None) -> dict:
        total = len(self.stages)
        results = {}

        known = {st.name for st in self.stages}
        unknown = sorted((set(only or []) | ({until} if until else set())
                          | set(getattr(ctx, "force_stages", []) or [])) - known)
        if unknown:
            raise ValueError(f"Unknown stage name(s): {', '.join(unknown)}. "
                             f"Valid stages: {', '.join(st.name for st in self.stages)}.")

        # THE PER-RUN BOUNDARY FOR summary.md.
        #
        # `append_context_summary` only ever grows that file, and
        # `automation_planner.plan_cases` re-sends the whole of it as
        # `prior_context` on every planning call. Without a boundary the
        # second run against a feature re-sends the first run's notes, the
        # third re-sends both, and so on — one real feature directory had
        # reached 28KB of accumulated history, all of it paid for on every
        # single call. `start_run_summary` was written for exactly this and
        # had no caller; this is it.
        #
        # Archives rather than deletes, so the flat-file audit trail
        # survives. Deliberately here and not in `RunContext.__init__`: a
        # context is also constructed for a resume and for `--only`, and
        # neither of those starts a new run. See `starts_fresh_pass`.
        if starts_fresh_pass(ctx, self.stages, only):
            start_run_summary(ctx.feature_dir)

        for index, stage in enumerate(self.stages, start=1):
            name = stage.name

            if only and name not in only:
                # A stage `--only` filters OUT still has to hand its previous
                # result to the ones it filters in — same reason the resume
                # path below rehydrates, and the same failure when it does
                # not. `--only coding_agent` skipped straight past the
                # Planner, so the Coding Agent read an empty
                # `ctx.state["automation_planner"]`, reported "nothing
                # planned", wrote no files and exited 0: a green run that did
                # nothing at all. Reloading is disk-only and costs no model
                # call, so doing it for every filtered-out stage is free.
                if stage.applies(ctx):
                    restored = stage.rehydrate(ctx)
                    if restored is not None:
                        ctx.state[name] = restored
                        results[name] = restored
                continue

            if not stage.applies(ctx):
                if stage._skip_reason is not None and ctx.should_run(name):
                    print(f"      [skipped] {stage.title}: {stage._skip_reason(ctx)}")
                # `--until X` stops at X even when X does not apply; carrying on
                # ran every later stage (and paid for them).
                if until and name == until:
                    break
                continue

            if not ctx.should_run(name) and not stage.always_run:
                # A skipped stage must still put its result back into
                # ctx.state, or resume is worse than no resume at all: the
                # next stage reads an EMPTY upstream result and treats it as
                # "nothing to do". A run resumed at the Coding Agent found no
                # plans, generated nothing, executed nothing and reported
                # success — the failure mode this rehydration exists to kill.
                restored = stage.rehydrate(ctx)
                if restored is not None:
                    ctx.state[name] = restored
                    results[name] = restored
                note = "" if restored is not None or not stage.rehydrates else \
                    " — its output could not be reloaded; --force it if the next " \
                    "stage finds nothing"
                print(f"[{index}/{total}] {stage.title} — already done "
                      f"(pass --force {name} to re-run){note}")
                # `--until X` must stop at X even when X is already done;
                # `continue` here used to carry on and run every later stage.
                if until and name == until:
                    break
                continue

            print(f"[{index}/{total}] {stage.title}")
            if stage.description:
                for line in stage.description.strip().splitlines():
                    print(f"      {line.strip()}")

            if name in MODEL_STAGES and not getattr(ctx, "model_checked", False):
                _require_model(ctx, name)
            outcome = run_stage(ctx, name, stage.fn, ctx)
            ctx.state[name] = outcome
            results[name] = outcome

            summary = stage.summarize(outcome)
            ctx.mark_done(name, summary)
            _refresh_case_state(ctx)
            if summary:
                print(f"      -> {summary}")
            # AFTER mark_done: a stage that finished is finished. Checking the
            # budget first left a fully paid-for stage unmarked whenever it
            # tipped the cap, so the next resume paid for it again.
            budget.check_tokens(ctx, name)
            self._warn_if_downstream_is_stale(ctx, name, forced_here=name in (getattr(ctx, "force_stages", None) or ()))

            if stage.halts(ctx, outcome):
                print(f"      Stopping after '{name}' — nothing further to do.")
                break

            if until and name == until:
                break

        # The loop ran to its end (or halted deliberately), so this pass is
        # over. A stage that RAISES never reaches here, which is what leaves
        # an interrupted run resumable — and its summary.md intact. See
        # `RunContext.mark_run_finished`.
        # `--only` is a surgical re-run, not a pass over the pipeline: marking
        # the run finished made the NEXT resume treat the interrupted run as
        # new and archive its summary.
        if not only:
            ctx.mark_run_finished()
        _refresh_case_state(ctx)
        return results


class Stage:
    """One named step in the graph."""

    def __init__(self, name, title, fn, *, description="", applies=None,
                 summarize=None, halts=None, rehydrate=None, always_run=False,
                 skip_reason=None):
        self.name = name
        #: Why the stage was left out when `applies` said no. A stage that vanishes silently
        #: (a blank Jira project key) looks like a stage that ran and did nothing.
        self._skip_reason = skip_reason
        #: Re-evaluated on every run, never skipped as "done". For a gate whose
        #: answer depends on something that can change underneath it (the
        #: sheet a human reviews), so a past approval is not a standing one.
        self.always_run = always_run
        self.title = title
        self.fn = fn
        self.description = description
        self._applies = applies
        self._summarize = summarize
        self._halts = halts
        #: Rebuilds this stage's `ctx.state` entry from artifacts on disk, for
        #: a resumed run that skips it. A stage whose output nothing
        #: downstream reads does not need one.
        self._rehydrate = rehydrate

    def applies(self, ctx) -> bool:
        return self._applies(ctx) if self._applies else True

    def summarize(self, outcome) -> str:
        if self._summarize:
            try:
                return self._summarize(outcome) or ""
            except Exception:
                return ""
        return ""

    def halts(self, ctx, outcome) -> bool:
        return bool(self._halts(ctx, outcome)) if self._halts else False

    @property
    def rehydrates(self) -> bool:
        return self._rehydrate is not None

    def rehydrate(self, ctx):
        """This stage's previous result, reloaded from disk, or None.

        Never raises: a missing or corrupt artifact means "could not restore",
        which the caller reports — turning it into an exception would make a
        resume fail harder than a fresh run.
        """
        if not self._rehydrate:
            return None
        try:
            return self._rehydrate(ctx)
        except Exception as exc:
            print(f"      ⚠️  Could not reload '{self.name}' state "
                  f"({type(exc).__name__}: {exc}).")
            return None

    def __repr__(self):
        return f"<Stage {self.name}>"
