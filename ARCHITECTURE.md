# Architecture

How this pipeline actually works, component by component, as built.

A JIRA story (or a design document, a Figma file, an API spec, or an existing
test suite) goes in. Reviewed manual test cases, a Playwright suite inside a
fixed scaffold, an executed run, a triaged review, healed tests and a bug list
come out.

---

## 1. The governing rule

> **The model proposes; deterministic code disposes.**

Every model output passes a zero-LLM verifier before the next stage may
consume it. No artifact crosses a stage boundary unverified, and **no locator
enters generated code unless something observed it on the running app.**

Two consequences follow, and they explain most of the design:

- A skill file *asks*; a verifier *enforces*. `reviewer.py` instructs the
  model not to invent compliance findings and then **checks it in code anyway**,
  with the comment "a skill instruction is not a guarantee". That distrust is
  deliberate and load-bearing.
- Failures surface at the stage that caused them, seconds after they happen,
  instead of thirty minutes later in a Playwright stack trace.

## 2. Layered layout

```
qa_pipeline/          the engine
  contracts/     L0   artifact schemas, validated on write AND on read
  brain/         L1   the ledger: sourced facts + provenance + trust
  verifiers/     admin   the gates — none of them a model call
  orchestrator/  L3   stage graph, resume, budgets, human gates
  agents/        L4   adapters over agents/, plus the framework scaffolder
  framework/          the deterministic POM scaffold, as data
agents/               stage implementations (LLM-calling and not)
utils/                LLM client, DOM inspection, locator verification, parsers
skills/               per-agent model instructions (*.skill.md)
instructions/         conventions.md — shared rules prepended to every skill
profiles/             one folder of DATA per application
workspaces/           generated Playwright repos, one per profile
runs/<app>/           per-run output, one folder per <epic>_<story>; layout is decided
                      in one place, utils/run_layout.py (summary.md, 1_testcases/,
                      2_automation/, 3_results/, _internal/ — unknown files default to
                      _internal/). Inputs live in profiles/<app>/input/{docs,recordings}
```

**The engine holds no project knowledge.** The test that this still holds:
adding a second application must require zero changes to `qa_pipeline/` — only
a new folder under `profiles/`.

Layering rule, enforced by convention: `agents/` imports nothing from
`qa_pipeline/`. Where an agent needs ledger access it receives a **callable**
(`knowledge_provider`), not a ledger object.

## 3. The thirteen stages

Fixed order, never a model deciding what comes next. Predictability is the
feature: an operator can say in advance what will happen, a resume knows where
it stopped, and a failure is attributable to one named stage.

| # | Stage | Model? | What it does |
|---|---|---|---|
| 1 | `doc_ingestor` | ✅ | Reads `input/docs/` into typed ledger cards |
| 2 | `app_explorer` | — | Crawls declared screens; ground-truth locators |
| 3 | `testcase_writer` | ✅ | Writes the manual test cases → `.xlsx` |
| 4 | `review_gate` | — | **Human touchpoint 1** — waits for `Reviewed` |
| 5 | `jira_uploader` | — | Optional; only if `connectors.jira.project` is set |
| 6 | `framework_scaffolder` | — | Writes the fixed POM skeleton |
| 7 | `automation_planner` | ✅ | Maps each case to files, screens, elements |
| 8 | `coding_agent` | ✅ | Generates Playwright code |
| 9 | `executor` | — | Runs the suite |
| 10 | `reviewer` | ✅ | Classifies each failure |
| 11 | `self_healer` | ✅ | Repairs what it can, re-runs |
| 12 | `bug_reporter` | — | Writes `BUG_LIST.md` |
| 13 | `brain_feedback` | — | Promotes locators that actually ran |

Six stages call a model: `doc_ingestor`, `testcase_writer`,
`automation_planner`, `coding_agent`, `reviewer`, `self_healer`. Each **must**
have `skills/<name>.skill.md` — a missing skill file is a hard
`FileNotFoundError` by design, since an agent must never run without its
standing instructions.

`bug_reporter`, `executor` and `jira_uploader` are fully deterministic and
have no skill file, no provider dependency.

