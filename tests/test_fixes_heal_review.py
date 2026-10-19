"""Regression tests for the heal / review / report / upload fixes.

Each test opens with the real failure it prevents.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import openpyxl  # noqa: E402
import pytest  # noqa: E402

from agents import bug_reporter, jira_uploader, reviewer, self_healer  # noqa: E402
from agents.coding_agent import _SYNTAX_PASS  # noqa: E402
from qa_pipeline import decline  # noqa: E402
from qa_pipeline.orchestrator import review_support  # noqa: E402

# --- self_healer ------------------------------------------------------------

_SPEC = "tests/e2e/register.spec.ts"
_POM = "src/pages/RegisterPage.ts"
_SPEC_SRC = "test('TC-1 capture', async ({ page }) => { await page.goto('/'); });\n"
_GEN_POM = ("/**\n * Register — GENERATED from the Planner's grounded locators.\n */\n"
            "export class RegisterPage { start = this.page.getByTestId('start-capture-button'); }\n")


def _remember(feature_dir, old, new):
    path = feature_dir / "_internal" / "locator_memory.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"old_locator": old, "new_locator": new}]), encoding="utf-8")


def _repo(tmp_path, pom_source):
    for rel, src in ((_SPEC, _SPEC_SRC), (_POM, pom_source)):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(src, encoding="utf-8")
    return tmp_path


def _llm_declines(monkeypatch):
    calls = []
    monkeypatch.setattr(self_healer, "call_agent", lambda **kw: calls.append(kw) or
                        '{"patched_content": null, "unresolved_reason": "no"}')
    monkeypatch.setattr(self_healer, "_check_syntax_status", lambda *_a, **_k: (_SYNTAX_PASS, ""))
    return calls


def test_a_remembered_fix_never_rewrites_a_generated_page_object(tmp_path, monkeypatch):
    """The LLM path refuses to patch generated files (rebuilt every run), but the
    remembered-fix path wrote into them, so the fix was silently lost next run."""
    repo, feature = _repo(tmp_path, _GEN_POM), tmp_path / "feature"
    _remember(feature, "getByTestId('start-capture-button')", "getByTestId('start')")
    calls = _llm_declines(monkeypatch)

    result = self_healer.run(
        broken_file_content=_SPEC_SRC, failure_reason="timeout", feature_dir=feature,
        repo_output_dir=repo, file_path=_SPEC, related_files={_POM: _GEN_POM},
        failure_dom={"elements": [], "url": "x"})

    assert (repo / _POM).read_text(encoding="utf-8") == _GEN_POM
    assert not result.get("from_memory") and calls


def test_a_short_generic_remembered_locator_is_not_applied_blindly(tmp_path, monkeypatch):
    """A remembered 'button' matched anywhere in any file; the failure never mentioned it."""
    spec = "// the button is blue\ntest('TC-1', async () => { await page.getByRole('button').click(); });\n"
    repo, feature = _repo(tmp_path, "export class P {}\n"), tmp_path / "feature"
    (repo / _SPEC).write_text(spec, encoding="utf-8")
    _remember(feature, "button", "link")
    calls = _llm_declines(monkeypatch)

    result = self_healer.run(
        broken_file_content=spec, failure_reason="Timeout waiting for the Start Capture control",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC,
        failure_dom={"elements": [], "url": "x"})

    assert not result.get("from_memory") and calls
    assert (repo / _SPEC).read_text(encoding="utf-8") == spec


def test_a_short_locator_the_failure_names_is_still_applied(tmp_path, monkeypatch):
    spec = "test('TC-1', async () => { await page.locator('#go').click(); });\n"
    repo, feature = _repo(tmp_path, "export class P {}\n"), tmp_path / "feature"
    (repo / _SPEC).write_text(spec, encoding="utf-8")
    _remember(feature, "#go", "#go-now")
    monkeypatch.setattr(self_healer, "_check_syntax_status", lambda *_a, **_k: (_SYNTAX_PASS, ""))
    monkeypatch.setattr(self_healer, "call_agent",
                        lambda **_k: (_ for _ in ()).throw(AssertionError("LLM called")))

    result = self_healer.run(
        broken_file_content=spec, failure_reason="locator('#go') not found",
        feature_dir=feature, repo_output_dir=repo, file_path=_SPEC)

    assert result["from_memory"] is True


def test_the_dom_sent_to_the_healer_is_capped_with_interactive_elements_first():
    """The failure DOM was dumped whole; the reviewer already caps at 40."""
    elements = ([{"role": "heading", "name": f"h{i}"} for i in range(60)]
                + [{"role": "button", "name": "Start Capture"}])

    capped = self_healer._cap_dom({"url": "u", "elements": elements})

    assert len(capped["elements"]) == self_healer._MAX_DOM_ELEMENTS
    assert capped["elements"][0]["name"] == "Start Capture"
    assert capped["element_count"] == 61


def test_ansi_codes_are_stripped_from_the_error_the_healer_sees(tmp_path, monkeypatch):
    """Colour codes wasted the 1500-char cap and skewed difflib ratios."""
    repo, feature = _repo(tmp_path, "export class P {}\n"), tmp_path / "feature"
    seen = []
    monkeypatch.setattr(self_healer, "call_agent", lambda **kw: seen.append(kw["user_prompt"]) or
                        '{"patched_content": null, "unresolved_reason": "no"}')

    self_healer.run(broken_file_content=_SPEC_SRC, failure_reason="r", feature_dir=feature,
                    repo_output_dir=repo, file_path=_SPEC, failure_dom={"elements": [], "url": "x"},
                    raw_error="\x1b[31mError: boom\x1b[39m")

    assert "\x1b" not in seen[0] and "Error: boom" in seen[0]


# --- reviewer ----------------------------------------------------------------

def _report(*tests):
    return {"suites": [{"specs": [
        {"title": f"{cid} does a thing", "file": "a.spec.ts",
         "tests": [{"status": status, "results": [{"status": r, "error": {"message": msg}}
                                                  for r, msg in results]}]}
        for cid, status, results in tests]}]}


def test_compliance_findings_for_unchanged_files_survive_the_next_review():
    """review_report.json is overwritten wholesale and unchanged files are sent as
    markers only, so their prior findings were lost on every re-review."""
    review = {"compliance": [{"file": "b.spec.ts", "issues": []}]}
    prior = {"compliance": [{"file": "a.spec.ts", "issues": ["hard wait"]},
                            {"file": "b.spec.ts", "issues": ["stale"]},
                            {"file": "gone.spec.ts", "issues": ["x"]}]}

    reviewer._carry_forward_compliance(review, prior, {"a.spec.ts", "b.spec.ts"})

    files = {c["file"]: c["issues"] for c in review["compliance"]}
    assert files == {"b.spec.ts": [], "a.spec.ts": ["hard wait"]}


def test_generated_files_are_not_sent_to_the_reviewer_in_full():
    generated = [{"path": "src/pages/RegisterPage.ts", "content": _GEN_POM},
                 {"path": "tests/a.spec.ts", "content": "test('x')"}]

    sent = reviewer._files_for_prompt(generated, {})

    assert [f["path"] for f in sent] == ["tests/a.spec.ts"]


def test_prematched_flaky_cases_cost_no_tokens_and_the_case_index_is_trimmed(tmp_path, monkeypatch):
    """Pre-matched known_flaky cases still had errors/DOM sent, and the whole case
    index and every known_flaky pattern went out for a handful of failures."""
    msg = "\x1b[31mError: Timeout 5000ms exceeded waiting for getByTestId('start')\x1b[39m"
    run_results = _report(("TC-1", "flaky", [("failed", msg), ("passed", "")]),
                          ("TC-2", "unexpected", [("failed", "Error: Expected: A Received: B")]),
                          ("TC-3", "expected", [("passed", "")]))
    index = [{"test_case_id": f"TC-{i}", "title": f"case {i}"} for i in (1, 2, 3)]
    patterns = [{"test_case_id": "TC-1", "reason": "Error: Timeout 3000ms exceeded waiting for "
                                                   "getByTestId('start')"},
                {"test_case_id": "TC-3", "reason": "unrelated"}]
    prompts = []
    monkeypatch.setattr(reviewer, "call_agent", lambda **kw: prompts.append(kw) or
                        '{"compliance": [], "failure_classification": []}')

    out = reviewer.run(run_results, [], "std", patterns, tmp_path, case_index=index)

    sent = prompts[0]["user_prompt"]
    assert "Expected: A" in sent and "Timeout 5000ms" not in sent    # TC-1 dropped, TC-2 kept
    assert "\x1b" not in sent
    assert '"TC-3"' not in sent and '"TC-2"' in sent                 # only failing rows
    assert "unrelated" not in prompts[0]["extra_system"]
    assert any(c["test_case_id"] == "TC-1" for c in json.loads(out.read_text())["failure_classification"])


def test_the_whole_case_index_is_sent_when_a_failure_maps_to_no_case(tmp_path, monkeypatch):
    run_results = _report(("MYSTERY-9", "unexpected", [("failed", "boom")]))
    index = [{"test_case_id": "TC-1"}, {"test_case_id": "TC-2"}]
    prompts = []
    monkeypatch.setattr(reviewer, "call_agent", lambda **kw: prompts.append(kw) or
                        '{"compliance": [], "failure_classification": []}')

    reviewer.run(run_results, [], "std", [], tmp_path, case_index=index)

    assert '"TC-1"' in prompts[0]["user_prompt"] and '"TC-2"' in prompts[0]["user_prompt"]


def test_known_flaky_does_not_grow_by_one_entry_per_run_for_the_same_failure():
    """Deduped on the model's free-text reason, so every run appended a near-duplicate
    and changed the Reviewer's cached prefix."""
    known = [{"test_case_id": "TC-1", "reason": "Timeout 5000ms exceeded on start"}]
    ctx = SimpleNamespace(state={"executor": {"cases": [{"test_case_id": "TC-1", "flaky": True}]}},
                          artifact=lambda name: Path("unused") / name)

    review_support._merge_known_flaky(
        ctx, [{"verdict": "known_flaky", "test_case_id": "TC-1",
               "reason": "Timeout 30000ms exceeded on start"}], known)

    assert len(known) == 1


