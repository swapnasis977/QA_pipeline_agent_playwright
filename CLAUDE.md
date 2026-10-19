# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Zero-touch QA automation pipeline. A JIRA story (or a design doc, Figma file,
API spec, or existing test suite) goes in; reviewed manual test cases, a
generated Playwright suite, an executed run, triaged failures, self-healed
tests and a bug list come out. Thirteen fixed stages behind deterministic
(non-LLM) gates and a provenance-tracked knowledge ledger.

**Read [ARCHITECTURE.md](ARCHITECTURE.md) before making any non-trivial
change** — it documents the stage graph, the ledger's trust/confidence model,
the verifier gates, the repair loop, and known gaps, in more depth than is
useful to restate here. [USER_GUIDE.md](USER_GUIDE.md) has every CLI command
and flag. [README.md](README.md) has the 60-second overview.

## Commands

```bash
# Verify the engine imports + run the suite
python -m qa_pipeline.cli contracts
pytest                                   # whole suite
pytest tests/test_heal_verdict.py -q     # one file
pytest tests/test_heal_verdict.py::test_name -q   # one test

# Lint / type-check (CI: ruff is blocking, mypy is not — see pyproject.toml)
ruff check .
ruff check --fix .
mypy qa_pipeline agents utils

# Compile-check without running (fast sanity check after an edit)
python -m compileall -q agents utils qa_pipeline

# End-to-end, against a profile (one folder of DATA per application)
python -m qa_pipeline.cli init-profile myapp
python -m qa_pipeline.cli ingest  --profile myapp
python -m qa_pipeline.cli explore --profile myapp
python -m qa_pipeline.cli scaffold --profile myapp
python -m qa_pipeline.cli run --profile myapp --story PROJ-1234 --epic PROJ-1000

# Operate on a suite that already exists (no model, no API key)
python -m qa_pipeline.cli execute  --profile myapp [--workers 2] [--grep ID] [--rerun-failed none|infra|all]
python -m qa_pipeline.cli decline  --profile myapp --case ID --reason "why" [--dry-run]
python -m qa_pipeline.cli link-jira --profile myapp           # write Jira keys back after an upload
python -m qa_pipeline.cli coverage --profile myapp            # controls vs the scripts that use them
```

On Windows, `python` on PATH is often the Microsoft Store stub — use `py` or
the real interpreter's full path if `python` fails with a WindowsApps error
(see USER_GUIDE.md §1).

Exit codes (CI branches on these without parsing output): `0` ok · `1` failed
· `2` review required · `3` demonstration required · `4` budget exceeded · `5` model API unusable · `6` tests still failing after healing. Exit 6 is a finished run, not a crash: never chain `execute` with `||`.

## Architecture essentials

**The governing rule: the model proposes, deterministic code disposes.**
Every model output passes a zero-LLM verifier before the next stage may
consume it. A skill file (`skills/*.skill.md`) *asks* the model to behave;
a verifier (`qa_pipeline/verifiers/`) *enforces* it in code. Never trust a
skill instruction alone for anything that matters — add or extend a verifier.

**Layering rule, enforced by convention, not by tooling:** `agents/` imports
nothing from `qa_pipeline/`. Where an agent needs ledger access it receives a
plain callable (`knowledge_provider`), never a ledger object — this is what
keeps `agents/` the pure stage-logic layer and `qa_pipeline/` the stateful
engine around it. Check this before adding any new cross-import.

```
qa_pipeline/          the engine: contracts (L0) -> brain/ledger (L1)
                       -> verifiers (admin) -> orchestrator (L3) -> agents adapters (L4)
agents/                stage implementations (LLM-calling and not)
utils/                  LLM client, DOM inspection, locator verification, parsers,
                        JIRA client — provider-agnostic, no qa_pipeline/ imports
skills/                 per-agent model instructions, one *.skill.md per LLM stage
instructions/           conventions.md — shared rules prepended (cached) to every skill
profiles/<app>/         one folder of DATA per application (project.json, input/docs, input/recordings, brain/)
workspaces/<app>/       the generated Playwright repo for that profile
runs/<app>/<epic>_<story>/ per-run output: summary.md, 1_testcases/, 2_automation/,
                        3_results/, _internal/ (routing: utils/run_layout.py — you edit
                        input/, you read runs/)
```

**"The engine holds no project knowledge."** Adding a second application must
require zero changes to `qa_pipeline/`, `agents/`, or `utils/` — only a new
folder under `profiles/`. Keep this invariant when touching any of those three
packages.

