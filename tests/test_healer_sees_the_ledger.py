"""The Self-Healer must be shown the ledger's verified locators.

Every other agent is grounded in the ledger. The Planner is handed
`catalogue.render(ledger)` — names plus real selectors — precisely so it
selects from a closed list instead of inventing. The Coding Agent gets
POM_SUMMARY.md and the confirmed fixes from locator_memory.json.

The Self-Healer got none of it. Its entire job is to find a correct locator,
and it was the one agent with no access to the 640-card store that exists to
hold correct locators. It saw only the broken file, the failure text, and one
DOM snapshot — and when that snapshot came back empty it had nothing left.

Observed on a real run. The healer declined, and it was right to:

    "The snapshot provides no element data to verify the correct Registration
     button locator ... A patch here would be a guess."

Meanwhile the ledger held that element, crawled and confirmed:

    Registration. Click to access Registration module
      -> page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")

and locator_memory.json held the same fix, already proven by a passing re-run.
The answer was in the building. Nobody handed it over.

Run with:  python -m pytest tests/test_healer_sees_the_ledger.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from agents import self_healer  # noqa: E402
from agents.coding_agent import _SYNTAX_PASS  # noqa: E402

_SPEC = "tests/e2e/register.spec.ts"
_SOURCE = "const tile = page.getByRole('button', { name: 'Registration. Click to access' });\n"

_CATALOGUE = (
    "ELEMENT VOCABULARY — verified selectors.\n"
    "Dashboard:\n"
    "  - Registration. Click to access Registration module "
    '[page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")]\n'
)


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / _SPEC
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_SOURCE, encoding="utf-8")
    return tmp_path


@pytest.fixture
def captured(monkeypatch):
    """Captures the prompt instead of calling a model."""
    box = {}

    def fake(**kwargs):
        box.update(kwargs)
        return json.dumps({"patched_content": _SOURCE.replace(
            "getByRole('button', { name: 'Registration. Click to access' })",
            'locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")')})

    monkeypatch.setattr(self_healer, "call_agent", fake)
    monkeypatch.setattr(self_healer, "inspect_current_dom", lambda _c: {"elements": []})
    monkeypatch.setattr(self_healer, "_check_syntax_status",
                        lambda *_a, **_kw: (_SYNTAX_PASS, ""))
    return box


def test_the_ledger_catalogue_reaches_the_healers_prompt(repo, tmp_path, captured):
    self_healer.run(
        broken_file_content=_SOURCE, failure_reason="locator not found",
        feature_dir=tmp_path / "feat", repo_output_dir=repo, file_path=_SPEC,
        known_locators=_CATALOGUE)

    prompt = captured["user_prompt"]
    assert "section > section > div:nth-of-type(1)" in prompt
    assert "Registration. Click to access Registration module" in prompt


def test_the_catalogue_is_labelled_as_verified_ground_truth(repo, tmp_path, captured):
    """It must be distinguishable from the DOM snapshot, which can be empty
    or of the wrong screen — otherwise the healer weighs them the same."""
    self_healer.run(
        broken_file_content=_SOURCE, failure_reason="locator not found",
        feature_dir=tmp_path / "feat", repo_output_dir=repo, file_path=_SPEC,
        known_locators=_CATALOGUE)

    prompt = captured["user_prompt"].lower()
    assert "verified" in prompt or "confirmed" in prompt


def test_omitting_it_changes_nothing_for_a_caller_that_has_no_ledger(repo, tmp_path, captured):
    """A direct `python agents/self_healer.py` invocation has no ledger, and
    must behave exactly as before rather than being handed an empty heading
    it would then treat as 'no elements exist'."""
    self_healer.run(
        broken_file_content=_SOURCE, failure_reason="locator not found",
        feature_dir=tmp_path / "feat", repo_output_dir=repo, file_path=_SPEC)

    assert "ELEMENT VOCABULARY" not in captured["user_prompt"]
