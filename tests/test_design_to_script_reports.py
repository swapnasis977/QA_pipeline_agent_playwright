"""The four things a reviewer asked of the design-to-script path: a screen model from Figma, a
requirement-to-result matrix, test data templates, and an effort figure that admits its assumption."""

import json

from utils import effort_report, testdata_template, traceability
from utils.figma_model import build_screen_model


def _file():
    def node(id_, type_, name, children=None, characters=None):
        n = {"id": id_, "type": type_, "name": name}
        if children is not None:
            n["children"] = children
        if characters is not None:
            n["characters"] = characters
        return n

    login = node("1:1", "FRAME", "Login", [
        node("2:1", "INSTANCE", "Button / Primary", [node("3:1", "TEXT", "label", characters="Sign in")]),
        node("2:2", "INSTANCE", "Text field", [node("3:2", "TEXT", "ph", characters="Username")]),
        node("2:3", "INSTANCE", "Remember me checkbox"),
        node("2:4", "RECTANGLE", "Button background"),   # decoration, not a control
    ])
    return {"document": {"children": [{"name": "Page 1", "children": [login]}]}}


def test_figma_layers_become_typed_components_and_decoration_is_ignored():
    screen = build_screen_model(_file())["screens"][0]
    got = {(c["type"], c["label"]) for c in screen["components"]}
    assert got == {("button", "Sign in"), ("text_field", "Username"), ("checkbox", "")}


def test_the_screen_model_says_it_is_never_a_locator_source():
    assert "never a locator" in build_screen_model(_file())["note"].lower()


def test_a_stated_limit_yields_its_boundary_values_and_loose_wording_yields_none():
    cases = [
        {"id": "A-1-AUTO-TC-001", "text": "Name accepts a maximum of 255 characters"},
        {"id": "A-1-AUTO-TC-002", "text": "Brightness is between -100 and 100"},
        {"id": "A-1-AUTO-TC-003", "text": "At least two markers are placed; draw a line between two points"},
    ]
    limits = {item["rule"]: item["boundary"] for item in testdata_template.extract_limits(cases)}
    assert limits["max 255 characters"] == {"below": 254, "at": 255, "above": 256}
    assert limits["between -100 and 100"]["min"]["below"] == -101
    assert len(limits) == 2


def test_text_fields_from_the_design_get_negative_inputs_beside_a_valid_placeholder():
    template = testdata_template.build([], build_screen_model(_file()))
    values = template["fields"][0]["values"]
    assert {"valid", "empty", "whitespace_only", "over_long", "markup"} <= set(values)
    assert testdata_template.build([], None) is None


def _workspace_and_run(tmp_path):
    ws = tmp_path / "ws"
    (ws / "tests" / "e2e").mkdir(parents=True)
    (ws / "tests" / "e2e" / "a.spec.ts").write_text(
        "test('S-1-AUTO-TC-001 does a thing', async () => {});\n", encoding="utf-8")
    run = tmp_path / "runs" / "S-1_S-2"
    (run / "1_testcases").mkdir(parents=True)
    (run / "3_results").mkdir()
    (run / "1_testcases" / "test_cases.json").write_text(json.dumps([
        {"test_case_id": "S-1-AUTO-TC-001", "title": "t1", "acceptance_criterion": "AC one", "tags": ["Negative"]},
        {"test_case_id": "S-1-AUTO-TC-002", "title": "t2", "acceptance_criterion": "AC two"},
    ]), encoding="utf-8")
    (run / "3_results" / "run_results.json").write_text(json.dumps({"suites": [{"specs": [
        {"title": "S-1-AUTO-TC-001 does a thing", "ok": True, "tests": [{"status": "expected"}]}]}]}),
        encoding="utf-8")
    return ws, run


def test_a_case_with_no_script_is_listed_as_such_not_dropped(tmp_path):
    ws, run = _workspace_and_run(tmp_path)
    matrix = traceability.build(run, ws)
    by_id = {r["id"]: r for r in matrix["rows"]}
    assert by_id["S-1-AUTO-TC-001"]["scripts"] == ["tests/e2e/a.spec.ts"]
    assert by_id["S-1-AUTO-TC-001"]["result"] == "passed"
    assert by_id["S-1-AUTO-TC-002"]["scripts"] == [] and by_id["S-1-AUTO-TC-002"]["result"] == ""
    assert matrix["totals"] == {"cases": 2, "with_script": 1, "passed": 1, "failed": 0}
    assert "AC one" in traceability.render(run.name, matrix)


def test_the_effort_saving_follows_the_team_s_own_minutes_and_calls_them_an_assumption(tmp_path, monkeypatch):
    ws, run = _workspace_and_run(tmp_path)
    monkeypatch.setenv("EFFORT_MANUAL_MINUTES_PER_CASE", "120")
    monkeypatch.setenv("EFFORT_REVIEW_MINUTES_PER_CASE", "30")
    totals = traceability.build(run, ws)["totals"]
    text = effort_report.render(run.name, effort_report.measure(run, [], totals))
    assert "120 min each" in text and "30 min each" in text and "75%" in text
    assert "assumption" in text.lower()
    assert effort_report.render(run.name, effort_report.measure(run, [], {})) is None
