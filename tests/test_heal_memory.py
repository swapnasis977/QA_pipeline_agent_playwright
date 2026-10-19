"""The Self-Healer's locator memory: it has to be reachable, and it has to
be able to give up on itself.

`locator_memory.json` is described as the biggest per-fix saving in the
pipeline: a locator this pipeline already healed and then CONFIRMED by a
passing re-run gets reapplied for free instead of costing another diagnosis.
Two things stopped that from happening.

1. IT SEARCHED THE ONE FILE THAT CANNOT CONTAIN THE BUG. The lookup ran
   against `broken_file_content` — the spec. But this pipeline's own
   conventions forbid a raw locator in a `.spec.ts` (that is what
   `coding_agent._inline_locators_in_spec` warns about, and what this agent's
   own docstring says: "THE BROKEN LOCATOR IS USUALLY NOT IN THE SPEC"). Every
   locator lives in a page object, which arrives separately in
   `related_files` — and the memory lookup never looked there. On a correctly
   generated suite the memory could essentially never hit, and every
   recurrence of an already-solved failure paid for a fresh LLM call.

2. A BAD ENTRY WAS UNKILLABLE. A remembered fix is applied through the same
   `_apply_patch` as a generated one, so it can be rejected (it doesn't
   compile in this file's context). That raised straight out of `run()`.
   Because it raised, the heal stage counted a failed attempt and
   `mark_heal_result` was never reached — so the entry was never blocklisted
   and re-poisoned every future heal that touched that file, forever, without
   ever reaching the LLM that could have fixed it properly.

Run with:  python -m pytest tests/test_heal_memory.py -q
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
_POM = "src/pages/RegisterPage.ts"

_SPEC_SOURCE = """import { test } from '@playwright/test';
test('REG-TC-001 capture', async ({ page }) => {
  await new RegisterPage(page).startCapture();
});
"""

_POM_SOURCE = """export class RegisterPage {
  readonly start = this.page.getByRole('button', { name: 'Start' });
  async startCapture() { await this.start.click(); }
}
"""


@pytest.fixture
def repo(tmp_path):
    """A workspace holding both files, as a real heal would find them."""
    for rel, source in ((_SPEC, _SPEC_SOURCE), (_POM, _POM_SOURCE)):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return tmp_path


@pytest.fixture
def feature(tmp_path):
    return tmp_path / "feature"


def _remember(feature_dir, old, new):
    feature_dir.mkdir(parents=True, exist_ok=True)
    memory = feature_dir / "_internal" / "locator_memory.json"
    memory.parent.mkdir(parents=True, exist_ok=True)
    memory.write_text(
        json.dumps([{"old_locator": old, "new_locator": new}]), encoding="utf-8")


def _no_llm(monkeypatch):
    """Any call to the model is a test failure here — the point of memory is
    that it does not make one."""
    def boom(*_a, **_kw):
        raise AssertionError("the LLM was called; the memory path should have hit")
    monkeypatch.setattr(self_healer, "call_agent", boom)


@pytest.fixture
def compiles(monkeypatch):
    """`_apply_patch` type-checks against the REAL toolchain, which cannot
    resolve `@playwright/test` inside a bare tmp_path. That is the syntax
    gate working correctly and is covered by its own test below; here it is
    noise that would mask what these tests are actually about."""
    monkeypatch.setattr(self_healer, "_check_syntax_status",
                        lambda *_a, **_kw: (_SYNTAX_PASS, ""))


# --- 1. the memory must reach the page objects ----------------------------

def test_a_remembered_fix_is_found_in_an_imported_page_object(repo, feature, compiles, monkeypatch):
    _no_llm(monkeypatch)
    _remember(feature, "getByRole('button', { name: 'Start' })",
              "getByRole('button', { name: 'Start Capture' })")

    result = self_healer.run(
        broken_file_content=_SPEC_SOURCE, failure_reason="Timeout waiting for locator",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        related_files={_POM: _POM_SOURCE})

    assert result["from_memory"] is True
    # It must say WHICH file it patched, or the caller writes the spec back
    # over a page-object fix.
    assert result["patched_file_path"] == _POM
    assert "Start Capture" in (repo / _POM).read_text(encoding="utf-8")
    # ...and the spec is untouched.
    assert (repo / _SPEC).read_text(encoding="utf-8") == _SPEC_SOURCE


def test_a_locator_genuinely_in_the_spec_still_heals_there(repo, feature, compiles, monkeypatch):
    """The pre-existing behaviour must not regress."""
    _no_llm(monkeypatch)
    _remember(feature, "new RegisterPage(page)", "new RegistrationPage(page)")

    result = self_healer.run(
        broken_file_content=_SPEC_SOURCE, failure_reason="boom",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        related_files={_POM: _POM_SOURCE})

    assert result["patched_file_path"] == _SPEC
    assert "RegistrationPage" in (repo / _SPEC).read_text(encoding="utf-8")


def test_the_spec_is_searched_before_the_page_objects(repo, feature, compiles, monkeypatch):
    """Determinism: the file under diagnosis wins a tie, so the same failure
    heals the same way every run."""
    _no_llm(monkeypatch)
    _remember(feature, "RegisterPage", "RegistrationPage")

    result = self_healer.run(
        broken_file_content=_SPEC_SOURCE, failure_reason="boom",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        related_files={_POM: _POM_SOURCE})

    assert result["patched_file_path"] == _SPEC


def test_a_blocklisted_entry_is_not_reused(repo, feature, monkeypatch):
    _remember(feature, "getByRole('button', { name: 'Start' })", "getByTestId('start')")
    blocklist = feature / "_internal" / "locator_memory_failed.json"
    blocklist.parent.mkdir(parents=True, exist_ok=True)
    blocklist.write_text(
        json.dumps(["getByRole('button', { name: 'Start' })::getByTestId('start')"]),
        encoding="utf-8")

    calls = []
    monkeypatch.setattr(self_healer, "call_agent",
                        lambda **kw: calls.append(kw) or '{"patched_content": null, '
                                                        '"unresolved_reason": "no"}')
    monkeypatch.setattr(self_healer, "inspect_current_dom", lambda _c: {"elements": []})

    self_healer.run(
        broken_file_content=_SPEC_SOURCE, failure_reason="boom",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        related_files={_POM: _POM_SOURCE})

    assert calls, "a blocklisted memory entry must fall through to the LLM"


# --- 2. a rejected memory patch must self-blocklist, not raise ------------

def test_a_remembered_fix_that_does_not_compile_blocklists_itself(
        repo, feature, monkeypatch):
    """It must neither be applied nor left in memory to fail again — and the
    heal must continue to the LLM rather than dying on the way there."""
    _remember(feature, "getByRole('button', { name: 'Start' })", "!!! not typescript !!!")

    # Stand in for a real tsc rejection of the patched file.
    monkeypatch.setattr(self_healer, "_check_syntax_status",
                        lambda *_a, **_kw: (self_healer._SYNTAX_FAIL, "TS1005"))
    monkeypatch.setattr(self_healer, "inspect_current_dom", lambda _c: {"elements": []})
    monkeypatch.setattr(self_healer, "call_agent",
                        lambda **_kw: '{"patched_content": null, '
                                      '"unresolved_reason": "needs a human"}')

    result = self_healer.run(
        broken_file_content=_SPEC_SOURCE, failure_reason="boom",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        related_files={_POM: _POM_SOURCE})

    # The page object was restored, not left corrupt.
    assert (repo / _POM).read_text(encoding="utf-8") == _POM_SOURCE
    # The entry is blocklisted, so the next heal does not retry it.
    blocked = json.loads((feature / "_internal" / "locator_memory_failed.json").read_text(encoding="utf-8"))
    assert any("not typescript" in key for key in blocked)
    # And the heal reached the LLM instead of raising on the way.
    assert result["declined"] is True
