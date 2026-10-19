"""Regression tests for the orchestrator/verifier/brain review fixes.

Each test opens with the real failure it prevents.
"""

from __future__ import annotations

import types

from qa_pipeline.agents import adapters
from qa_pipeline.brain import cards as card_builders
from qa_pipeline.brain import catalogue, ingest, trust, writeback
from qa_pipeline.brain.store import Ledger
from qa_pipeline.orchestrator import budget, code_support, demo_support, review_support
from qa_pipeline.verifiers import VerifyResult, locator_check, repair_loop

WHEN = "2026-01-01T00:00:00Z"


def _el(ledger, screen, name, locator, source, **kw):
    return ledger.upsert(card_builders.element(
        screen=screen, logical_name=name, locator=locator, source=source,
        role=kw.get("role", "button"), accessible_name=kw.get("accessible_name", name),
        unique=True, evidence="t", when=WHEN))


# --- 2. ledger trust leak ---------------------------------------------------

def test_a_green_run_does_not_confirm_a_design_doc_card_with_a_different_locator(tmp_path):
    """promote() changed source only, so a design_doc card with an empty locator
    became `confirmed` at 1.0 while the passing test used another locator."""
    ledger = Ledger(tmp_path)
    _el(ledger, "Login", "Sign in", "", "design_doc")
    gen = {"path": "tests/a.spec.ts", "test_case_ids": ["T-1"],
           "content": "await page.getByRole('button', { name: 'Sign in' }).click();"}
    run = {"cases": [{"file": "tests/a.spec.ts", "status": "passed"}]}
    plans = [{"test_case_id": "T-1", "elements": [{"screen": "Login"}]}]
    writeback.from_run(ledger, run, [gen], plans)
    card = ledger.find_element("Login", "Sign in")
    assert card["source"] == "confirmed"
    assert "getByRole" in card["value"]["locator"]


def test_a_disproved_card_is_never_promoted_and_loses_to_a_fresh_crawl(tmp_path):
    ledger = Ledger(tmp_path)
    old = _el(ledger, "S", "Save", "#old", "recorded")
    ledger.demote(old["id"], "wrong")
    fresh = card_builders.element(
        screen="S", logical_name="Save", locator="#new", source="crawled",
        role="button", accessible_name="Save", unique=True, evidence="t", when=WHEN)
    assert trust.resolve(ledger.get(old["id"]), fresh) is fresh
    ledger.upsert(fresh)
    assert ledger.find_element("S", "Save")["value"]["locator"] == "#new"


# --- 4. locator name matching -----------------------------------------------

def test_a_written_name_longer_than_the_real_one_does_not_pass_the_gate():
    """Playwright matches the written name as a substring of the REAL name, so
    'Save changes now' can never find a button named 'Save changes'."""
    card = {"value": {"accessible_name": "Save changes", "logical_name": "Save"}}
    assert locator_check._name_would_match("save", card)
    assert not locator_check._name_would_match("Save changes now", card)


# --- 7. repair loop ---------------------------------------------------------

def _res(n):
    return VerifyResult.failed("g", [f"e{i}" for i in range(n)], repairs=[f"fix{n}"])


def test_repair_prompt_describes_the_best_attempt_not_a_regressed_one():
    prompts = []
    results = iter([_res(1), _res(5), _res(5)])

    def produce(repair):
        prompts.append(repair)
        return ["x"]

    repair_loop.run(produce, lambda _a: next(results), max_repairs=2, verbose=False)
    assert prompts[1] == _res(1).repair_prompt()
    assert prompts[2] == _res(1).repair_prompt()


def test_a_declining_repair_keeps_the_earlier_paid_for_attempt():
    outputs = iter([["good"], {"declined": True, "decline_reason": "no"}])
    out = repair_loop.run(lambda r: next(outputs), lambda a: _res(2),
                          max_repairs=2, verbose=False)
    assert out.artifact == ["good"] and not out.declined


def test_a_spent_budget_stops_the_loop_before_a_repair_call():
    calls = []
    repair_loop.run(lambda r: calls.append(r) or ["x"], lambda a: _res(1),
                    max_repairs=3, verbose=False, should_stop=lambda: "budget")
    assert len(calls) == 1


# --- 9. memoised gate -------------------------------------------------------

def test_the_same_generated_files_are_gated_once():
    count = []
    verify = code_support.memoised_verify(lambda files: count.append(1) or [])
    files = [{"path": "a", "content": "x"}]
    verify(files)
    verify([dict(files[0])])
    assert len(count) == 1
    verify([{"path": "a", "content": "y"}])
    assert len(count) == 2