# --- decline -----------------------------------------------------------------

def _decline_profile(tmp_path):
    root = tmp_path / "profiles" / "app"
    run = tmp_path / "runs" / "app" / "F" / "1_testcases"
    tests = tmp_path / "workspaces" / "app" / "tests" / "e2e"
    for d in (root, run, tests):
        d.mkdir(parents=True)
    (run / "test_cases.json").write_text(json.dumps([{"test_case_id": "TC-1"}]))
    (tests / "a.spec.ts").write_bytes(b"test('TC-1 x', async () => {\r\n  go();\r\n});\r\n")
    book = openpyxl.Workbook()
    book.active.append(["Test Case ID", "Status"])
    book.active.append(["TC-1", "Reviewed"])
    book.save(run / "Master.xlsx")
    return SimpleNamespace(root=root, context_root=tmp_path / "runs" / "app",
                           workspace=tmp_path / "workspaces" / "app"), run, tests


def test_a_corrupt_or_lock_workbook_does_not_leave_a_half_declined_case(tmp_path):
    """An Excel lock file (~$x.xlsx) matches *.xlsx; loading it raised BadZipFile,
    which escaped after the specs and JSON were already written."""
    profile, run, tests = _decline_profile(tmp_path)
    (run / "~$Master.xlsx").write_bytes(b"not a zip")
    (run / "Broken.xlsx").write_bytes(b"not a zip either")

    report = decline.decline(profile, "TC-1", "not in build")

    assert report["specs"] and report["json"] and len(report["sheets"]) == 1


