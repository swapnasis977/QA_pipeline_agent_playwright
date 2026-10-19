"""Turning an OpenAPI/Swagger spec into API test-case input: loading it, rendering the contract, deriving stories.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

#: Operations per derived story when a spec is used as the story source.
#: Not a style choice — `_generate_one_story` caps the model at 16000 output
#: tokens, and a thorough API case set runs 6-9 cases per operation at roughly
#: 400 tokens each. Four operations lands around 11-15k; eight overflows and
#: comes back as truncated JSON that fails the shape check. Stories are cheap
#: (one sheet each, four generated in parallel), truncation is not.
_MAX_OPS_PER_API_STORY = 4


def fetch_api_spec(spec_path_or_url: str) -> dict:
    """Loads an OpenAPI 3 / Swagger 2 JSON spec into a flat, prompt-ready shape.

    Accepts a local path or an http(s) URL (many services publish their own
    spec — this one at /getOpenApiSpecification). YAML is supported only if
    PyYAML happens to be installed; it is not a dependency of this project, so
    the error tells you to convert rather than failing obscurely.

    Returns {"title", "version", "base_url", "operations": [...], "schemas": {...}},
    where each operation is one method+path with its parameters, request schema
    and every documented response code — the units both `render_api_contract`
    and `api_spec_to_stories` work from.
    """
    text = ""
    source = spec_path_or_url
    if spec_path_or_url.lower().startswith("http"):
        import requests
        resp = requests.get(spec_path_or_url, timeout=30)
        resp.raise_for_status()
        text = resp.text
    else:
        path = Path(spec_path_or_url)
        if not path.is_file():
            raise FileNotFoundError(f"API spec not found: {spec_path_or_url}")
        text = path.read_text(encoding="utf-8")
        source = str(path)

    try:
        spec = json.loads(text)
    except json.JSONDecodeError:
        try:
            import yaml  # optional — not in requirements.txt
        except ImportError:
            raise ValueError(
                f"'{source}' is not valid JSON. If it is YAML, either install PyYAML "
                f"(pip install pyyaml) or convert the spec to JSON first."
            ) from None
        spec = yaml.safe_load(text)

    if "paths" not in spec:
        raise ValueError(f"'{source}' has no 'paths' object — not an OpenAPI/Swagger spec.")

    info = spec.get("info") or {}
    servers = spec.get("servers") or []
    base_url = (servers[0].get("url") if servers else "") or spec.get("host", "")

    # Swagger 2 keeps schemas under "definitions", OpenAPI 3 under
    # "components/schemas" — normalize so the rest of this file has one shape.
    schemas = (spec.get("components") or {}).get("schemas") or spec.get("definitions") or {}

    http_methods = {"get", "post", "put", "patch", "delete", "head", "options"}
    operations = []
    for path, methods in spec["paths"].items():
        shared_params = methods.get("parameters") or []
        for method, op in methods.items():
            if method.lower() not in http_methods or not isinstance(op, dict):
                continue
            params = shared_params + (op.get("parameters") or [])
            body_ref = _request_schema_name(op)
            operations.append({
                "tag": (op.get("tags") or ["Untagged"])[0],
                "path": path,
                "method": method.upper(),
                "operation_id": op.get("operationId", ""),
                "summary": op.get("summary", ""),
                "path_params": [p.get("name") for p in params if p.get("in") == "path"],
                "query_params": [
                    {"name": p.get("name"),
                     "schema": p.get("schema") or {"type": p.get("type", "string")},
                     "required": bool(p.get("required"))}
                    for p in params if p.get("in") == "query"
                ],
                "request_schema": body_ref,
                "request_required": bool((op.get("requestBody") or {}).get("required")),
                "responses": {
                    str(code): (body or {}).get("description", "")
                    for code, body in (op.get("responses") or {}).items()
                    if str(code) != "default"
                },
            })

    return {
        "source": source,
        "title": info.get("title", "API"),
        "version": str(info.get("version", "")),
        "base_url": base_url,
        "operations": operations,
        "schemas": schemas,
    }


def _request_schema_name(op: dict) -> str:
    """Component name of an operation's JSON request body, or "" if it has none.

    Handles OpenAPI 3 (requestBody.content) and Swagger 2 (a body parameter),
    and only follows a $ref — an inline schema has no component name to give,
    and `_render_schema` below renders it from the operation instead.
    """
    try:
        ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        return ref.rsplit("/", 1)[-1]
    except (KeyError, TypeError):
        pass
    for param in op.get("parameters") or []:
        if param.get("in") == "body":
            ref = (param.get("schema") or {}).get("$ref", "")
            if ref:
                return ref.rsplit("/", 1)[-1]
    return ""


def _render_schema(name: str, schemas: dict, indent: str = "  ") -> list:
    """One schema as flat text lines: field, type, required/optional, constraints.

    Deliberately not raw JSON Schema — the model needs to see which fields are
    REQUIRED and which values an enum allows (that is what rule 3b of the skill
    turns into omission and rejected-value cases), and that reads far more
    reliably as a labelled list than as nested JSON. Angle brackets are stripped
    because descriptions round-trip through JIRA's HTML-stripping path.
    """
    schema = schemas.get(name) or {}
    required = set(schema.get("required") or [])
    lines = [f"{indent}{name}:"]
    properties = schema.get("properties") or {}
    if not properties:
        lines.append(f"{indent}  (free-form object — no properties declared)")
    for prop, meta in properties.items():
        meta = meta or {}
        bits = [meta.get("type", "object")]
        for key, label in (("format", ""), ("default", "default="), ("minItems", "minItems="),
                           ("maxItems", "maxItems="), ("minimum", "min="), ("maximum", "max="),
                           ("minLength", "minLength="), ("maxLength", "maxLength=")):
            if meta.get(key) is not None:
                bits.append(f"{label}{meta[key]}")
        if meta.get("enum"):
            bits.append("enum: " + " | ".join(str(e) for e in meta["enum"]))
        if meta.get("nullable"):
            bits.append("nullable")
        flag = "REQUIRED" if prop in required else "optional"
        line = f"{indent}  - {prop} ({', '.join(str(b) for b in bits)}) [{flag}]"
        description = re.sub(r"[<>]", "", meta.get("description") or "")
        if description:
            line += f" - {description}"
        lines.append(line)
    return lines


def render_api_contract(spec_data: dict, operations: list = None) -> str:
    """The API Contract block the prompt shows the model.

    `operations` scopes it to a subset (one derived story's slice); omitted, the
    whole spec is rendered. Only the schemas those operations actually reference
    are included, plus every response schema — a story about four endpoints does
    not need the other twenty-seven endpoints' payloads competing for attention.
    """
    operations = spec_data["operations"] if operations is None else operations
    schemas = spec_data.get("schemas") or {}

    out = [
        f"{spec_data['title']}"
        + (f" (version {spec_data['version']})" if spec_data.get("version") else ""),
        f"Base URL: {spec_data.get('base_url') or '(not declared in the spec)'}",
        "",
        "OPERATIONS IN SCOPE:",
    ]
    referenced = []
    for op in operations:
        out.append("")
        out.append(f"  {op['method']} {op['path']}"
                   + (f" - operationId {op['operation_id']}" if op["operation_id"] else ""))
        if op.get("summary"):
            out.append(f"    Summary: {op['summary']}")
        if op.get("path_params"):
            out.append(f"    Path params: {', '.join(op['path_params'])}")
        for param in op.get("query_params") or []:
            schema = param.get("schema") or {}
            bits = [schema.get("type", "string")]
            if schema.get("enum"):
                bits.append("enum: " + " | ".join(str(e) for e in schema["enum"]))
            if schema.get("default") is not None:
                bits.append(f"default={schema['default']}")
            out.append(f"    Query param: {param['name']} ({', '.join(str(b) for b in bits)})"
                       f" [{'REQUIRED' if param.get('required') else 'optional'}]")
        if op.get("request_schema"):
            out.append(f"    Request body: {op['request_schema']} "
                       f"({'required' if op['request_required'] else 'optional'})")
            referenced.append(op["request_schema"])
        for code, description in op.get("responses", {}).items():
            out.append(f"    Response {code}: {description}")

    # Response schemas aren't tracked per-operation above (a response can be an
    # array, an envelope, or a bare $ref), so every remaining schema is included
    # — they are what rule 3b's "assert the named fields" cases are written from.
    ordered = list(dict.fromkeys(referenced)) + [n for n in schemas if n not in referenced]
    if ordered:
        out.append("")
        out.append("REQUEST / RESPONSE SCHEMAS:")
        for name in ordered:
            out.append("")
            out.extend(_render_schema(name, schemas))

    return "\n".join(out)


def api_spec_to_stories(spec_data: dict, story_prefix: str, epic_key: str,
                        max_ops: int = _MAX_OPS_PER_API_STORY) -> list:
    """Turns a spec into story-shaped inputs — one story per tag, chunked.

    Returns [(story_key, jira_data, operations)], where jira_data has exactly the
    shape `fetch_jira_story` returns, so everything downstream (the prompt, the
    per-story sheet, story_context_*.json, the coverage gate) works unchanged.

    ONE ACCEPTANCE CRITERION PER OPERATION, stating its summary, every documented
    response code, its required fields and its enum constraints. That is what
    makes an API sheet auditable the same way a story-driven one is: the coverage
    gate can then report "nothing covers PUT /cases/{caseId}/priority's 409".

    Tags are the natural story boundary (they are how the API's own authors
    grouped it); a tag with more operations than `max_ops` is split into parts
    rather than overflowing the model's output cap — see _MAX_OPS_PER_API_STORY.
    """
    by_tag = {}
    for op in spec_data["operations"]:
        by_tag.setdefault(op["tag"], []).append(op)

    schemas = spec_data.get("schemas") or {}
    stories = []
    for index, (tag, ops) in enumerate(by_tag.items(), start=1):
        chunks = [ops[i:i + max_ops] for i in range(0, len(ops), max_ops)] or [[]]
        for part, chunk in enumerate(chunks, start=1):
            suffix = f"{index:02d}" + (chr(64 + part) if len(chunks) > 1 else "")
            story_key = f"{story_prefix}-{suffix}"
            criteria = [_operation_criterion(op, schemas) for op in chunk]
            # Some specs tag every operation with the service's own name; the
            # story title would then read "Foo Service - Foo Service: verify...".
            area = f"{spec_data['title']} - {tag}" if tag != spec_data["title"] else tag
            title = (f"{area}"
                     + (f" (part {part} of {len(chunks)})" if len(chunks) > 1 else "")
                     + ": verify every operation, validation rule and response code")
            stories.append((story_key, {
                "story_key": story_key,
                "epic_key": epic_key,
                "title": title,
                "description": (
                    f"API-level verification of the {tag} area of {spec_data['title']}, "
                    f"served at {spec_data.get('base_url') or 'the deployed base URL'}. "
                    f"In scope: "
                    + ", ".join(f"{op['method']} {op['path']}" for op in chunk)
                    + ". The API Contract block accompanying this story is the source of "
                    "truth for request shapes, validation rules and response codes."
                ),
                "acceptance_criteria": criteria,
                "issue_type": "Story",
                "source": "api_spec",
            }, chunk))
    return stories


def _operation_criterion(op: dict, schemas: dict) -> str:
    """One operation rendered as a single acceptance criterion sentence."""
    parts = [f"{op['method']} {op['path']}"
             + (f" ({op['operation_id']})" if op["operation_id"] else "")
             + f": {op.get('summary') or 'operation'}."]
    for code, description in op.get("responses", {}).items():
        parts.append(f"Returns {code}{' - ' + description if description else ''}.")

    schema = schemas.get(op.get("request_schema")) or {}
    if schema.get("required"):
        parts.append(f"{op['request_schema']} required fields: "
                     f"{', '.join(schema['required'])}.")
    constraints = [
        f"{prop} must be one of {' | '.join(str(e) for e in (meta or {}).get('enum'))}"
        for prop, meta in (schema.get("properties") or {}).items()
        if (meta or {}).get("enum")
    ]
    if constraints:
        parts.append("Constraints: " + "; ".join(constraints) + ".")
    return " ".join(parts)