## 4. L0 — Contracts

Eleven registered artifact schemas:

```
automation_plan   case_result     classification   element_value
generated_file    heal_outcome    knowledge_card   plan_element
run_result        screen_value    test_case
```

Validated **on write and on read**. Reading is the half people skip, and it is
the half that catches a file edited by hand between runs.

## 5. L1 — The brain is a ledger, not a document store

`profiles/<app>/brain/ledger.json`. Every fact is a **card** carrying its
source, its evidence and a confidence. Cards are keyed by natural id and
merged through `trust.resolve`, so ingestion is idempotent: re-ingesting an
unchanged document changes nothing, an edited one updates only what changed.
There is no "have I seen this file before" bookkeeping to get wrong.

**Confidence by source** (`brain/trust.py`):

| Source | Confidence |
|---|---|
| `confirmed` — a locator that actually ran green | 1.00 |
| `recorded` — a human demonstrated it | 0.95 |
| `crawled` — the Explorer saw it on the live app | 0.85 |
| `design_doc` | 0.55 |
| `figma` | 0.45 |
| `legacy_testcase` | 0.35 |
| `inferred` — the model guessed | 0.10 |

Two thresholds do the real work:

- **`OBSERVED = {crawled, recorded, confirmed}`** — only these may supply a
  locator to generated code. A design doc can name a screen; it can never
  produce a selector.
- **`GROUNDING_FLOOR = 0.80`** — below this a card is background knowledge
  only: usable to phrase a test case, never to build a click or an assertion.

A card proved wrong is **demoted, not deleted** (`DISPROVED_CONFIDENCE = 0.15`),
so the ledger remembers that it was disproved rather than rediscovering it.

Ranking is defensive: an unrecognised source ranks **below everything**, so a
card written by a future pipeline version can never outrank a confirmed
observation just because this build doesn't know its name.

## 6. admin — The verifiers (no model calls)

Three composed gates over eight modules:

| Gate | Composes | Catches |
|---|---|---|
| `gate_test_cases` | `schema_check`, `coverage_check` | malformed cases, duplicates, uncovered criteria |
| `gate_plan` | `schema_check`, `locator_check` | ungrounded elements before any code is written |
| `gate_code` | `schema_check`, `compile_check`, `locator_check`, `spec_hygiene` | code that won't compile, invented locators, convention breaches |

`scaffold_check` verifies the generated workspace skeleton itself.

The gate ordering matters: **grounding is checked at plan time, not at code
time.** Generating code around an element nobody can locate produces a file
that compiles, runs, and times out — the expensive failure the gates exist to
prevent.

### The repair loop

Every model stage is wrapped in `verifiers/repair_loop.py`, so the shape is
always **propose → verify → repair → verify → accept**.

Three details that were each earned:

- **The best attempt wins, not the last.** A later attempt can come back worse;
  returning it discards a better artifact already paid for.
- **The repair prompt carries the previous attempt's files**, not just the
  diagnosis. Without them each attempt was an independent rewrite and repairs
  oscillated (13 → 5 → 10 → 9 problems on one real case) rather than converging.
- **Exceptions are not caught here.** A rate limit must not be repaired as if
  it were a contract violation — that repairs a prompt which never reached the
  provider. Transient retry belongs to `orchestrator/runner.py`.

An identical result twice in a row stops the loop: *"the repair instruction is
not landing, so stopping instead of paying for another identical attempt."*

## 7. L3 — Orchestrator

`runner.py` walks the stage graph. Notable behaviours:

- **Resume.** A completed stage is skipped but still **rehydrates** its result
  into `ctx.state`, or the next stage reads an empty upstream result and
  reports "nothing to do" — a green run that did nothing.
- **`--only` still rehydrates filtered-out stages**, for the same reason.
- **The sheet wins over the cache.** `_rehydrate_test_cases` compares
  `test_cases.json` against the `.xlsx` and takes the spreadsheet when they
  disagree, announcing it. The sheet is what a human reviewed and edits.
