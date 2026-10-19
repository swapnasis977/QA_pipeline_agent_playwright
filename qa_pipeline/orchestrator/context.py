"""RunContext — everything one run needs, assembled once and passed down.

The old pipeline threaded a dozen positional arguments through every stage
(`repo_output_dir`, `feature_dir`, `workers`, `shard`, `include_manual`,
`app_design_doc_path`, ...), which is how `_run_stage` ended up needing
positional-only parameters to stop a caller's `feature_dir=` keyword
colliding with its own. One object removes that whole class of problem: a
stage takes `ctx` and reads what it needs.

It also gives the run a single place to record what happened, which is what
`metrics` is for. The four numbers the design calls for — first-run pass
rate, locator verification rate, escalation rung reached, human-edit rate —
are only computable if something counts them as the run goes. Nothing else in
the pipeline is positioned to.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.artifact_io import read_json, write_json
from qa_pipeline import CONTRACT_VERSION
from qa_pipeline.brain import open_ledger
from utils.run_layout import artifact_path
from utils.timestamps import utc_iso


def _meta_blocks(meta, marker: str, _depth: int = 0):
    """Every counter block inside a (possibly nested) VerifyResult meta.

    A gate's meta is one level deep; a COMBINED gate's is two, and the
    orchestrator combines twice — once per file inside `gate_code`, then once
    over the whole batch — so the counters end up at depth three. The old
    single-level unwrap looked one level down, found `{"schema_check": {...},
    "locator_check": {...}}`, asked it for "total", got 0, and reported every
    run as having verified zero locators and performed zero crawls.

    Yields only blocks that actually carry `marker`, and does not descend into
    one once it matches, so nothing is counted twice.
    """
    if not isinstance(meta, dict) or _depth > 6:
        return
    if marker in meta:
        yield meta
        return
    for value in meta.values():
        yield from _meta_blocks(value, marker, _depth + 1)


class RunMetrics:
    """The health numbers, accumulated across a run.

    Recorded per stage rather than derived afterward because the interesting
    distinctions are invisible in the final artifacts. "Passed on the first
    attempt" and "passed after two repairs" produce byte-identical output;
    only the loop that ran them knows which happened, and that difference is
    exactly what separates a pipeline that is generating well from one that is
    repairing its way to adequacy.
    """

    def __init__(self):
        self.data = {
            "cases_planned": 0,
            "files_generated": 0,
            "first_attempt_ok": 0,
            "repaired": 0,
            "declined": 0,
            "locators_total": 0,
            "locators_grounded": 0,
            "locators_crawled": 0,
            "locators_failed": 0,
            "crawls": 0,
            "escalation_rung": {},   # rung -> count; see the design's ladder
            "gate_failures": {},     # verifier name -> count
            "cases_passed_first_run": 0,
            "cases_healed": 0,
            "cases_still_failing": 0,
        }

    def bump(self, key: str, amount: int = 1):
        if key in self.data and isinstance(self.data[key], int):
            self.data[key] += amount

    def rung(self, level: int):
        """Records how far up the escalation ladder a resolution had to go.
        1 = ledger hit, 2 = crawl, 3 = near-miss repair, 4 = demonstration
        needed, 5 = environment blocker."""
        key = str(level)
        self.data["escalation_rung"][key] = self.data["escalation_rung"].get(key, 0) + 1

    def gate_failed(self, verifier: str):
        self.data["gate_failures"][verifier] = self.data["gate_failures"].get(verifier, 0) + 1

    def absorb_verify(self, result):
        """Folds a VerifyResult's locator counts into the run totals."""
        if result is None:
            return
        for source in _meta_blocks(result.meta, "total"):
            self.data["locators_total"] += int(source.get("total", 0) or 0)
            self.data["locators_grounded"] += int(source.get("grounded", 0) or 0)
            self.data["locators_crawled"] += int(source.get("crawled", 0) or 0)
            self.data["locators_failed"] += int(source.get("failed", 0) or 0)
        if result.status == "fail":
            self.gate_failed(result.name)

    def rates(self) -> dict:
        d = self.data
        total_locators = d["locators_total"] or 1
        attempted = (d["first_attempt_ok"] + d["repaired"] + d["declined"]) or 1
        return {
            "locator_verification_rate": round(
                (d["locators_grounded"] + d["locators_crawled"]) / total_locators, 3),
            "first_attempt_rate": round(d["first_attempt_ok"] / attempted, 3),
            "decline_rate": round(d["declined"] / attempted, 3),
            "repair_rate": round(d["repaired"] / attempted, 3),
        }

    def snapshot(self) -> dict:
        return {**self.data, "rates": self.rates()}


