# qa-agent-pipeline

Zero-touch QA automation. Nobody writes manual test cases. Nobody writes
automation scripts.

A JIRA story goes in — or a design document, a Figma file, an API spec, or an
existing test suite. Reviewed manual test cases, a Playwright suite built into
a fixed framework scaffold, an executed run, a triaged review, healed tests and
a bug list come out. Thirteen stages behind gates that make no model call, and
a knowledge ledger where every fact carries its source.

## Documentation

| | |
|---|---|
| **[USER_GUIDE.md](USER_GUIDE.md)** | Install, configure, and every command |
| **[ARCHITECTURE.md](ARCHITECTURE.md)** | How it works — components, gates, the ledger, known gaps |

## The principle

> **The model proposes; deterministic code disposes.**

Every model output passes a zero-LLM verifier before the next stage may
consume it. No artifact crosses a stage boundary unverified, and **no locator
enters generated code unless something observed it on the running app.**

## What a human actually does

| | What | How often |
|---|---|---|
| **1** | **Approve the test case sheet.** Read the generated cases, set `Status` to `Reviewed`. | ~2 min per story |
| **2** | **Demonstrate an unreachable screen.** Click through it once under `playwright codegen`. | Rare, once per screen, ever |

Neither is authoring.

## Quick start

```bash
# 1. Install (Python 3.10+, Node 20+), then fill in .env
./setup.sh                                             # .\setup.ps1 on Windows

# 2. Create a profile for the application under test
python -m qa_pipeline.cli init-profile myapp
#    edit profiles/myapp/project.json — set app.base_url
#    drop design docs into profiles/myapp/input/docs/

# 3. Ground the pipeline in the app
python -m qa_pipeline.cli ingest  --profile myapp
python -m qa_pipeline.cli explore --profile myapp

# 4. Scaffold the workspace the generated suite lives in
python -m qa_pipeline.cli scaffold --profile myapp
cd workspaces/myapp && npm install && cd -

# 5. Run a story
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000
```

Step 5 stops at the human review gate the first time. Open the sheet in
`runs/<app>/<epic>_<story>/1_testcases/`, set every row's `Status` to exactly `Reviewed`, and
run the same command again — it picks up where it left off.

A story is the preferred input, not a required one:

```bash
python -m qa_pipeline.cli run --profile myapp --epic MYAPP \
    --design-doc ./domain.docx --until testcase_writer
```

See [USER_GUIDE.md §7](USER_GUIDE.md#7-generate-test-cases) for every input.

Exit codes: `0` ok · `1` failed · `2` review required · `3` demonstration
required · `4` budget exceeded · `5` model API unusable · `6` tests still failing after healing. CI can branch on these without parsing output.

Working on a suite that already exists (no model, no API key): `execute` (parallel, live progress, flaky
re-run, Allure), `decline` (take a case out of play in every file at once), `link-jira` (write Jira keys
back to the sheets and the workspace) and `coverage` (which page-object controls the scripts really use).
See USER_GUIDE.md section 12.

## Layout

```
qa_pipeline/      the engine — contracts, ledger, verifiers, orchestrator, CLI
agents/           the stage implementations (LLM-calling and not)
utils/            LLM client, DOM inspection, locator verification, parsers
skills/           per-agent model instructions (*.skill.md)
instructions/     conventions.md — shared rules prepended to every skill
profiles/         one folder of DATA per application
workspaces/       the generated Playwright repos, one per profile
runs/             per-run output: runs/<app>/<epic>_<story>/{summary.md, 1_testcases, 2_automation, 3_results, _internal}
tests/            the Python test suite
```

The engine holds no project knowledge. The test that this still holds:

> Adding a second application must require **zero** changes to `qa_pipeline/`.
> Only a new folder under `profiles/`.

## Tests

```bash
pytest
```

The `tests/` suite covers the verifiers, contracts, stage graph and the agents'
non-LLM logic. CI states out loud when there is nothing to run rather than
reporting a green build for a suite that was never collected.

## Health metrics

Written to `runs/<app>/<feature>/_internal/run_metrics.json` every run. Robustness is not an
agent count — it is these trending the right way.

| Metric | Reads on | Healthy |
|---|---|---|
| `first_attempt_rate` | generation quality | climbing |
| `locator_verification_rate` | grounding quality | approaching 1.0 |
| `escalation_rung` | ledger maturity | shifting toward rung 1 |