- **Transient faults retry per request** (`utils/transient.py`), with
  exponential backoff and **full jitter** — the Coding Agent fires concurrent
  chunks on one key, so an unjittered delay marches every retry back into the
  provider in the same instant that earned the 429. A provider's `Retry-After`
  always wins over the computed delay.

### Failure classification

`utils/error_classify.py` separates **infra** (auth, billing, quota, spend cap)
from **content** (bad JSON, missing fields). The distinction decides whether
the operator should fix their account or rewrite their test cases — opposite
responses. An infra outage in `coding_agent` raises `StageFailed`, so the run
exits non-zero rather than reporting an empty success.

### Budgets and metrics

Per-profile: `max_run_tokens`, `max_files_per_run`, `max_crawls_per_run`,
`max_heal_attempts`. Written to `runs/<app>/<feature>/_internal/run_metrics.json`:

| Metric | Reads on | Healthy |
|---|---|---|
| `first_attempt_rate` | generation quality | climbing |
| `locator_verification_rate` | grounding quality | approaching 1.0 |
| `escalation_rung` | ledger maturity | shifting toward rung 1 |

## 8. The escalation ladder

How an unresolved element gets resolved, cheapest first. `ctx.metrics.rung()`
records how far up each resolution had to climb.

| Rung | Mechanism | Cost |
|---|---|---|
| 1 | Ledger hit | free |
| 2 | Crawl the live app | seconds |
| 3 | Near-miss repair | one model call |
| 4 | **Ask a human to demonstrate** | minutes, once per screen ever |
| 5 | Environment blocker | reported, not worked around |

Rung 4 is the only one that stops the pipeline, and it is asked **once per
screen, ever** — the resulting recording becomes the highest-confidence source
in the ledger, permanently.

## 9. The two human touchpoints

Neither is authoring.

**1. Approve the test case sheet** (~2 min per story). Set every row's
`Status` to exactly `Reviewed`. Two hard-won details in `gates.py`: a fully
blank row is skipped (Excel counts a formatting-only trailing row toward
`max_row`, making the gate permanently unsatisfiable), and a header-only sheet
**halts** rather than passing (zero rows means zero unreviewed rows, which the
naive check waves through).

**2. Demonstrate an unreachable screen** (rare). Hardware-gated states,
external redirects and seeded records genuinely cannot be crawled to, and the
only alternative to a person clicking through once is a model guessing — the
thing this architecture exists to prevent.

## 10. Test Case Writer inputs

The story is the **preferred** source, not a required one. Any one of these is
enough on its own, and they combine:

| Input | Flag | Notes |
|---|---|---|
| JIRA story | `--story` | issue key, or a local `.xml` export |
| OpenAPI spec | `--api-spec` | JSON; YAML needs PyYAML. Without a story it *derives* stories, one per tag |
| Design document | `--design-doc` | one file |
| Figma | `--figma` | |
| Existing test cases | `--reference-testcases` | **coverage baseline** — generate only what these do not cover |
| Folder of documents | `profiles/<app>/input/docs/` | ingested into the ledger |

With no `--story` the writer synthesizes one keyed `DOC-1`, so a
document-derived sheet is visibly not traceable to a tracker item. Its
`acceptance_criteria` is left **empty, not invented** — the coverage gate reads
that field, and a fabricated criterion would be trusted as if a human wrote it.

`--epic` alone is refused: it names the sheet, it does not describe anything to
test. With no usable input at all the stage fails and lists what it accepts,
rather than generating invented cases — which look like work.

**`--reference-testcases` sends titles only.** Given full existing cases plus a
story, a model tends to rewrite the existing ones instead of writing what is
missing; a bare title list supports the one judgement being asked. Capped at
400, and a truncated list says so, or the model treats two thirds of the
coverage as the whole picture.

Import *replaces* generation; reference *informs* it. Only the import flags are
mutually exclusive with each other.

## 11. The LLM client

`utils/llm_client.py` is the only module that knows a provider wire format.
`AI_PROVIDER` selects `anthropic | openai | azure_openai | openrouter | ollama`.

Three token mechanisms:

1. **Skill caching** — each agent's `skills/<name>.skill.md` is sent with
   `cache_control="ephemeral"`.