def test_a_multiline_reason_cannot_break_the_spec_string_literal(tmp_path):
    profile, _run, tests = _decline_profile(tmp_path)

    decline.decline(profile, "TC-1", "line one\nline two")

    text = (tests / "a.spec.ts").read_bytes().decode()
    assert "test.skip(true, 'Declined: line one line two');" in text


def test_declining_keeps_the_specs_existing_line_endings(tmp_path):
    """write_text turned every LF into CRLF on Windows; a CRLF file must stay CRLF
    and not gain stray endings, and an LF one must stay LF."""
    profile, _run, tests = _decline_profile(tmp_path)

    decline.decline(profile, "TC-1", "r")

    raw = (tests / "a.spec.ts").read_bytes()
    assert b"\r\r" not in raw and raw.count(b"\n") == raw.count(b"\r\n")


# --- bug_reporter ------------------------------------------------------------

_ERR = "Error: expect(locator).toHaveText(expected) failed"


def _bug_review(tmp_path, *case_ids):
    path = tmp_path / "review_report.json"
    path.write_text(json.dumps({"failure_classification": [
        {"test_case_id": c, "classification": "genuine_bug", "severity": "major",
         "reason": "r", "file": "e2e/a.spec.ts"} for c in case_ids]}), encoding="utf-8")
    return path


def _bug_report(case_id):
    return {"suites": [{"specs": [{"title": f"{case_id} t", "tests": [
        {"results": [{"status": "failed", "error": {"message": _ERR}}]}]}]}]}


