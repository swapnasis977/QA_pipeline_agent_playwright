"""Every module must import, and the stage graph must build.

WHY THIS EXISTS. `agents/testcase_writer.py` once shipped with an unterminated
string literal. It was not a subtle bug — the file could not be parsed at all —
but nothing caught it until a real run died at stage 3, because the first thing
that imports the agents is the pipeline itself. A syntax error in ANY agent
takes down the whole CLI, including commands that never touch that agent.

These tests cost milliseconds and close that entire class of failure. They need
no browser, no network, no API key and no live environment, so they can run in
CI on every commit — which is precisely where the missing check belonged.

Run with:  python -m pytest tests/test_import_smoke.py -q
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _module_names(package_name: str) -> list:
    package = importlib.import_module(package_name)
    return sorted(
        f"{package_name}.{m.name}"
        for m in pkgutil.iter_modules(package.__path__)
        if not m.name.startswith("_")
    )


@pytest.mark.parametrize("module_name", _module_names("agents"))
def test_every_agent_module_imports(module_name):
    """A syntax error in one agent breaks every command, not just its own."""
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _module_names("qa_pipeline.brain"))
def test_every_brain_module_imports(module_name):
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _module_names("qa_pipeline.verifiers"))
def test_every_verifier_module_imports(module_name):
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _module_names("qa_pipeline.orchestrator"))
def test_every_orchestrator_module_imports(module_name):
    importlib.import_module(module_name)


@pytest.mark.parametrize("module_name", _module_names("utils"))
def test_every_util_module_imports(module_name):
    importlib.import_module(module_name)


def test_cli_parser_builds():
    """The CLI imports every command's module lazily, so a broken agent shows
    up here as a parser that cannot even be constructed."""
    from qa_pipeline.cli import build_parser
    parser = build_parser()
    assert parser is not None


def test_stage_graph_builds_and_is_ordered():
    """The graph is fixed and deterministic — that is the design's core claim.

    Asserting the exact names in order means a stage cannot be renamed,
    dropped or reordered without a test saying so out loud.
    """
    from qa_pipeline.orchestrator import build_pipeline
    stages = build_pipeline().stages
    assert [s.name for s in stages] == [
        "doc_ingestor",
        "app_explorer",
        "testcase_writer",
        "review_gate",
        "jira_uploader",
        "framework_scaffolder",
        "automation_planner",
        "coding_agent",
        "executor",
        "reviewer",
        "self_healer",
        "bug_reporter",
        "brain_feedback",
    ]


def test_every_gate_is_exposed():
    import qa_pipeline.verifiers as verifiers
    for gate in ("gate_code", "gate_plan", "gate_test_cases"):
        assert callable(getattr(verifiers, gate))


def test_every_skill_file_referenced_by_an_agent_exists():
    """A missing skill file fails at call time, deep inside a paid run."""
    skills_dir = _ROOT / "skills"
    assert skills_dir.is_dir(), "skills/ directory is missing"
    for expected in ("testcase_writer", "automation_planner", "coding_agent",
                     "reviewer", "self_healer", "doc_ingestor"):
        assert (skills_dir / f"{expected}.skill.md").is_file(), (
            f"skills/{expected}.skill.md is missing — the agent that loads it "
            f"will fail at call time, after the run has already paid for "
            f"everything upstream of it")