2. **Condensed context** — `runs/<app>/<feature>/summary.md`, a running summary
   agents extend instead of re-sending raw payloads. Archived at the start of a
   fresh pass; one real feature directory had reached 28 KB of accumulated
   history, re-sent and paid for on every call.
3. **Shared prefix** — `instructions/conventions.md` is prepended as its own
   cached block. This is not only DRY: caching needs a prefix above a per-model
   floor (~1024 tokens Sonnet-class, ~4096 Haiku-class), and most skill files
   are under that alone. The shared block is what makes mechanism 1 engage.

A missing skill file is fatal; a missing `conventions.md` degrades silently.
That asymmetry is intentional.

## 12. The generated workspace

`scaffold` writes a normal Playwright repository that a team owns and edits by
hand. The layout is fixed and identical on every project — which is what lets a
human move between suites, and what lets the generator produce code that
reliably compiles.

```
src/pages/        Page Objects       — one per screen, extends BasePage
src/components/   Component Objects  — reusable controls
src/fixtures/     Shared fixtures    — specs import { test, expect } from here
src/config/       env.ts — the only place process.env is read
tests/ui|api|e2e/ specs by kind
```

Path aliases (`@pages/*`, `@fixtures/*`, `@config/*`) are configured in the
scaffold's `tsconfig.json`. The scaffolder **never overwrites an existing
file**, so it is safe to re-run and a workspace can never be half-scaffolded.

`POM_SUMMARY.md` in the workspace root is read by the Planner and Coding Agent
as the inventory of reusable locators and helpers.

## 13. Configuration precedence

```
CLI flag  >  .env  >  profile (project.json)  >  built-in DEFAULTS
```

Per-agent `.env` variables are **defaults, never overrides**. Every value taken
from the environment is printed with its variable name before use — an input
nobody typed, silently steering a run, is how two runs differ for no visible
reason. A path that does not exist is reported and dropped, because the
downstream loaders treat an unreadable document as "no extra context".

`--story` and `--epic` stay on the command line by design: a story key in a
config file is the one value that must never be reused by accident.

## 14. Exit codes

CI can branch on these without parsing output.

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Failed |
| 2 | Review required (touchpoint 1) |
| 3 | Demonstration required (touchpoint 2) |
| 4 | Budget exceeded |

---

## Known gaps

Recorded honestly rather than omitted.

- **`REPO_OUTPUT_DIR` and `CONVENTIONS_FILE`** in `.env` are dead — no module
  reads either. Kept only so an existing `.env` does not appear to lose
  settings.
- **A skill instruction is a request, not a guarantee.** Where correctness
  matters the verifier must check it. `--reference-testcases` currently relies
  on the model honouring "do not duplicate"; there is no gate enforcing it.
- **A blank app breaks crawling, not just slows it.** `_wait_for_app_ready`
  returning False is a common outcome, not a rare timing edge — measured 3 in 5
  on one environment, staying blank through the reload it performs. Callers
  must treat it as a real result; `_navigate_declared_route` once discarded it
  and blamed the route map for an app that never painted.
- **Two independent story-key resolutions must stay in sync, with nothing
  catching drift.** `qa_pipeline/cli.py`'s `_apply_env_defaults`/XML peek
  (`adapters.peek_story_from_xml`) computes `ctx.sheet_path`/`epic_key`/
  `story_key` *before* the pipeline starts; `agents/testcase_writer.py`'s
  `fetch_jira_story_from_file` independently re-parses the same XML at run
  time to name its own output file. No shared source of truth and no
  assertion that the two agree — unconfirmed as a live failure today (no XML
  fixture found that makes them disagree), but if they ever do, `stage_code`
  silently reads the wrong/stale sheet rather than failing loudly. Only
  reachable via the `--story <xml-path>` import path, not JIRA-key or
  `--import-testcases`/`--reference-testcases` sheet imports.
