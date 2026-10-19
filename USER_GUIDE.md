# User Guide

Every command, in the order you need them. For how the pipeline works
internally, see [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Contents

1. [Install](#1-install)
2. [Configure `.env`](#2-configure-env)
3. [Verify the install](#3-verify-the-install)
4. [Create a profile](#4-create-a-profile)
5. [Ground the pipeline](#5-ground-the-pipeline)
6. [Scaffold the workspace](#6-scaffold-the-workspace)
7. [Generate test cases](#7-generate-test-cases)
8. [Run the full pipeline](#8-run-the-full-pipeline)
9. [Run one stage at a time](#9-run-one-stage-at-a-time)
10. [Inspect and correct the brain](#10-inspect-and-correct-the-brain)
11. [Troubleshooting](#11-troubleshooting)
12. [Command reference](#12-command-reference)

---

## 1. Install

**You need first:** Python 3.10+, Node 20+, and a provider API key.

```bash
./setup.sh          # macOS / Linux
```
```powershell
.\setup.ps1         # Windows
```

The script creates a virtualenv (`.venv`), installs the Python dependencies into it,
installs the Playwright browsers, creates `.env` from `.env.example`, and finishes by
running `qa-pipeline doctor`. Activate the virtualenv in every new terminal
(`source .venv/bin/activate`, or `.\.venv\Scripts\Activate.ps1` on Windows).

`qa-pipeline ...` and `python -m qa_pipeline.cli ...` are the same command; this guide
uses the shorter one once the virtualenv is active. Run **`qa-pipeline doctor`** any time
something is not working - it lists what is missing and how to fix it, and never prints
a secret.

### If `python` doesn't work on Windows

`python` on PATH is often the Microsoft Store stub, which fails with:

```
The system cannot find the file ...\WindowsApps\python.exe
```

Use the real interpreter's full path, e.g.:

```
C:\Users\<you>\AppData\Local\Python\bin\python.exe -m qa_pipeline.cli ...
```

Find it with `where python` or `(Get-Command python).Source`. All commands
below are written as `python`; substitute that path.

## 2. Configure `.env`

Copy the template and fill it in:

```bash
cp .env.example .env
```

**Minimum viable:** one provider key and the app URL.

```bash
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

TEST_ENV=local
LOCAL_APP_BASE_URL=https://your-app.example.com
LOCAL_APP_TEST_USER=you@example.com
LOCAL_APP_TEST_PASSWORD=...
```

`TEST_ENV` selects which `LOCAL_`/`CENTRAL_` triple is copied into the plain
`APP_BASE_URL` / `APP_TEST_USER` / `APP_TEST_PASSWORD` names every agent reads.
**Switch environments by changing `TEST_ENV` only.**

### Per-agent settings

Grouped by the agent that consumes them, so you can see what one stage reads.
All are **defaults** — the matching CLI flag always wins.

```bash
# Agent 1 — Test Case Writer
TESTCASE_WRITER_KNOWLEDGE_DIR=      # blank = profiles/<app>/input/docs/
TESTCASE_WRITER_DESIGN_DOC=         # one file    → --design-doc
TESTCASE_WRITER_REFERENCE_CASES=    # .xlsx       → --reference-testcases
TESTCASE_WRITER_IMPORT_FILE=        # .xlsx       → --import-testcases
TESTCASE_WRITER_API_SPEC=           # openapi.json→ --api-spec
TESTCASE_WRITER_FIGMA_URL=          #             → --figma
```

Every value taken from `.env` is **printed with its variable name** at the
start of the run. A path that doesn't exist is reported and dropped, not
silently ignored.

> **Do not point `TESTCASE_WRITER_KNOWLEDGE_DIR` at a profile root.** Ingestion
> recurses and `.json` is a text type, so `project.json` and `brain/ledger.json`
> would be ingested as "knowledge" — the ledger eating itself.

## 3. Verify the install

```bash
python --version                       # 3.10+
node --version                         # 20+
npx playwright --version               # 1.48+
python -m qa_pipeline.cli contracts    # proves the engine imports
pytest                                 # the whole suite
qa-pipeline doctor --profile myapp     # checks tools, keys, profile and workspace
```

## 4. Create a profile

One folder of **data** per application. The engine holds no project knowledge.

```bash
python -m qa_pipeline.cli init-profile myapp
```

Creates `profiles/myapp/` with `project.json`, `input/docs/`, `input/recordings/`,
`brain/`.

> `init-profile` **refuses an existing folder**. If you already made the
> directory, create `project.json`, `input/docs/`, `input/recordings/` and `brain/`
> by hand instead.

### Edit `profiles/myapp/project.json`

```json
{
  "app": {
    "base_url": "https://your-app.example.com",
    "screens": [
      { "name": "Dashboard", "context": "dashboard home screen" },
      { "name": "Login",     "context": "sign in screen" }
    ]
  },
  "connectors": { "jira": { "project": "", "issue_type": "Test" } },
  "gates": {
    "require_human_review": true,
    "require_locator_grounding": true,
    "allow_crawl": true,
    "max_repairs": 2
  },
  "budgets": { "max_run_tokens": 0, "max_files_per_run": 40, "max_crawls_per_run": 25 },
  "execution": { "workers": null, "allure": false }
}
```

Optional settings (all have safe defaults):

| Setting | Default | What it does |
|---|---|---|
| `gates.fail_on_test_failures` | `true` | `run` exits **6** when tests are still failing after self-healing, so CI cannot go green on a red suite. `--allow-failures` overrides per run. |
| `gates.merge_split_specs` | `true` | Merges a feature's `_tc003-019`-style split spec files back into one file (only when it provably compiles). |
| `budgets.max_run_seconds` | `0` (off) | Wall-clock ceiling for the self-heal stage; remaining cases are reported as not attempted. |
| `budgets.max_heal_attempts` | `2` | Heal attempts per case group. A failed attempt is rolled back before the next. |
| `execution.timeout_minutes` | `0` (90 min) | Time limit for `execute`. `EXECUTOR_TIMEOUT_SECONDS` in the environment wins. |

`app.screens[].context` is what the crawler navigates by and what the route map
is matched against — it is not decorative. Leave `connectors.jira.project`
empty to skip the Jira Uploader stage entirely (the run prints `[skipped] Jira Uploader` and why).

### Route map (`workspaces/myapp/routes.json`)

A screen the crawler cannot reach by name is reached by a declared route. The
active profile's own `routes.json` is picked up automatically.

```json
"image-editing-tool": {
  "clicks": ["Registration. Click to access Registration module",
             "css=#btnNewRegistration",
             "css=#btnContinue:visible",
             "upload=#fileUpload|C:\\path\\to\\images"],
  "expect": "#ieSyncView",
  "aliases": ["image editing tool"]
}
```

- `css=<selector>` clicks the first **visible** match (ids are not always unique).
- `upload=<selector>|<file or folder>` sets a file on an `<input type=file>` or on
  the control that opens the chooser. A folder yields the first jpg/png under
  5 MB by name; `QA_UPLOAD_RANDOM=1` picks randomly instead.
- `expect` is a selector that must exist once the route has run. Without it a
  route whose clicks all "succeed" can still land on the wrong screen. A route
  that falls back to keyword guessing, or misses its `expect`, is **not recorded**
  into the ledger.
- A state that only exists after an action (an opened panel, a dialog) is just a
  second, deeper route with its own `aliases`; the deepest matching route wins.

Dry-run every route against the live app before a long run:

```bash
python -m qa_pipeline.cli check-routes --profile myapp
```

The Planner and Coding Agent reuse one snapshot per route for
`QA_ROUTE_SNAPSHOT_TTL` seconds (default 600; `0` re-walks the route for every
case).

### Generated page objects and navigation

The Coding Agent no longer asks the model to write page objects. It builds one
class per screen from the Planner's grounded locators (`src/pages/<Screen>Page.ts`,
one `readonly` Locator per element) and a `src/utils/routes.ts` with
`followRoute(page, '<route>')` generated from your `routes.json`. The model writes
specs only, against a member list it is given; a model-written page object is
discarded. Set `QA_GENERATED_POM=0` to restore the old behaviour.

| Variable | Default | Effect |
|---|---|---|
| `QA_GENERATED_POM` | `1` | `0` lets the model write page objects again |
| `QA_SNAPSHOT_MAX_ELEMENTS` | `120` | elements kept per live DOM snapshot |
| `QA_ROUTE_SNAPSHOT_TTL` | `600` | seconds a route snapshot is reused; `0` disables |
| `QA_UPLOAD_RANDOM` | unset | `1` picks a random image for `upload=` steps |

### Stage-selection rules

- `--until X` stops at X even when X is already done.
- `review_gate` is re-evaluated on every run: approving a sheet once is not a
  standing approval if the sheet is regenerated.
- `--only`, `--until` and `--force` reject unknown stage names, and `--only` never
  marks the run finished.
- `--force X` re-runs X only; later stages that were built from X's old output
  are listed as stale so you can `--force` them too.

### Drop in what you know

```
profiles/myapp/input/docs/     design docs, specs, Figma exports, screenshots
profiles/myapp/input/recordings/ playwright codegen demonstrations
profiles/myapp/conventions.md house style for generated code
```

### Where the output goes

You edit `input/`; you read `runs/`. Each run gets one folder:

```
runs/myapp/PROJ-1000_PROJ-1234/
  summary.md        open this first
  1_testcases/      the manual test case sheets (.xlsx) you review
  2_automation/     plan, generated-file list, code review
  3_results/        run results, triage, heals, BUG_LIST.md, evidence
  _internal/        bookkeeping — manifests, token logs, caches. Ignore it.
```

The generated Playwright code is not in the run folder; it lives in
**Agent 1's one output.** `1_testcases/` holds the final test cases: one
`<epic>_TestCases.xlsx` (plus `test_cases.json`). With several stories, each
story's own sheet is an intermediate kept in `_internal/per_story/`; the merged
sheet is the one to review. Near-duplicate cases across stories are flagged in
`_internal/testcase_writer_duplicates.json`, not deleted.

**One sheet of every case.** `py scripts/build_master_sheet.py --profile myapp --feature PROJ-1000 --run <run>`
joins the per-story manual cases, the consolidated picks, the run's sheet, the spec files and the last
run's results into one worksheet: each case with its automation status (script exists / selected, no
script yet / automatable / manual only), its test file and its last result. Re-run it after any
generation or execution to refresh it.

**The final automation sheet.** `py scripts/build_final_automation_sheet.py --profile myapp --feature PROJ-1000 --run <run>`
picks one representative case per distinct tool behaviour (grouped by feature + control + behaviour, so Zoom In
and Zoom Out are never merged), keeps every case that already has a script, leaves out manual-only and
engineering-only cases, and lists any tool control with no case at all. It uses the pipeline's own column
layout, so the Coding Agent can read it directly.

**Scoping the final sheet to the real screen.** `profiles/<app>/automation_scope.json` says what the
application's screen has, which cases an existing script already covers, and which need test data or a tool.
`build_final_automation_sheet.py` applies it: cases for things that are not on the screen are left out, covered
cases are attached to the script that covers them, and every case gets a decision. Rules are checked in order and
the first match wins, so put the more specific rule first.

**Sharing the framework.** `workspaces/myapp/` is a self-contained Playwright
repo: `git init` it, push it to Bitbucket, and anyone can
`npm install && npm run install:browsers && cp .env.example .env && npm test`
with no AI. It ships `.env.example`, a `bitbucket-pipelines.yml` and a README.

`workspaces/myapp/`. If you have folders from before this layout, move them
once with `py scripts/migrate_layout.py` (dry run) then `--apply`.

Supported for ingestion: `.md .txt .markdown .rst .csv .json .yaml .yml
.docx .pdf` and images (read as **real images**, so button labels are read off
the pixels). Anything else is skipped with a reason. Subfolders are read too.

## 5. Ground the pipeline

```bash
python -m qa_pipeline.cli ingest --profile myapp
python -m qa_pipeline.cli explore --profile myapp
```

Crawl specific screens only:

```bash
python -m qa_pipeline.cli explore --profile myapp --screen Login --screen Dashboard
```

Check what it learned:

```bash
python -m qa_pipeline.cli brain --profile myapp
```

## 6. Scaffold the workspace

```bash
python -m qa_pipeline.cli scaffold --profile myapp
cd workspaces/myapp && npm install && cd -
```

Deterministic, no AI. Never overwrites an existing file, so it is safe to
re-run.

## 7. Generate test cases

### From a JIRA story

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000 --until testcase_writer
```

Output: `runs/myapp/PROJ-1000_PROJ-1234/1_testcases/PROJ-1000_PROJ-1234_TestCases.xlsx`

### From a local JIRA XML export (no network to JIRA)

The epic is read from the file, so `--epic` becomes optional:

```bash
python -m qa_pipeline.cli run --profile myapp --story ./exports/PROJ-1234.xml --until testcase_writer
```

### Without a story at all

**Any one input is enough.** With no `--story` the sheet is keyed `DOC-1`.

```bash
# from a design document
python -m qa_pipeline.cli run --profile myapp --epic MYAPP --design-doc ./domain.docx --skip-execution --until testcase_writer

# from an OpenAPI spec (JSON; YAML needs PyYAML)
python -m qa_pipeline.cli run --profile myapp --epic MYAPP --api-spec ./openapi.json --skip-execution --until testcase_writer

# from Figma
python -m qa_pipeline.cli run --profile myapp --epic MYAPP --figma https://figma.com/file/abc --skip-execution --until testcase_writer

# from a folder of documents (ingest first)
python -m qa_pipeline.cli ingest --profile myapp
python -m qa_pipeline.cli run --profile myapp --epic MYAPP --skip-execution --until testcase_writer
```

`--skip-execution` stops the App Explorer crawling while `base_url` is still a
placeholder. Drop it once the real URL is set.

Two refusals, both deliberate: **`--epic` alone** (it names the sheet, it
describes nothing to test) and **no usable input at all** (the stage fails and
lists what it accepts, rather than inventing cases).

### Growing an existing suite

`--reference-testcases` reads a sheet as a **coverage baseline** — the writer
produces only what those cases do not already cover. The old sheet is never
modified.

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000 --reference-testcases ./regression.xlsx --until testcase_writer
```

Not to be confused with `--import-testcases`, which **replaces** generation by
normalising an existing sheet into this pipeline's format:

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000 --import-testcases ./existing.xlsx
```

Import *replaces*; reference *informs*.

### Rebuilding a sheet

An existing sheet is **reused, not regenerated** — rebuilding would blank the
`Status` column and destroy the approval marks the next stage requires. To
genuinely rebuild:

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000 --only testcase_writer --force testcase_writer
```

### Batch-import a folder of sheets

Files named `<EPIC>_<STORY>.xlsx` or `<STORY>.xlsx`, one per story:

```bash
python -m qa_pipeline.cli import-folder --profile myapp --folder ./sheets --epic PROJ-1000
```

## 8. Run the full pipeline

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000
```

It stops at the review gate the first time. Open the sheet, set every row's
`Status` to exactly **`Reviewed`** (case-sensitive), then run the same command
again — it resumes where it stopped.

### Useful variants

```bash
# stop after code generation (no live environment)
... --skip-execution

# also automate rows marked Execution Type = Manual
... --include-manual

# stop after a named stage
... --until automation_planner

# run only these stages (repeatable)
... --only coding_agent

# re-run a stage whose output already exists (repeatable)
... --force testcase_writer

# crawl only these screens
... --screens Login --screens Dashboard

# never prompt on failure — for CI
... --non-interactive

# cap this run's spend; 0 = no cap
... --max-run-tokens 200000

# parallelism / sharding
... --workers 4 --shard 1/3
```

## 9. Run one stage at a time

```bash
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000 --only <stage>
```

Stage names, in order:

```
doc_ingestor  app_explorer  testcase_writer  review_gate  jira_uploader
framework_scaffolder  automation_planner  coding_agent  executor
reviewer  self_healer  bug_reporter  brain_feedback
```

A stage that already completed is **skipped** with a note. Add
`--force <stage>` to re-run it.

Filtered-out stages still reload their previous results, so `--only coding_agent`
sees the Planner's output rather than finding nothing.

## 10. Inspect and correct the brain

```bash
# what does it know?
python -m qa_pipeline.cli brain --profile myapp

# what would an agent retrieve for this task?
python -m qa_pipeline.cli brain --profile myapp --query "overlay blend slider" --agent coding_agent

# mark a card wrong so it stops grounding generated code
python -m qa_pipeline.cli brain --profile myapp --demote CARD_ID --reason "renamed in release 4.2"
```

Demoting is the manual correction path: use it when you have proved a locator
wrong by hand. The card is demoted, not deleted, so the ledger remembers.

## 11. Troubleshooting

**Not sure what is wrong?** Run `qa-pipeline doctor --profile myapp` (add `--online` to
also test that the application URL is reachable). It checks Python, Node, the Playwright
browsers, `.env`, the model key, the application login, and the profile's inputs and
workspace, and tells you the fix for each. It prints variable *names*, never values.
Every `run` also keeps its full console output in `runs/<app>/<run>/_internal/run.log`.

**What did the self-healer actually change?** `runs/<app>/<run>/3_results/heals/` holds one
`.diff` per attempt (`<case>.attempt1.failed.diff`, `...verified.diff`, `...rejected.diff`)
with the model's claim and the real before/after. A heal that did not pass is rolled back,
so the test file is exactly as generated. A `force: true` in generated or healed code must
carry `// qa-allow-force: <reason>` on the line above, or it is rejected.

**`no config file. Expected one of: project.json, ...`**
The profile folder exists but isn't a profile. See §4.

**`Ledger holds no cards yet — run qa-pipeline ingest`**
Ingestion never succeeded. Run `ingest` and confirm with `brain` that the total
is non-zero before generating.

**`Route '<name>': the app home page never painted a clickable element`**
The app or its backing services are down — **not** the route map. Retry when
the environment is healthy.

**`Route '<name>': no clickable element named '<label>'`**
This one *is* the route map. Compare `workspaces/<app>/routes.json` against the
real UI; accessible names must match exactly.

**`workspace API usage limits` / `credit balance`**
A provider account or billing problem, not a modelling one. Fix the account and
re-run the stage; the run exits non-zero rather than reporting empty success.

**The review gate won't pass**
`Status` must be exactly `Reviewed`, case-sensitive, on every non-blank row. A
header-only sheet halts by design.

**The Allure report shows a test twice, or totals that match no run**
Old results were left in `allure-results/` (a run that was killed). `execute` now keeps only the newest
result per test before it builds the report; a full run also starts from a clean folder.

**`ERR_TOO_MANY_REDIRECTS`, `page.goto` timeouts, `ERR_NETWORK_CHANGED`**
The environment, not the test: usually too many parallel logins. Use `--workers 2` (or 1) and let
`--rerun-failed` sort flaky from real. If they persist at 1 worker, check the application build first:
a wrong build redirects the login in a loop.

**A button opens the browser's Save dialog**
Playwright cannot click inside an OS dialog and no `download` event fires. Use
`src/utils/save-picker.ts` (`installSavePickerStub` before the page loads, `captureSave` around the click):
it records the name and file types the app asked for and captures the bytes it wrote.

**Excel locks the sheet**
Close the workbook before a stage rewrites it, or the write fails with
`PermissionError`.

## 11b. When the model API is unavailable

Before the first model-powered stage the run makes one tiny request through the
configured provider. If the key is missing or invalid, the workspace quota or billing
is exhausted, or the network is down, it stops at once with **exit code 5** and the
provider's own message — nothing is spent, and nothing is half-written.

Six stages need the model (doc_ingestor, testcase_writer, automation_planner,
coding_agent, reviewer, self_healer). Everything else still works and is the way to
keep making progress:

```bash
python -m qa_pipeline.cli explore      --profile myapp      # crawl, refresh the ledger
python -m qa_pipeline.cli check-routes --profile myapp      # dry-run every route
python -m qa_pipeline.cli run --profile myapp --story X --epic Y --only executor --force executor --workers 3
```

A generated suite can also be fixed by hand: the spec files are ordinary Playwright
TypeScript under `workspaces/<app>/tests/`. Read the real failures from
`runs/<app>/<epic>_<story>/3_results/run_results.json` (each carries the error, the failing line and
a screenshot), and probe the live screen rather than guessing. Page objects
(`src/pages/*Page.ts`), `src/utils/routes.ts` and `src/utils/sliders.ts` are
**generated** and rebuilt on every Coding Agent run, so put hand fixes in the specs.

### Parallel workers, shared logins and an unstable environment

`execution.workers` defaults to 1. Whether more workers help is **not established** for the
target app: on the same day the full suite passed 20 of 25 on 3 workers, then 6 of 25 on
1 worker, and the failures that dominated the bad runs were the same on both — the
New Registration dialog never opening, `page.goto` timeouts, `ERR_TOO_MANY_REDIRECTS`,
`ERR_NAME_NOT_RESOLVED`, an identity-provider "Internal Server Error". They come and go
with the time of day, so they point at the shared test environment, not at the tests.

Treat a run in which many cases fail before any test step as an environment problem: the
executor lists those cases in `environment_failures.json` and prints "N of M case(s) failed
before their own steps ... re-run before triaging", and the Self-Healer skips them. Re-run,
ideally at a quieter time, before spending model calls on triage.

## 12. Command reference

| Command | Purpose |
|---|---|
| `init-profile <name>` | Create a profile skeleton |
| `ingest --profile P` | Read `input/docs/` into the ledger |
| `explore --profile P [--screen S]` | Crawl the live app |
| `check-routes --profile P` | Dry-run every declared route against the live app |
| `brain --profile P [--query Q --agent A] [--demote ID --reason R]` | Inspect or correct the ledger |
| `scaffold --profile P` | Write the framework skeleton |
| `contracts [--show NAME]` | Show artifact contracts |
| `import-folder --profile P --folder D [--epic E]` | Batch-import sheets |
| `run --profile P [--story S] [--epic E] ...` | Run the pipeline |
| `execute --profile P [--workers N] [--shard 1/3] [--grep ID] [--rerun-failed none|infra|all] [--no-progress] [--no-allure]` | Re-run the existing suite in parallel with an Allure report, a live line per test, and one lone re-run of each failure to separate flaky from real. **No model, no API key.** Exit 6 if any test fails |
| `decline --profile P --case ID --reason R [--dry-run]` | Take one case out of play everywhere: run JSON, plan, the spec (`test.skip`) and every workbook |
| `status --profile P [--run R]` | Per-case progress locators → page object → script, written to each run's `pipeline_state.yml` (derived from the artifacts; marks a stage `stale` when its inputs changed but it was not rebuilt). No model |
| `link-jira --profile P [--results F] [--sheet X]` | After a Jira upload, write each Jira key back into the sheets and `jira-map.json` so every script maps to its Jira test |
| `coverage --profile P` | Which page-object controls the scripts actually use (untested / thin), read from the code |

### `execute`: run what already exists (no API key)

```bash
python -m qa_pipeline.cli execute --profile myapp                 # 4 parallel workers + Allure report
python -m qa_pipeline.cli execute --profile myapp --workers 6
python -m qa_pipeline.cli execute --profile myapp --shard 1/3     # one slice, for a CI matrix
python -m qa_pipeline.cli execute --profile myapp --grep PROJ-68479-TC-003
```

Results land in `runs/<app>/SUITE_latest/3_results/`; the Allure report in the workspace's
`allure-report/` (needs Java; open it with `npx allure open allure-report`). Workers default to
the profile's `execution.workers`, else 4: the target app is slow and shares one login, so a
fixed modest number is safer than half the CPU cores. A targeted `--grep` re-run keeps the
existing reports and REPLACES the earlier result of each re-run test, so every test appears once.
Without the pipeline at all, the framework runs on its own with
`npx playwright test` (what the generated `bitbucket-pipelines.yml` does, in 3 shards).

**While it runs.** One line per finished test (`[PASS  ]`, `[FAIL  ]` with the first line of the error) and
a running total, read from the Allure results as they appear (`--no-progress` to silence it).

**Flaky or real?** Each failed test gets ONE more attempt, alone on 1 worker (`--rerun-failed all`, the
default; `infra` retries only page-load / network errors; `none` turns it off). A test that passes on the
re-run is listed as *Flaky* and counts as passed; a test that fails again keeps its original error, so a
real defect is never hidden. Why: on this application 12 of 17 failures under 4 workers were
`page.goto` timeouts that passed when run alone.

**Known issues.** A test the spec annotates `{ type: 'issue' }` (the case and the application disagree) is
listed as `KNOWN ISSUE`, apart from unexplained failures. It still counts as failed and still exits 6.

**App script errors.** The `recordAppErrors` fixture notes uncaught script errors the application throws
during a test; `execute` lists the most frequent ones after the run. They never fail a test.

**Time limit.** 90 minutes by default (Playwright's own ceiling is 5 minutes below it, so it still writes
what it got through). Set `execution.timeout_minutes` in `project.json` for a longer suite;
`EXECUTOR_TIMEOUT_SECONDS` in the environment wins over both. If a run is cut short, the tests that did
finish are reported from the Allure results.

**Exit code 6 is not a crash.** It means the run worked and some tests failed. Do not write
`execute ... || execute ...`: that starts a second full run. Check the code, not the output.

### `decline`: take a case out of play, everywhere

```bash
# look first, change nothing:
python -m qa_pipeline.cli decline --profile myapp --case PROJ-68294-AUTO-TC-065 --reason "Feature not in build; only Dock exists." --dry-run
# then do it:
python -m qa_pipeline.cli decline --profile myapp --case PROJ-68294-AUTO-TC-065 --reason "Feature not in build; only Dock exists."
```

Marks the case `declined` in the run's `test_cases.json` and `automation_plan.json`, puts
`test.skip(true, 'Declined: ...')` first in its test (the steps stay for the day the feature returns),
sets *Declined - not applicable* and the reason in every workbook under `runs/<app>/*/1_testcases/`, and
records it in `automation_scope.json` (`declined`) so a REBUILT sheet keeps the decision. A test whose
title is built in a loop is reported as *needs a hand edit* rather than edited blindly. Nothing is deleted.

### `link-jira`: every script maps to its Jira test

The Jira Uploader records `case id -> Jira key` in `jira_upload_result.json`. `link-jira` (and the Jira
stage itself, for the sheet it uploaded) writes those keys back: a `Jira Key` column in the workbook(s)
and `jira-map.json` at the workspace root, which the `linkJira` fixture reads so each test in the report
carries a `jira` annotation. Rows that failed to upload get no key. When `connectors.jira.project` is
blank the stage says so (`[skipped] Jira Uploader: ...`) instead of vanishing.

### `coverage`: which controls do the scripts really use?

Reads the page objects (`readonly x: Locator`) and the specs: a control is covered when a test's code uses
it. Lists the ones used by no test or by exactly one. It cannot tell whether a test asserts *enough*
about a control; that is a review question. Controls only used through `routes.json` navigation (the
Registration pages) show as untested by design.

### `run` flags

| Flag | Meaning |
|---|---|
| `--story` | JIRA key or `.xml` export. **Optional** — without it, cases derive from other inputs |
| `--epic` | Required unless the XML carries an Epic Link, or no story is given |
| `--design-doc` | One human-authored reference file |
| `--api-spec` | OpenAPI/Swagger JSON |
| `--figma` | Figma file/frame URL |
| `--reference-testcases` | Existing sheet as a coverage baseline |
| `--import-testcases` | Existing sheet to import *instead of* generating |
| `--import-from-jira` | Zephyr cycle id or case keys to import |
| `--include-manual` | Also automate rows marked Manual |
| `--skip-execution` | Stop after code generation |
| `--skip-bug-report` | Skip `BUG_LIST.md` |
| `--only` / `--until` / `--force` | Stage selection, repeatable |
| `--screens` | Limit the Explorer's crawl |
| `--workers` / `--shard` | Execution parallelism |
| `--max-run-tokens` | Halt above N tokens; 0 = uncapped |
| `--cycle` | Test cycle id |
| `--non-interactive` | Never prompt; abort instead (CI) |

### Exit codes

| Code | Meaning |
|---|---|
| 0 | OK |
| 1 | Failed |
| 2 | Review required |
| 3 | Demonstration required |
| 4 | Budget exceeded |
| 5 | Model API unusable (missing/invalid key, quota, network) — nothing was spent |
| 6 | Run finished, but tests are still failing after self-healing (`--allow-failures` to exit 0) |