class RunContext:
    """One invocation of the pipeline against one story."""

    def __init__(self, profile, story_key: str, epic_key: str, *,
                 figma_url: str = "", api_spec: str = "", cycle_id: str = "",
                 include_manual: bool = False,
                 skip_execution: bool = False, allow_crawl: bool = None,
                 workers=None, shard=None, interactive: bool = True,
                 force_stages=(), design_doc: str = "",
                 import_testcases: str = "", import_from_jira: str = "",
                 reference_testcases: str = "", derive_from_documents: bool = False,
                 skip_bug_report: bool = False, max_run_tokens: int = None,
                 explore_screens=(), feature_link: str = ""):
        self.profile = profile
        self.story_key = story_key
        self.epic_key = epic_key
        self.figma_url = figma_url
        #: OpenAPI/Swagger spec for the service under test — the API-side
        #: counterpart of figma_url, handed to the Test Case Writer so a
        #: story stated in endpoints and status codes gets API cases.
        self.api_spec = api_spec
        self.cycle_id = cycle_id
        self.include_manual = include_manual
        self.skip_execution = skip_execution
        self.workers = workers
        self.shard = shard
        self.interactive = interactive
        self.design_doc = design_doc
        #: Pasted JIRA Feature/Epic/Story/Test link or key — the entry point
        #: for the Test Case Writer's hierarchy walk (ancestor issues + linked
        #: Zephyr Test issues, each with comments/attachments). See
        #: agents.testcase_writer.fetch_jira_hierarchy. Independent of
        #: story_key/epic_key, which still name the feature directory.
        self.feature_link = feature_link
        #: Import mode: use existing test cases instead of generating new ones.
        #: Mutually exclusive; the CLI enforces that.
        self.import_testcases = import_testcases
        #: Existing cases used as a COVERAGE BASELINE for generation
        #: (generate only what these do not cover). Unlike
        #: `import_testcases`, which replaces generation, this one feeds
        #: it — so the two are independent rather than exclusive.
        self.reference_testcases = reference_testcases
        #: True when the run was started with NO --story, so test cases are
        #: derived from documents/Figma/spec/existing-cases instead.
        #:
        #: `story_key` still carries the DOC-1 placeholder, because the
        #: feature directory and sheet filename are built from it before any
        #: agent runs. This flag is what stops that placeholder being sent to
        #: the writer as if it were real: given a non-empty key the writer
        #: fetches it from JIRA, and a run with no story spent three retries
        #: failing DNS on `/rest/api/2/issue/DOC-1` before this existed.
        self.derive_from_documents = derive_from_documents
        self.import_from_jira = import_from_jira
        self.skip_bug_report = skip_bug_report
        #: CLI override for the profile's token ceiling. None means "use the
        #: profile"; 0 explicitly means "no cap", which is why this cannot
        #: just be `or`-ed against the profile value.
        self.max_run_tokens = max_run_tokens
        #: Stage names to re-run even when their output already exists.
        self.force_stages = set(force_stages or ())
        #: Screens the App Explorer should crawl this run. Empty means "every
        #: screen the profile declares" — the previous, unconditional
        #: behaviour. Naming screens matters because the crawl is per-run
        #: cost against the live app: a Vendor registration gains nothing
        #: from crawling the Identification module, and the unrelated visits
        #: made a run log read as though the pipeline had wandered off.
        self.explore_screens = tuple(explore_screens or ())

        self.started = utc_iso()
        #: Identifies THIS invocation in run_manifest.json, so a resume can be
        #: told apart from a fresh run over the same feature directory. See
        #: `starts_new_run`.
        self.run_id = f"{self.started}-{os.getpid()}"
        self.metrics = RunMetrics()
        self.state = {}          # stage name -> whatever that stage returned
        self.crawl_budget_used = 0

        self.feature_dir = profile.context_root / f"{epic_key}_{story_key}"
        self.feature_dir.mkdir(parents=True, exist_ok=True)
        self.repo_dir = profile.workspace

        self._ledger = None
        self._allow_crawl = allow_crawl

    # -- lazily-opened resources ------------------------------------------

    @property
    def ledger(self):
        """Opened once per run. Held open across stages so a crawl in the
        Planner is already known to the Coding Agent without a reload."""
        if self._ledger is None:
            self.profile.brain_dir.mkdir(parents=True, exist_ok=True)
            self._ledger = open_ledger(self.profile.brain_dir)
        return self._ledger

    @property
    def allow_crawl(self) -> bool:
        """Crawling needs a reachable app. `--skip-execution` means there
        isn't one, so the gates degrade to ledger-only rather than paying a
        failed browser launch per unresolved element — the same latch
        `dom_inspector` applies after its first failure."""
        if self._allow_crawl is not None:
            return self._allow_crawl
        if self.skip_execution or not self.profile.base_url:
            return False
        if self.crawl_budget_used >= self.profile.get("budgets.max_crawls_per_run", 25):
            return False
        return bool(self.profile.get("gates.allow_crawl", True))

    @property
    def max_repairs(self) -> int:
        return int(self.profile.get("gates.max_repairs", 2))

    def note_crawls(self, result):
        """Charges the crawls a gate actually performed against this run's
        allowance.

        `allow_crawl` above reads `crawl_budget_used`, but nothing ever
        incremented it — `budgets.max_crawls_per_run` was configuration the
        pipeline could not enforce, and an escalation ladder on a story whose
        element names are all wrong crawls once per element and looks like a
        hang. The verifiers already report how many locators they resolved by
        crawling, so the count is taken from the gate's own meta rather than
        by threading a callback through every check.
        """
        if result is None:
            return
        # Failed crawls cost a browser session too, so they are charged as well.
        for source in _meta_blocks(result.meta, "crawled"):
            self.crawl_budget_used += int(source.get("crawled", 0) or 0)
            self.crawl_budget_used += int(source.get("crawl_failed", 0) or 0)

    # -- paths -------------------------------------------------------------

    def artifact(self, filename: str) -> Path:
        return artifact_path(self.feature_dir, filename)

    @property
    def sheet_path(self) -> Path:
        return self.artifact(f"{self.epic_key}_{self.story_key}_TestCases.xlsx")

    # -- resume ------------------------------------------------------------

    def stage_done(self, stage: str) -> bool:
        return stage in self._manifest().get("completed", {})

    def mark_done(self, stage: str, summary: str = ""):
        manifest = self._manifest()
        manifest.setdefault("completed", {})[stage] = {
            "at": utc_iso(), "summary": summary}
        manifest["contract_version"] = CONTRACT_VERSION
        # Claimed by THIS run, and not yet finished. See `starts_new_run`.
        manifest["run_started"] = self.run_started(manifest)
        manifest["run"] = {"id": self.run_id, "finished": False}
        write_json(self.artifact("run_manifest.json"), manifest)

    def run_started(self, manifest: dict = None) -> str:
        """When the LIVE run began, surviving resumes.

        `self.started` is fresh per invocation, so a token budget measured from
        it restarted at zero on every resume and an interrupted run could
        spend its cap again and again. An unfinished run's original start is
        read back from the manifest; a new run starts now."""
        manifest = manifest if manifest is not None else self._manifest()
        run = manifest.get("run")
        if (isinstance(run, dict) and not run.get("finished") and manifest.get("run_started")
                and manifest.get("completed")):
            return str(manifest["run_started"])
        return getattr(self, "started", "") or utc_iso()

    def mark_run_finished(self):
        """Records that a pass over the stage graph reached its end.

        THIS IS THE RUN BOUNDARY the pipeline previously had no way to
        express. The feature directory doubled as the run, so "resume an
        interrupted run" and "start a new run over the same feature" were
        indistinguishable: both are the operator typing the same command
        again, against a manifest with the same completed stages.

        Written on a normal end of the loop, INCLUDING a deliberate halt
        (`--until`, a stage that stops the pipeline) — those finished doing
        what was asked. Deliberately NOT written when a stage raises, so an
        interrupted run is still resumable and its context is preserved.
        """
        manifest = self._manifest()
        if not manifest.get("completed"):
            return  # nothing ran; there is no run to close
        manifest["run_started"] = self.run_started(manifest)
        manifest["run"] = {"id": self.run_id, "finished": True}
        manifest["contract_version"] = CONTRACT_VERSION
        write_json(self.artifact("run_manifest.json"), manifest)

    def starts_new_run(self) -> bool:
        """Whether this invocation begins a NEW run rather than resuming one.

        Three states, and the middle one is the whole point:

          - No completed stages           -> new run (nothing to resume).
          - Previous pass FINISHED        -> new run (that one is over).
          - Previous pass did NOT finish  -> a resume; its notes and its
                                             partial artifacts are still live.

        A manifest with no `run` block at all predates this bookkeeping. Its
        completions cannot be attributed to any live run, so they are treated
        as history and this counts as a new run — which is right for the
        common case (a completed or long-abandoned feature directory) and
        costs a legacy interrupted run one archived summary, once.
        """
        manifest = self._manifest()
        if not manifest.get("completed"):
            return True
        run = manifest.get("run")
        if not isinstance(run, dict):
            return True
        return bool(run.get("finished"))

    def _manifest(self) -> dict:
        manifest = read_json(self.artifact("run_manifest.json"), default={}) or {}
        # A feature directory written by an incompatible contract version is
        # not resumable — half its artifacts would fail validation on read and
        # be treated as absent, which produces a confusing partial re-run
        # rather than a clean one.
        if manifest.get("contract_version") not in (None, CONTRACT_VERSION):
            print(f"      ⚠️  {self.feature_dir.name} was written by contract version "
                  f"{manifest.get('contract_version')}, this build is "
                  f"{CONTRACT_VERSION} — ignoring its resume state and re-running "
                  f"from the start.")
            return {}
        return manifest

    def should_run(self, stage: str) -> bool:
        if stage in self.force_stages:
            return True
        return not self.stage_done(stage)

    # -- reporting ---------------------------------------------------------

    def save_metrics(self):
        path = self.artifact("run_metrics.json")
        history = read_json(path, default=[])
        if not isinstance(history, list):
            history = []
        history.append({"run_start": self.started, "finished": utc_iso(),
                        "story": self.story_key, **self.metrics.snapshot()})
        write_json(path, history)
        return path