- **`agents/testcase_writer.py`'s `fetch_jira_story` reads `JIRA_*` env vars
  directly**, bypassing `qa_pipeline/cli.py`'s `_ENV_DEFAULTS`/
  `_apply_env_defaults` table — no CLI-flag override, no "taken from
  environment" print. Same un-migrated-auth shape CLAUDE.md already calls out
  for `agents/jira_uploader.py` ("still has its own duplicated auth helpers,
  not yet switched to `jira_client.py`"), just not previously listed there.
  Cosmetic/consistency only — JIRA calls here already degrade gracefully.

### Added this session

- **Login smoke check before `automation_planner`'s per-case loop.**
  `utils/dom_inspector.verify_login()` pays for one login attempt up front
  (`stage_plan` in `qa_pipeline/orchestrator/stages.py`, gated on
  `ctx.allow_crawl` and a configured `APP_BASE_URL`) and halts with a clear
  diagnosis if it fails, rather than letting every automatable case
  independently rediscover the same root cause through its own 4-attempt
  repair loop — on an 89-case run that was up to 356 wasted LLM calls before
  the fix in the item below was found.

### Fixed this session (previously silent)

- **`resolve_app_env()` was never called on the real orchestrator path.**
  `qa_pipeline/cli.py`'s `main()` loaded `.env` but never called
  `utils/env_config.resolve_app_env()` — that function is the only thing that
  copies `LOCAL_APP_BASE_URL`/`LOCAL_APP_TEST_USER`/`LOCAL_APP_TEST_PASSWORD`
  into the plain `APP_BASE_URL`/`APP_TEST_USER`/`APP_TEST_PASSWORD` every DOM
  inspection call reads. It was previously called only from each agent's own
  standalone `_cli()`. Effect: every full `run` had empty credentials, so
  every live-app login silently failed, and `dom_inspector.py` *always*
  reported `_login_attempted=True` regardless — misreporting a
  never-attempted login as "the page still shows a password field
  afterward," which looked exactly like a real app/credentials problem. Fixed
  by calling `resolve_app_env()` once in `main()`, before any stage runs, and
  printing which `TEST_ENV` target it resolved. (A second, compounding bug:
  `.env`'s `TEST_ENV` held a human label — `Dev2` — rather than the literal
  `local`/`central` keyword `get_target_env()` requires; corrected to
  `TEST_ENV=local`.)
- **A failed first login poisoned the rest of the run, with no retry.**
  `dom_inspector._get_page()` caches its Playwright `Page` and the
  `_login_ok`/`_login_attempted` module globals on first use and never
  revisits them — a purely transient first failure (slow IDP redirect,
  one-off render glitch) permanently graded every remaining case in the
  process `dom_inspection_quality="login_page"`, indistinguishable from a
  genuinely broken login. Fixed with one bounded retry (fresh `goto` +
  `_attempt_login`) before the failure is treated as permanent for the rest
  of the process.
- **`utils/jira_client.py`'s `children_of()` never tried Jira Cloud's
  `/search/jql` endpoint.** `GET /rest/api/3/search` is removed on Cloud
  (`*.atlassian.net`); `agents/jira_uploader.py`'s own
  `find_existing_jira_issue` already knew to try `/search/jql` first and fall
  back, but `children_of()` (used by `fetch_jira_hierarchy`'s Epic/Feature
  descendant walk) only ever called the removed endpoint — `_get` returns
  `None` on the resulting 404 (by design), so every Cloud instance's
  hierarchy walk silently came back empty, with no warning, dropping the
  Feature→Epic→Story→linked-Test-issue context `testcase_writer.py` folds
  into generation. Fixed by porting the same try-`/search/jql`-then-fall-back
  pattern into `children_of()`.
- **`stage_code`'s `prior_files` tracked the LAST attempt, not the BEST
  one** — contradicting the repair loop's own documented rule ("the best
  attempt wins, not the last"; see the repair loop's write-up above). A
  repair that regressed (attempt 2 worse than attempt 1) still became the
  baseline the next repair attempt was told to "edit," so the loop could
  spend its whole budget diverging from the best code already on file while
  the orchestrator was always going to return that better attempt-1 code
  regardless. Fixed by scoring each attempt with the repair loop's own
  `_score` and only replacing the cached baseline on an actual improvement.

## Run folder, healing and reporting rules (hardening pass)

Rules a change must keep. Each exists because the opposite shipped a real failure; the
test that pins it is named in brackets.

**Where files go.** Every artifact path goes through `utils/run_layout.artifact_path`;
nothing builds `feature_dir / "name"` by hand. The folder is decided in one table, and
an unregistered filename lands in `_internal/` (never in a folder a person reads).
Inputs live in `profiles/<app>/input/`, outputs in `runs/<app>/<run>/`, generated code in
`workspaces/<app>/`. [`test_run_folder_layout`, `test_one_final_test_case_file`]

**The Executor must not report a broken run as clean.** A failed `*.setup.ts` (sign-in)
is `failure_kind=infra`, not an empty run; Playwright is launched through `node` so a `|`
in `--grep` is not a shell pipe; heal verification runs with `--retries=0`; a full run
clears stale reports. `run` exits **6** when cases are still failing after healing.
[`test_executor_reliability`, `test_exit_code_when_tests_fail`]

**A heal may not leave a trace unless it worked.** Each attempt starts from the ORIGINAL
file; a failed attempt is rolled back; one `.diff` per attempt is kept under
`3_results/heals/`; an outcome records only the VERIFIED attempt's locator change (the
ledger is promoted from it); a patch to a shared page object re-runs every spec that
imports it and fails on a regression; a patch aimed at a generated file is refused, not
redirected. `heal_guard` compares the multiset of `(matcher, argument)` pairs, so swapping
`toHaveText('x')` for `toBeVisible()` is caught, and `force: true` needs a written reason
(`// qa-allow-force: <why>`). [`test_heal_rollback_and_audit`,
`test_heal_guard_catches_changed_assertions`]