def test_a_skipped_gate_is_reported_not_silent():
    result = VerifyResult.skipped("compile_check", "no tsc")
    outcome = repair_loop.RepairOutcome([], result, 1)
    said = []
    notes = code_support.report_gate_outcome(outcome, say=said.append)
    assert notes and "SKIPPED" in said[0]


# --- 10. classification confidence -----------------------------------------

def test_a_word_for_confidence_does_not_discard_the_review():
    out = adapters.normalize_classification(
        {"test_case_id": "T-1", "verdict": "genuine_bug", "confidence": "high"})
    assert out["confidence"] == 0.7


# --- 6. reviewer skipped when green ----------------------------------------

def test_no_reviewer_call_is_needed_when_nothing_failed():
    green = {"cases": [{"status": "passed"}], "stats": {"unexpected": 0}}
    red = {"cases": [{"status": "failed"}]}
    assert review_support.nothing_to_review(green, {})
    assert not review_support.nothing_to_review(red, {})
    assert not review_support.nothing_to_review({"cases": []}, {})


# --- 5. catalogue scope -----------------------------------------------------

def test_heal_vocabulary_is_limited_to_the_named_screens_case_insensitively(tmp_path):
    ledger = Ledger(tmp_path)
    _el(ledger, "dashboard", "Search", "#s", "crawled")
    _el(ledger, "Other", "Elsewhere", "#e", "crawled")
    scoped = catalogue.render(ledger, screens=["Dashboard"], only_screens=True)
    assert "Search" in scoped and "Elsewhere" not in scoped
    assert "Elsewhere" in catalogue.render(ledger, screens=["Dashboard"])


# --- 3. ingest manifest -----------------------------------------------------

def test_an_unchanged_document_is_not_sent_to_the_model_again(tmp_path, monkeypatch):
    calls = []

    def fake_call(*a, **k):
        calls.append(1)
        return '{"cards": [{"type": "glossary", "tester_word": "tile", "ui_label": "button"}]}'

    monkeypatch.setattr(ingest, "call_agent", fake_call)
    monkeypatch.setattr(ingest, "parse_json_response",
                        lambda raw, *a: __import__("json").loads(raw))
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("hello", encoding="utf-8")
    ledger = Ledger(tmp_path / "brain")
    ingest.from_documents(ledger, docs, tmp_path, verbose=False)
    ingest.from_documents(ledger, docs, tmp_path, verbose=False)
    assert len(calls) == 1
    (docs / "a.md").write_text("changed", encoding="utf-8")
    ingest.from_documents(ledger, docs, tmp_path, verbose=False)
    assert len(calls) == 2


# --- 1. demonstration can be cleared by re-running -------------------------

def test_a_recording_added_after_planning_clears_the_demonstration_request(tmp_path):
    """Exit 3 said 're-run the same command', but the stale plan state was
    reloaded and the recording never absorbed."""
    ledger = Ledger(tmp_path / "brain")
    feature = tmp_path / "feat"
    (feature / "_internal").mkdir(parents=True)
    ctx = types.SimpleNamespace(
        ledger=ledger, feature_dir=feature,
        artifact=lambda name: feature / "_internal" / name)
    needs = ctx.artifact("needs_demonstration.json")
    needs.write_text("[]", encoding="utf-8")
    item = {"test_case_id": "T-1", "logical_name": "Save", "screen": "S", "reason": "x"}
    plan_state = {"plans": [{"test_case_id": "T-1", "target_file": "tests/e2e/a.spec.ts",
                             "test_kind": "e2e", "elements": [
        {"logical_name": "Save", "screen": "S", "locator": ""}]}],
        "unresolved": [item]}

    def absorb(_ctx):  # stands in for _absorb_recordings finding the new recording
        _el(ledger, "S", "Save", "getByRole('button', { name: 'Save' })", "recorded")

    still = demo_support.settle_demonstrations(ctx, plan_state, absorb)
    assert still == [] and plan_state["unresolved"] == []
    assert plan_state["plans"][0]["elements"][0]["locator"]
    assert not needs.exists()


# --- 8. budget survives a resume -------------------------------------------

def test_a_resume_measures_tokens_from_the_original_run_start(tmp_path):
    from qa_pipeline.orchestrator import context as context_mod
    ctx = object.__new__(context_mod.RunContext)
    ctx.feature_dir = tmp_path
    ctx.run_id = "r1"
    ctx.started = "2026-01-01T00:00:00Z"
    ctx.mark_done("doc_ingestor")
    resumed = object.__new__(context_mod.RunContext)
    resumed.feature_dir = tmp_path
    resumed.run_id = "r2"
    resumed.started = "2026-01-02T00:00:00Z"
    assert resumed.run_started() == "2026-01-01T00:00:00Z"
    assert budget.over_budget(types.SimpleNamespace(
        max_run_tokens=0, profile=None)) == ""
