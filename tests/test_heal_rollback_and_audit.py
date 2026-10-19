"""A heal that does not work must leave no trace on the test, and a heal that does
work must leave an audit trail.

Failures these prevent:
  - A failed heal stayed on disk. Attempt 2 then patched attempt 1's patch, the
    "must not prove less" guard compared against that intermediate text, and a
    case that stayed red was left carrying an LLM-modified spec.
  - heal_outcomes.json held only the model's own claim of what it changed - no
    diff - so nobody could check what a heal did to a test.
  - old/new locator accumulated across attempts, so a swap that FAILED and was
    blocklisted ended up recorded as the verified fix and the ledger confirmed it.
  - A group heal "passed" when one of its cases silently did not run.
"""

from types import SimpleNamespace

import pytest

from qa_pipeline.orchestrator import stages

SPEC = "tests/e2e/a.spec.ts"
ORIGINAL = "test('PROJ-1-TC-001 x', async () => {\n  await a.click();\n  await expect(b).toHaveText('Saved');\n});\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "tests" / "e2e").mkdir(parents=True)
    (repo / SPEC).write_text(ORIGINAL, encoding="utf-8")

    ctx = SimpleNamespace(
        repo_dir=repo, feature_dir=tmp_path / "run", workers=None,
        metrics=SimpleNamespace(data={}), ledger=None,
        profile=SimpleNamespace(get=lambda key, default=None: default),
        artifact=lambda name: stages.artifact_path(tmp_path / "run", name),
        state={
            "reviewer": {"classifications": [{
                "test_case_id": "PROJ-1-TC-001", "verdict": "fixable_test_issue",
                "file": SPEC, "reason": "locator", "suggested_fix": ""}]},
            "coding_agent": {"files": [{"path": SPEC, "kind": "spec", "content": ORIGINAL,
                                        "test_case_ids": ["PROJ-1-TC-001"]}]},
            "automation_planner": {"plans": []},
            "executor": {"ran": True, "cases": []},
        })
    monkeypatch.setattr(stages.catalogue, "render", lambda *a, **k: "")
    monkeypatch.setattr(stages, "read_json", lambda *a, **k: {})
    return ctx, repo


def _healer_writing(repo, updates):
    """A fake Self-Healer that applies one scripted patch per attempt."""
    calls = iter(updates)

    def fake_run(**kwargs):
        edit = next(calls)
        (repo / SPEC).write_text(edit["content"], encoding="utf-8")
        return {"patched_content": edit["content"], "patched_file_path": SPEC,
                "locator_memory_update": edit.get("update", {})}
    return fake_run


def _executor(results):
    outcomes = iter(results)

    def fake_execute(*a, **k):
        passed = next(outcomes)
        return {"ran": True, "returncode": 0 if passed else 1, "error": "",
                "cases": [{"test_case_id": "PROJ-1-TC-001",
                           "status": "passed" if passed else "failed"}]}
    return fake_execute


def test_a_heal_that_never_passes_is_rolled_back_to_the_original(world, monkeypatch):
    ctx, repo = world
    monkeypatch.setattr(stages._self_healer, "run", _healer_writing(repo, [
        {"content": ORIGINAL.replace("a.click", "a.dblclick")},
        {"content": ORIGINAL.replace("a.click", "a.hover")}]))
    monkeypatch.setattr(stages.adapters, "execute", _executor([False, False]))

    result = stages.stage_heal(ctx)

    assert (repo / SPEC).read_text(encoding="utf-8") == ORIGINAL
    assert result["still_failing"] == ["PROJ-1-TC-001"]


def test_every_attempt_leaves_a_diff_a_person_can_read(world, monkeypatch):
    ctx, repo = world
    monkeypatch.setattr(stages._self_healer, "run", _healer_writing(repo, [
        {"content": ORIGINAL.replace("a.click", "a.dblclick")},
        {"content": ORIGINAL.replace("a.click", "a.hover")}]))
    monkeypatch.setattr(stages.adapters, "execute", _executor([False, False]))

    stages.stage_heal(ctx)

    heals = ctx.feature_dir / "3_results" / "heals"
    names = sorted(p.name for p in heals.iterdir())
    assert names == ["PROJ-1-TC-001.attempt1.failed.diff", "PROJ-1-TC-001.attempt2.failed.diff"]
    text = (heals / names[0]).read_text(encoding="utf-8")
    assert "-  await a.click();" in text and "+  await a.dblclick();" in text


def test_the_second_attempt_starts_from_the_original_not_the_failed_patch(world, monkeypatch):
    ctx, repo = world
    seen = []

    def fake_run(**kwargs):
        seen.append(kwargs["broken_file_content"])
        content = ORIGINAL.replace("a.click", f"a.try{len(seen)}")
        (repo / SPEC).write_text(content, encoding="utf-8")
        return {"patched_content": content, "patched_file_path": SPEC,
                "locator_memory_update": {}}

    monkeypatch.setattr(stages._self_healer, "run", fake_run)
    monkeypatch.setattr(stages.adapters, "execute", _executor([False, False]))

    stages.stage_heal(ctx)

    assert seen == [ORIGINAL, ORIGINAL]