**A label is only as good as its evidence.** `known_flaky` is recorded only for a case that
actually passed on a retry. Bug numbers are keyed on Playwright's own first error line, not
on the Reviewer's prose, and a bug absent from a later run is listed under "No longer
failing". [`test_known_flaky_needs_evidence`, `test_bug_numbers_are_stable`]

**Generated code is held to the skill's own rules.** `spec_hygiene` rejects hard waits,
`expect(await ...)` snapshots, unexplained `force`, and untagged tests; `data_files` warns
when a spec loads a `src/data/` file that does not exist. Split spec files are merged
back by `agents/spec_merge.py` only when the merge compiles, otherwise left split. Page
objects are rebuilt ADDITIVELY (existing members and their names are kept); the DOM
inspector prefers role+name locators and uses one only when it is unambiguous on the page.
[`test_spec_hygiene_enforces_the_skill`, `test_split_specs_are_merged_safely`,
`test_page_object_rebuild_keeps_existing_members`, `test_snapshot_prefers_role_locators`]

**The first thing to run is `qa-pipeline doctor`.** It prints names and yes/no, never a
value (`.env` holds real credentials). Every `run` keeps `_internal/run.log`.
[`test_doctor_and_cli_errors`, `test_run_log_is_kept`]

## Module map after splitting the large files

The four biggest modules were split into focused ones. Every moved name is still importable
from the ORIGINAL module (re-exported, and it is the same object), so existing imports and
test patches keep working. New code goes in the focused module, not back into the big one.
[`test_split_modules_keep_their_public_names` pins this and ratchets the line counts.]

| Original | Now also in | Holds |
|---|---|---|
| `agents/testcase_writer.py` (~800) | `testcase_sheet` | spreadsheet columns, writing/reading a sheet |
| | `testcase_jira` | reading a story, its hierarchy, acceptance criteria |
| | `testcase_design_docs` | Figma and local design documents |
| | `testcase_api_spec` | OpenAPI spec -> contract and derived stories |
| | `testcase_import` | importing existing cases; the coverage baseline |
| | `testcase_limits` | shared concurrency limit |
| `agents/coding_agent.py` (~1060, mostly `run`) | `ts_check` | tsc syntax/type checks, property-owner hint |
| | `coding_cases` | case sheet intake, chunking, split-file names |
| | `coding_dom` | which cases need a live DOM re-inspection |
| | `coding_repair` | locator repair, proven-locator hints, diagnostics |
| `orchestrator/stages.py` (~1350) | `heal_support`, `review_support`, `code_support`, `resume_support` | helpers each stage uses; the `stage_*` functions stay in `stages.py` |
| `utils/dom_inspector.py` (~1530) | `dom_snapshot` | the stateless page-snapshot extractor and MCP snapshot parser |

