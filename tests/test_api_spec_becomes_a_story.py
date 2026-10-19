"""An OpenAPI spec must reach the Test Case Writer as a testable story.

The writer was built for UI work: a JIRA story plus Figma. Handed a service
contract instead, it had nowhere to put it — the spec had to be hand-converted
into a fake JIRA XML export before the agent would look at it, and the model
then invented browser steps for endpoints that have no UI at all.

A spec now travels the same road Figma does. Two things have to hold for that
to be worth anything:

  1. The contract survives the trip WITH ITS TEST BASIS INTACT. Required
     fields, enum values and every documented status code are what rule 3b of
     the skill turns into omission, rejected-value and error-path cases. A
     rendering that drops them looks fine and silently halves the suite.
  2. A spec used as the story source produces stories SMALL ENOUGH TO GENERATE.
     `_generate_one_story` caps the model at 16000 output tokens; eight
     operations of thorough API coverage overflows it and comes back as
     truncated JSON. Confirmed the hard way — two eight-operation stories out
     of nine failed exactly this way before the split existed.

Run with:  python -m pytest tests/test_api_spec_becomes_a_story.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.testcase_writer import (  # noqa: E402
    _MAX_OPS_PER_API_STORY,
    api_spec_to_stories,
    fetch_api_spec,
    render_api_contract,
)

_SPEC = {
    "openapi": "3.0.0",
    "info": {"title": "Sample Service", "version": "1.0.0"},
    "servers": [{"url": "http://localhost:8080"}],
    "paths": {
        "/cases": {
            "post": {
                "summary": "Create a new case",
                "operationId": "createCase",
                "tags": ["Cases"],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {
                        "schema": {"$ref": "#/components/schemas/CreateCaseRequest"}}},
                },
                "responses": {
                    "201": {"description": "Case created"},
                    "400": {"description": "Bad Request - schema/field validation failure"},
                    "422": {"description": "Unprocessable Entity - case type not published"},
                    "default": {"description": "unexpected error"},
                },
            },
            "get": {
                "summary": "Search cases",
                "operationId": "searchCases",
                "tags": ["Cases"],
                "parameters": [
                    {"name": "status", "in": "query", "schema": {"type": "string"}},
                    {"name": "pageSize", "in": "query",
                     "schema": {"type": "integer", "default": 25}},
                ],
                "responses": {"200": {"description": "Paginated list"}},
            },
        },
        "/cases/{caseId}": {
            "get": {
                "summary": "Retrieve a case",
                "operationId": "getCase",
                "tags": ["Cases"],
                "parameters": [{"name": "caseId", "in": "path", "required": True,
                                "schema": {"type": "integer"}}],
                "responses": {"200": {"description": "Case retrieved"},
                              "404": {"description": "Not Found"}},
            },
        },
        "/healthz": {
            "get": {"summary": "Health check", "operationId": "healthz",
                    "tags": ["Service"], "responses": {"200": {"description": "Healthy"}}},
        },
    },
    "components": {"schemas": {
        "CreateCaseRequest": {
            "type": "object",
            "required": ["caseTypeId", "subjectReferences"],
            "properties": {
                "caseTypeId": {"type": "string"},
                "subjectReferences": {"type": "array", "minItems": 1,
                                      "items": {"type": "string"}},
                "priority": {"type": "string",
                             "enum": ["Low", "Normal", "High", "Critical"],
                             "default": "Normal"},
            },
        },
    }},
}


def _spec_file(tmp_path: Path, spec: dict = None) -> str:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec or _SPEC), encoding="utf-8")
    return str(path)


def test_every_operation_is_read_off_the_spec(tmp_path):
    data = fetch_api_spec(_spec_file(tmp_path))

    assert data["base_url"] == "http://localhost:8080"
    assert {(op["method"], op["path"]) for op in data["operations"]} == {
        ("POST", "/cases"), ("GET", "/cases"),
        ("GET", "/cases/{caseId}"), ("GET", "/healthz"),
    }

    create = next(op for op in data["operations"] if op["operation_id"] == "createCase")
    # "default" is a catch-all, not a testable outcome — it must not become a case.
    assert set(create["responses"]) == {"201", "400", "422"}
    assert create["request_schema"] == "CreateCaseRequest"

    get_case = next(op for op in data["operations"] if op["operation_id"] == "getCase")
    assert get_case["path_params"] == ["caseId"]


def test_the_contract_keeps_what_the_negative_cases_are_written_from(tmp_path):
    contract = render_api_contract(fetch_api_spec(_spec_file(tmp_path)))

    assert "Response 400: Bad Request - schema/field validation failure" in contract
    assert "caseTypeId (string) [REQUIRED]" in contract
    assert "subjectReferences" in contract and "minItems=1" in contract
    assert "enum: Low | Normal | High | Critical" in contract
    # An optional field must not read as a required one, or every omission case
    # asserts a rejection the service is right not to make.
    assert "priority (string, default=Normal, enum: Low | Normal | High | Critical) [optional]" \
        in contract


def test_a_story_carries_only_its_own_slice_of_the_contract(tmp_path):
    data = fetch_api_spec(_spec_file(tmp_path))
    stories = api_spec_to_stories(data, "CMS", "CMS")

    by_key = {key: (jira, ops) for key, jira, ops in stories}
    assert set(by_key) == {"CMS-01", "CMS-02"}, "one story per tag"

    service_jira, service_ops = by_key["CMS-02"]
    contract = render_api_contract(data, service_ops)
    assert "GET /healthz" in contract
    assert "/cases" not in contract, \
        "a story about health must not carry the Cases endpoints into its prompt"
    assert service_jira["epic_key"] == "CMS"


def test_each_operation_becomes_one_auditable_acceptance_criterion(tmp_path):
    data = fetch_api_spec(_spec_file(tmp_path))
    cases_jira = next(jira for key, jira, _ in api_spec_to_stories(data, "CMS", "CMS")
                      if key == "CMS-01")

    criteria = cases_jira["acceptance_criteria"]
    assert len(criteria) == 3, "one criterion per operation, so the coverage gate can name a gap"

    create = next(c for c in criteria if c.startswith("POST /cases"))
    assert "Returns 400" in create and "Returns 422" in create
    assert "required fields: caseTypeId, subjectReferences" in create
    assert "priority must be one of Low | Normal | High | Critical" in create


def test_a_tag_too_big_for_one_generation_is_split(tmp_path):
    """The 16k output cap is a real ceiling, not a guideline — see the module docstring."""
    spec = json.loads(json.dumps(_SPEC))
    for n in range(10):
        spec["paths"][f"/cases/extra{n}"] = {
            "get": {"summary": f"Extra {n}", "operationId": f"extra{n}",
                    "tags": ["Cases"], "responses": {"200": {"description": "OK"}}},
        }

    stories = api_spec_to_stories(fetch_api_spec(_spec_file(tmp_path, spec)), "CMS", "CMS")

    cases_stories = [(key, ops) for key, _, ops in stories if key.startswith("CMS-01")]
    assert len(cases_stories) > 1, "13 Cases operations cannot go into one story"
    assert all(len(ops) <= _MAX_OPS_PER_API_STORY for _, ops in cases_stories)
    # Split parts get distinct keys, or two sheets collide on one filename and
    # every downstream stage joins on ids from whichever wrote last.
    assert len({key for key, _ in cases_stories}) == len(cases_stories)
    # Splitting must not lose an operation.
    assert sum(len(ops) for _, ops in cases_stories) == 13


def test_a_spec_that_is_not_a_spec_says_so(tmp_path):
    path = tmp_path / "notaspec.json"
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")

    try:
        fetch_api_spec(str(path))
    except ValueError as exc:
        assert "not an OpenAPI/Swagger spec" in str(exc)
    else:
        raise AssertionError("a JSON file with no paths must be rejected, not half-read")