def test_a_failed_swap_is_not_recorded_as_the_verified_fix(world, monkeypatch):
    ctx, repo = world
    monkeypatch.setattr(stages._self_healer, "run", _healer_writing(repo, [
        {"content": ORIGINAL.replace("a.click", "x.click"),
         "update": {"old_locator": "a", "new_locator": "x"}},     # attempt 1: fails
        {"content": ORIGINAL.replace("a.click", "a.blurThenClick")}]))  # attempt 2: passes, no swap
    monkeypatch.setattr(stages.adapters, "execute", _executor([False, True]))

    result = stages.stage_heal(ctx)

    outcome = result["outcomes"][0]
    assert result["healed"] == ["PROJ-1-TC-001"]
    assert outcome["old_locator"] == "" and outcome["new_locator"] == ""


def test_a_verified_heal_keeps_its_patch_and_its_own_locator_claim(world, monkeypatch):
    ctx, repo = world
    patched = ORIGINAL.replace("a.click", "x.click")
    monkeypatch.setattr(stages._self_healer, "run", _healer_writing(repo, [
        {"content": patched, "update": {"old_locator": "a", "new_locator": "x"}}]))
    monkeypatch.setattr(stages.adapters, "execute", _executor([True]))

    result = stages.stage_heal(ctx)

    assert (repo / SPEC).read_text(encoding="utf-8") == patched
    assert result["outcomes"][0]["new_locator"] == "x"
    assert (ctx.feature_dir / "3_results" / "heals" / "PROJ-1-TC-001.attempt1.verified.diff").is_file()


def test_a_group_where_one_case_did_not_run_is_not_a_pass():
    rerun = {"returncode": 0, "cases": [{"test_case_id": "A-1", "status": "passed"}]}
    assert stages.heal_succeeded(rerun, ["A-1", "A-2"]) is False


def test_other_specs_that_were_passing_and_now_fail_are_regressions():
    baseline = {"cases": [{"test_case_id": "B-1", "status": "passed"},
                          {"test_case_id": "B-2", "status": "failed"}]}
    rerun = {"cases": [{"test_case_id": "A-1", "status": "passed"},
                       {"test_case_id": "B-1", "status": "failed"},
                       {"test_case_id": "B-2", "status": "failed"}]}
    # B-2 was already red, so it is not a regression; B-1 is.
    assert stages._regressions(rerun, ["A-1"], baseline) == ["B-1"]


def test_specs_importing_the_patched_page_object_are_found():
    files = [{"path": "tests/e2e/a.spec.ts", "kind": "spec", "content": "import { P } from '@pages/Shared';"},
             {"path": "tests/e2e/b.spec.ts", "kind": "spec", "content": "import { P } from '@pages/Shared';"},
             {"path": "tests/e2e/c.spec.ts", "kind": "spec", "content": "import { Q } from '@pages/Other';"}]
    assert stages._dependent_specs(files, "tests/e2e/a.spec.ts", "src/pages/Shared.ts") == [
        "tests/e2e/b.spec.ts"]


def test_a_heal_stage_past_its_time_budget_attempts_nothing_more(world, monkeypatch):
    ctx, repo = world
    ctx.profile = type("P", (), {"get": lambda self, key, default=None:
                                 0.001 if key == "budgets.max_run_seconds" else default})()
    monkeypatch.setattr(stages.time, "monotonic", iter([0.0, 5.0, 5.0]).__next__)
    monkeypatch.setattr(stages._self_healer, "run",
                        lambda **k: (_ for _ in ()).throw(AssertionError("must not be attempted")))

    result = stages.stage_heal(ctx)

    assert result["still_failing"] == ["PROJ-1-TC-001"]
    assert "time budget" in result["outcomes"][0]["decline_reason"]


def test_without_a_time_budget_nothing_is_cut_short(world, monkeypatch):
    ctx, repo = world
    ctx.profile = type("P", (), {"get": lambda self, key, default=None: default})()
    monkeypatch.setattr(stages._self_healer, "run", _healer_writing(repo, [
        {"content": ORIGINAL.replace("a.click", "a.hover")}]))
    monkeypatch.setattr(stages.adapters, "execute", _executor([True]))

    assert stages.stage_heal(ctx)["healed"] == ["PROJ-1-TC-001"]


def test_the_profile_decides_how_many_heal_attempts_are_made(world, monkeypatch):
    ctx, repo = world
    ctx.profile = type("P", (), {"get": lambda self, key, default=None:
                                 1 if key == "budgets.max_heal_attempts" else default})()
    calls = []

    def fake_run(**kwargs):
        calls.append(kwargs["attempt"])
        content = ORIGINAL.replace("a.click", f"a.try{len(calls)}")
        (repo / SPEC).write_text(content, encoding="utf-8")
        return {"patched_content": content, "patched_file_path": SPEC, "locator_memory_update": {}}

    monkeypatch.setattr(stages._self_healer, "run", fake_run)
    monkeypatch.setattr(stages.adapters, "execute", _executor([False, False]))

    stages.stage_heal(ctx)

    assert calls == [1]            # one attempt, as configured - not the old hard-coded two