`dom_inspector.py` was split only where code is **stateless**. It keeps module-level mutable
state (`_page`, `_login_ok`, ...) that functions rewrite with `global`; moving such a function
to another module would silently give it its own copy of that state. Leave that cluster whole.

Two further rules this pass settled:
- **Process trees.** The Executor runs Playwright in its own process group/session and kills
  the whole tree on a timeout or interrupt (workers and browsers are grandchildren).
  [`test_timeout_kills_the_whole_process_tree`]
- **Typing.** The modules listed under `[[tool.mypy.overrides]]` in `pyproject.toml` are fully
  typed and CI blocks on them; the rest are on the lenient settings until brought across.
  [`test_strict_mypy_modules_are_in_ci`]

## `execute`, `decline`, `link-jira`, `coverage` (suite operations, no model)

These four commands work on a suite that already exists. None calls a model. New code lives in focused
modules, not in `execute_suite.py` / `cli.py`:

| Module | Holds |
|---|---|
| `qa_pipeline/execute_suite.py` | the `execute` command: workers, time limits, the run, the flaky re-run, the summary |
| `qa_pipeline/execute_support.py` | live progress (`ProgressWatcher`), `rerun_candidates` / `merge_rerun`, `dedupe_allure_results`, `cases_from_allure` |
| `qa_pipeline/decline.py` | declining one case in the run JSON, the plan, the spec (`test.skip`) and every workbook |
| `qa_pipeline/jira_link.py` | writing Jira keys back: a `Jira Key` column and `jira-map.json` |
| `qa_pipeline/coverage_report.py` | page-object controls vs the specs that use them |

Decisions worth knowing before changing them:

- **A failure gets one more look before it is called a defect.** `execute` re-runs each failed test once,
  alone on 1 worker. Only a test that turns green is *flaky*; one that fails again keeps its ORIGINAL error,
  so the re-run can never hide a real defect. It is a separate path from the heal verification, which stays
  at `retries=0` ("one honest attempt") because a patch that got lucky on attempt 2 must not count as fixed.
  [`test_a_real_defect_that_fails_again_is_never_hidden_by_the_rerun`]
- **A report has one result per test.** Allure appends; a re-run therefore used to leave the earlier
  attempt beside it. `dedupe_allure_results` keeps the newest per test (and removes the older attempt's
  attachments) before the report is built. [`test_a_rerun_replaces_the_earlier_attempt_so_each_test_appears_once`]
- **`issue` annotations are a separate bucket, not a pass.** A spec annotated `{ type: 'issue' }` records a
  known mismatch between a case and the application. `execute` lists it as `KNOWN ISSUE` but it still counts
  as failed and still exits 6; hiding it would be the pipeline deciding the application is right.
- **Declining is one operation.** A decision spread over four hand edits drifts. `decline` also writes
  `declined` into `automation_scope.json`, which `final_suite.annotate` honours, so a sheet rebuilt by
  `scripts/build_master_sheet.py` keeps it. Nothing is deleted; the spec is skipped, not removed.
- **Coverage is read from the code.** A control is covered when a test's code uses its page-object locator;
  title matching only says a case talks about it. It cannot judge assertion strength. A title built in a
  loop is attributed to the nearest preceding test, so counts are approximate for data-driven specs.
- **Scaffold additions** (`qa_pipeline/framework/templates.py`): the `recordAppErrors` and `linkJira` auto
  fixtures in `src/fixtures/index.ts` (annotations only; neither can fail a test) and
  `src/utils/save-picker.ts` for buttons that open the browser's native Save dialog.

Profile key: `execution.timeout_minutes` (0 = the 90-minute default). Precedence is unchanged: an
`EXECUTOR_TIMEOUT_SECONDS` in the environment wins.
