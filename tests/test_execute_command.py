"""`execute` must re-run the existing suite in parallel with no model and no API key.

The failures this prevents: needing the whole `run` pipeline (and its API key) just to re-run
tests that already exist; running on one worker because nothing set the count; and a red suite
finishing with exit 0 so CI goes green.
"""

from __future__ import annotations

from types import SimpleNamespace

from qa_pipeline import execute_suite


class _Profile:
    def __init__(self, tmp_path, workers=None):
        self.workspace = tmp_path / "ws"
        self.context_root = tmp_path / "runs"
        self._workers = workers
        self.workspace.mkdir(exist_ok=True)
        (self.workspace / "playwright.config.ts").write_text("export default {};")

    def get(self, key, default=None):
        return {"execution.workers": self._workers}.get(key, default)


def _args(**over):
    base = dict(workers=None, shard=None, grep=None, retries=0, allure=False, allow_failures=False)
    base.update(over)
    return SimpleNamespace(**base)


def _result(*statuses):
    return {"ran": True, "cases": [{"test_case_id": f"TC-{i}", "status": s, "error_message": "\x1b[31mboom"}
                                    for i, s in enumerate(statuses)]}


def test_parallel_workers_default_to_four_and_the_profile_can_change_it(tmp_path):
    assert execute_suite.resolve_workers(None, _Profile(tmp_path)) == 4
    assert execute_suite.resolve_workers(None, _Profile(tmp_path, workers=2)) == 2
    assert execute_suite.resolve_workers(6, _Profile(tmp_path, workers=2)) == 6


def test_the_suite_is_run_with_the_workers_and_no_model(tmp_path, monkeypatch):
    seen = {}

    def fake_execute(repo, feature, **kwargs):
        seen.update(kwargs)
        return _result("passed", "passed")

    monkeypatch.setattr("qa_pipeline.agents.adapters.execute", fake_execute)
    code = execute_suite.cmd_execute(_args(shard="1/3"), _Profile(tmp_path))
    assert code == 0
    assert seen["workers"] == 4 and seen["shard"] == "1/3" and seen["retries"] == 0
    assert seen["fresh_reports"] is True


def test_a_red_suite_exits_6_unless_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute", lambda *a, **k: _result("passed", "failed"))
    assert execute_suite.cmd_execute(_args(), _Profile(tmp_path)) == execute_suite.EXIT_TESTS_FAILING
    assert execute_suite.cmd_execute(_args(allow_failures=True), _Profile(tmp_path)) == 0


def test_a_suite_that_could_not_start_is_a_failure_not_a_pass(tmp_path, monkeypatch):
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute",
                        lambda *a, **k: {"ran": False, "error": "npx not found", "cases": []})
    assert execute_suite.cmd_execute(_args(), _Profile(tmp_path)) == execute_suite.EXIT_FAILED


def test_a_missing_framework_is_explained(tmp_path, capsys):
    profile = _Profile(tmp_path)
    (profile.workspace / "playwright.config.ts").unlink()
    assert execute_suite.cmd_execute(_args(), profile) == execute_suite.EXIT_FAILED
    assert "scaffold" in capsys.readouterr().out


def test_a_targeted_rerun_keeps_the_existing_reports(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute",
                        lambda repo, feature, **k: seen.update(k) or _result("passed"))
    execute_suite.cmd_execute(_args(grep=["TC-1"]), _Profile(tmp_path))
    assert seen["fresh_reports"] is False and seen["grep_ids"] == ["TC-1"]


def test_failure_lines_have_no_colour_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("qa_pipeline.agents.adapters.execute", lambda *a, **k: _result("failed"))
    execute_suite.cmd_execute(_args(), _Profile(tmp_path))
    out = capsys.readouterr().out
    assert "FAILED TC-0: boom" in out and "\x1b" not in out