def test_a_bug_whose_case_never_ran_is_not_reported_fixed(tmp_path):
    """A --grep subset (or an executor that yielded nothing) reported every bug it did
    not even try as "No longer failing"."""
    feature = tmp_path / "run"
    bug_reporter.run(_bug_review(tmp_path, "A-1", "A-2"), _bug_report("A-1"), feature)

    text = bug_reporter.run(_bug_review(tmp_path, "A-1"), _bug_report("A-1"), feature,
                            executed_case_ids={"A-1"}).read_text(encoding="utf-8")

    assert "No longer failing" not in text


def test_a_fixed_bug_is_reported_once_and_its_number_is_never_reused(tmp_path):
    """The no-bugs branch never updated the registry, so "No longer failing" repeated
    on every later run."""
    feature = tmp_path / "run"
    bug_reporter.run(_bug_review(tmp_path, "A-1"), _bug_report("A-1"), feature)

    first = bug_reporter.run(_bug_review(tmp_path), {"suites": []}, feature,
                             executed_case_ids={"A-1"}).read_text(encoding="utf-8")
    second = bug_reporter.run(_bug_review(tmp_path), {"suites": []}, feature,
                              executed_case_ids={"A-1"}).read_text(encoding="utf-8")
    again = bug_reporter.run(_bug_review(tmp_path, "A-9"), _bug_report("A-9"), feature
                             ).read_text(encoding="utf-8")

    assert "BUG-01 (A-1)" in first and "No longer failing" not in second
    assert "BUG-02 — A-9 (new)" in again


def test_the_repro_command_uses_the_case_index_file_and_an_anchored_id(tmp_path):
    """The model's `file` is often testDir-relative, and an unanchored -g "TC-1" also
    selects TC-10."""
    text = bug_reporter.run(
        _bug_review(tmp_path, "A-1"), _bug_report("A-1"), tmp_path / "run",
        case_index=[{"test_case_id": "A-1", "target_file": "tests/e2e/a.spec.ts"}]
    ).read_text(encoding="utf-8")

    assert 'npx playwright test tests/e2e/a.spec.ts -g "A-1(?![\\w-])"' in text


# --- jira_uploader -----------------------------------------------------------

class _Resp:
    def __init__(self, body=None, status=200):
        self._body, self.status_code = body if body is not None else {}, status

    def json(self):
        return self._body

    def raise_for_status(self):
        pass


@pytest.fixture
def jira(monkeypatch):
    import requests
    calls = {"put": [], "post": []}
    monkeypatch.setattr(jira_uploader, "_jira_auth_config",
                        lambda: ("http://j", "e", "t", "2", "basic"))
    monkeypatch.setattr(requests, "put", lambda url, **kw: calls["put"].append(kw) or _Resp())
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls["post"].append(kw) or _Resp({}))
    return calls


def test_updating_an_issue_sends_only_fields_jira_allows_on_edit(jira, monkeypatch):
    """PUT carried `project` and `issuetype`, which Jira's edit-issue rejects with a 400,
    so every update came back as an error. (UNVERIFIED against a live Jira.)"""
    monkeypatch.setattr(jira_uploader, "find_existing_jira_issue",
                        lambda *a, **k: ("KEY-1", True))

    out = jira_uploader.create_jira_test_issues([{"Test Case ID": "TC-1", "Name": "n"}], "P")

    assert out[0]["action"] == "updated"
    assert set(jira["put"][0]["json"]["fields"]) == {"summary", "description"}


def test_a_create_response_with_no_key_is_a_failure_not_a_created_issue(jira, monkeypatch):
    monkeypatch.setattr(jira_uploader, "find_existing_jira_issue", lambda *a, **k: ("", True))

    out = jira_uploader.create_jira_test_issues([{"Test Case ID": "TC-1", "Name": "n"}], "P")

    assert out[0]["action"] == "error" and out[0]["jira_key"] is None


def test_a_failed_steps_upload_is_visible_and_retried_next_run(jira, monkeypatch):
    """The issue exists but its structured steps do not; that was only a warning and the
    case was snapshotted as done, so it was never retried."""
    monkeypatch.setenv("JIRA_TEST_MANAGEMENT_TOOL", "xray")
    monkeypatch.setattr(jira_uploader, "find_existing_jira_issue",
                        lambda *a, **k: ("KEY-1", True))
    monkeypatch.setattr(jira_uploader, "upload_structured_steps", lambda *a, **k: "boom")

    out = jira_uploader.create_jira_test_issues(
        [{"Test Case ID": "TC-1", "Name": "n", "Test Step": "1. a"}], "P")

    assert out[0]["partial"] is True and out[0]["steps_warning"] == "boom"
