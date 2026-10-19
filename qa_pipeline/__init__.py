"""qa_pipeline — the Zero-Touch QA Pipeline engine.

This package is the architecture layer described in the design spec: four
layers (orchestration, agents, verifiers, grounding), with a validated
contract between every stage.

IMPORTANT — this package deliberately does NOT replace the existing
`agents/` and `utils/` packages that sit next to it. Those hold ~9,600 lines
of behavior that was earned against a real application: Windows file-lock
retries, empty-suite re-collection, ambiguous-locator detection, JSON repair
for truncated model output, provider fan-out. Rewriting them would throw all
of that away in exchange for nothing.

Instead this package:

  - WRAPS them (`qa_pipeline.agents.adapters`), validating what goes in and
    what comes out against `qa_pipeline.contracts`, so a stage that produces
    a malformed artifact fails AT that stage instead of three stages later.
  - ADDS the pieces the design calls for that did not exist: the knowledge
    ledger (`brain/`), the zero-LLM gates (`verifiers/`), the deterministic
    framework scaffolder, and the App Explorer that turns "I can't find this
    locator" into a crawl rather than a task on a human's desk.
  - REPLACES the orchestrator outright — the old `run_pipeline.py` has been
    retired, and `qa_pipeline.orchestrator` is where the stage graph, resume,
    budgets and gates now live.

THERE IS ONE PIPELINE AND ONE ENTRY POINT: `qa_pipeline.cli` (installed as
the `qa-pipeline` command). Every capability the old orchestrator had was
carried across before it was retired, including the three import modes, the
local JIRA XML story export, and the token ceiling. The `run_*_with_env.py`
wrappers are gone too — the CLI loads `.env` itself, which was the only
reason they existed.
"""

__version__ = "2.0.0-dev"

#: Bumped whenever an artifact schema in `contracts/` changes shape. The
#: orchestrator stamps it into every artifact it writes, so a resume against
#: a feature directory written by an incompatible version is detected and
#: reported rather than silently half-loaded.
CONTRACT_VERSION = 1