**Six stages call a model** (doc_ingestor, testcase_writer, automation_planner,
coding_agent, reviewer, self_healer) and each **must** have a matching
`skills/<name>.skill.md` — a missing one is a hard `FileNotFoundError` by
design. The other seven stages (app_explorer, review_gate, jira_uploader,
framework_scaffolder, executor, bug_reporter, brain_feedback) are fully
deterministic.

**The ledger (`profiles/<app>/brain/ledger.json`) is sourced, not scraped.**
Every card carries a source and a confidence (`confirmed` 1.00 > `recorded`
0.95 > `crawled` 0.85 > `design_doc` 0.55 > `figma` 0.45 > `legacy_testcase`
0.35 > `inferred` 0.10). Only `OBSERVED` sources (`confirmed`/`recorded`/
`crawled`) may ever supply a locator to generated code — a design doc can name
a screen, never a selector. A card proved wrong is demoted (to 0.15), never
deleted.

**Configuration precedence:** `CLI flag > .env > profile (project.json) >
built-in defaults`. Per-agent `.env` vars (prefixed by the consuming agent,
e.g. `TESTCASE_WRITER_*`, shared `JIRA_*`) are **defaults, never overrides**
— resolved in `qa_pipeline/cli.py`'s `_ENV_DEFAULTS` table/`_apply_env_defaults`,
and every value taken from the environment is printed with its variable name
before use. `--story`/`--epic` deliberately stay CLI-only (never given a
default in `.env`) since a story key in a config file is the one value that
must never get silently reused across runs.

**JIRA integration** (`utils/jira_client.py`): a shared REST client (auth/
version auto-detection — v3+basic for `*.atlassian.net`, v2+bearer PAT for
self-hosted, overridable via `JIRA_AUTH_TYPE`/`JIRA_API_VERSION`). Two call
sites: `agents/testcase_writer.py` (`fetch_jira_story` for the single story
being generated; `fetch_jira_hierarchy` for the optional `JIRA_FEATURE_LINK`/
`--feature-link` walk — Feature/Epic ancestors + linked Zephyr Test issues +
their comments/attachments, folded into `jira_data["hierarchy"]` in the
generation prompt) and `agents/jira_uploader.py` (Agent 3, creates/updates
Jira Test issues — still has its own duplicated auth helpers, not yet
switched to `jira_client.py`). Any JIRA call must degrade gracefully (return
`{}`/`None`/placeholder data with a printed warning) rather than raise —
missing credentials or an unreachable issue must not crash a run that has
other usable input (design doc, Figma, API spec, ledger knowledge).

**The repair loop** (`qa_pipeline/verifiers/repair_loop.py`) wraps every model
stage: propose → verify → repair → verify → accept. The best attempt wins,
not the last; the repair prompt carries the previous attempt's actual files,
not just the diagnosis; an identical result twice in a row stops the loop
rather than paying for a retry that won't land differently.

**Test Case Writer inputs are additive, not exclusive** — story, API spec,
design doc, Figma, reference-testcases (coverage baseline), and ingested
`profiles/<app>/input/docs/` docs can all be present at once; the story is
preferred but not required (see ARCHITECTURE.md §10 for the full input table
and the `DOC-1` synthesized-story fallback).

## Working in this repo

- The big modules are split (see ARCHITECTURE.md, "Module map"). Put new code in the focused
  module (`testcase_jira`, `coding_cases`, `heal_support`, ...), not back into `testcase_writer.py`,
  `coding_agent.py`, `stages.py` or `dom_inspector.py`; a test fails if they grow past their limits.
- Path building goes through `utils/run_layout.artifact_path`; never `feature_dir / "name"`.
- A missing `skills/<agent>.skill.md` for an LLM-calling agent is a bug, not
  an edge case to handle gracefully — fix the skill file, don't add a
  fallback that lets the agent run without it.
- When changing what an LLM agent produces, update both the Python code (the
  schema it's validated against, in `qa_pipeline/contracts/`) and the
  corresponding `skills/*.skill.md` — the two drift apart silently otherwise.
- `.env` is real credentials for a live QA environment (JIRA PAT, DB
  passwords, app test user). Never print its contents verbatim; redact values
  when showing it to anyone.
- Tests in `tests/` are behavior-first: each one opens with the real failure
  it exists to prevent, not a description of the function under test. Follow
  that convention for new tests rather than writing a generic "test X calls Y".
