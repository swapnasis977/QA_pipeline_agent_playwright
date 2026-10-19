"""
Agent 8: Jira Uploader
IN:  The reviewed test case Excel sheet (Status == "Reviewed" on every row —
     same gate the orchestrator already enforces before the Planner runs)
OUT: One Jira test issue per row (native Jira "Test" issue type by default;
     Xray/Zephyr instances can point JIRA_TEST_ISSUE_TYPE at their own test
     issue type name), plus jira_upload_result.json recording each row's
     created key or error.

Deliberately NOT an LLM agent — mapping sheet columns to Jira issue fields is
a templating problem, not a reasoning one (same rationale as bug_reporter.py).
Works with NO AI_PROVIDER key at all.

NOTE on MCP/API: `create_jira_test_issues` is a stub for JIRA's REST API,
following the exact same fallback pattern as `fetch_jira_story`
(testcase_writer.py) and `fetch_test_cycle_keys` (coding_agent.py) — real API
call when JIRA_BASE_URL/JIRA_API_TOKEN are set, a clearly-marked placeholder
result (never a crash) when they're not.
"""

import os
import re
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import read_json, write_json
from agents.coding_cases import read_test_case_sheet
from utils.run_layout import artifact_path

#: Where per-row upload results (created key, or error) are persisted —
#: re-running this agent against the same feature reuses an already-created
#: key instead of filing a duplicate Jira issue for the same test case.
_RESULT_FILENAME = "jira_upload_result.json"


#: Every uploaded issue's summary is prefixed with this marker (its pipeline
#: Test Case ID in brackets) so a JQL search can reliably find an issue this
#: pipeline already created for the SAME case in a PREVIOUS run — including a
#: run against a different feature_dir (e.g. after --regenerate-testcases
#: made a fresh context/<epic>_<story>/ directory). jira_upload_result.json
#: alone only protects against re-uploading within one feature_dir; this is
#: what protects against it across feature_dirs.
def _marker(case_id: str) -> str:
    return f"[{case_id}]"


def _load_prior_results(feature_dir: Path) -> dict:
    # Tolerant of a corrupt/hand-edited result file: degrades to "nothing
    # uploaded yet" (re-checks JIRA per case) instead of crashing. Non-dict
    # entries are skipped rather than raising inside the comprehension.
    prior = read_json(artifact_path(feature_dir, _RESULT_FILENAME), default=[])
    return {r["test_case_id"]: r for r in prior
            if isinstance(r, dict) and r.get("test_case_id")}


#: Matches this team's numbered-list convention (see
#: testcase_writer.skill.md: "1-Step text", "2- Result text" — hyphen
#: immediately or with one space after the number, inconsistent spacing
#: observed in practice from both the skill's own examples and real model
#: output). Captures the step number and the text through end of line.
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*[-.)]\s*(.*)$", re.M)


def _parse_numbered_line_map(text: str) -> dict:
    """Splits a numbered-list string ("1-Do X\n\n2-Do Y") into {number: text}.
    Returns {} for blank/non-numbered text (e.g. a Test Data column that's
    just "Username and Password", not a numbered list) — callers fall back
    to putting the whole field in the description instead of a structured
    step in that case."""
    if not text:
        return {}
    return {int(n): t.strip() for n, t in _NUMBERED_LINE_RE.findall(text)}


def _parse_numbered_steps(test_step: str, expected_result: str, test_data: str = "") -> list:
    """Aligns Test Step/Expected Result (and, best-effort, Test Data) by
    their shared step NUMBER — not by list position — into
    [{"step_number", "action", "data", "result"}] objects, for Xray/
    Zephyr's structured test-steps field instead of this team's plain-
    description fallback. Aligning by number (not zip()) means a step
    missing its matching expected result (a model/human dropped a number)
    still produces a step with an empty result, rather than every
    subsequent step silently shifting by one. Returns [] if test_step
    isn't actually a numbered list (nothing structured to extract)."""
    steps = _parse_numbered_line_map(test_step)
    if not steps:
        return []
    results = _parse_numbered_line_map(expected_result)
    data = _parse_numbered_line_map(test_data)
    # Test Data is usually NOT numbered per-step (e.g. "Username and
    # Password" applies to the whole case) — when it isn't, the SAME value
    # is attached to every step rather than only step 1, since it's equally
    # relevant context for any of them.
    shared_data = test_data.strip() if test_data and not data else ""
    return [
        {
            "step_number": number,
            "action": steps[number],
            "data": data.get(number, shared_data),
            "result": results.get(number, ""),
        }
        for number in sorted(steps)
    ]


#: Fields sent on an UPDATE (PUT). The rest of `_case_to_jira_fields` is create-only.
_EDITABLE_FIELDS = ("summary", "description")


def _case_to_jira_fields(case: dict, project_key: str, issue_type: str) -> dict:
    """Maps one sheet row (see testcase_writer.MANUAL_COLUMNS) to a Jira
    issue's `fields` payload. Steps/data/expected are folded into the
    description since native Jira has no dedicated "test steps" field
    outside Xray/Zephyr's own custom screens — Xray/Zephyr users who want
    their app's real structured steps field should extend this function
    with their instance's specific custom field id, not change the shape
    other callers rely on."""
    description_parts = [
        case.get("Description") or "",
        f"\nPrerequisite:\n{case.get('Prerequisite')}" if case.get("Prerequisite") else "",
        f"\nTest Step:\n{case.get('Test Step')}" if case.get("Test Step") else "",
        f"\nTest Data:\n{case.get('Test Data')}" if case.get("Test Data") else "",
        f"\nExpected Result:\n{case.get('Expected Result')}" if case.get("Expected Result") else "",
    ]
    case_id = case.get("Test Case ID") or ""
    summary = case.get("Name") or case_id or "(untitled test case)"
    description = "\n".join(p for p in description_parts if p).strip()
    return {
        "project": {"key": project_key},
        "summary": f"{_marker(case_id)} {summary}" if case_id else summary,
        # Jira Cloud (REST v3) needs Atlassian Document Format; Server/DC takes text.
        "description": _adf(description) if _jira_auth_config()[3] == "3" else description,
        "issuetype": {"name": issue_type},
    }


def _jira_auth_config():
    """Shared auth/endpoint resolution for create/search/update. Delegates to
    `utils.jira_client.auth_config`, the one definition of these rules — this
    module kept its own copy, which had already drifted (no trailing-slash
    handling)."""
    from utils import jira_client
    return jira_client.auth_config()


#: Matches a credential-ish KEY followed by its value, where the value is
#: either a quoted string (consumed to the closing quote) or a bare token
#: (consumed to the next delimiter).
#:
#: The quoted alternative comes first and is what fixes a leak in the earlier
#: single-pattern version: its value class was `[^\s",}]+`, which STOPS AT A
#: SPACE. Against the most common real shape — `"Authorization": "Bearer
#: eyJhbGci..."` — it therefore matched only the word `Bearer` and wrote
#: `"Authorization": "[REDACTED] eyJhbGci..."` to disk, i.e. it redacted the
#: scheme name and left the actual token in the artifact.
_CREDENTIAL_RE = re.compile(
    r'(?i)("?(?:authorization|api[_-]?token|api[_-]?key|access[_-]?token|refresh[_-]?token'
    r'|secret|password|passwd|pwd)"?\s*[:=]\s*)'
    r'(?:"[^"]*"|[^\s,}&]+)')

#: A bearer/basic credential appearing WITHOUT a preceding key — e.g. a raw
#: header line echoed into an error body ("... Bearer eyJhbGci... invalid").
#: The pattern above only fires when a recognizable key precedes the value.
_BARE_SCHEME_RE = re.compile(r'(?i)\b(bearer|basic)\s+([A-Za-z0-9\-._~+/=]{8,})')


def _redact(text: str) -> str:
    """Strips credential-looking values out of an HTTP error body before it
    is persisted to jira_upload_result.json.

    A misconfigured gateway can echo auth context in 401/403 bodies (seen in
    practice), and that JSON file is otherwise treated as a safe audit
    artifact — committed, attached to tickets, pasted into chat. The
    redaction was previously inlined in ONE of the two paths that persist an
    error string; upload_structured_steps' error reached disk unredacted via
    the result's "steps_warning" field. Both go through here now.

    Best-effort by nature: it cannot recognize a credential echoed with no
    surrounding key or scheme. It is a safety net over an artifact that
    should not contain secrets in the first place, not a guarantee.
    """
    redacted = _CREDENTIAL_RE.sub(lambda m: f'{m.group(1)}"[REDACTED]"', text or "")
    return _BARE_SCHEME_RE.sub(r"\1 [REDACTED]", redacted)


def _error_detail(exc, limit: int = 300) -> str:
    """Response body (preferred — it carries JIRA's actual field-level
    validation errors) or the exception text, truncated and redacted."""
    response = getattr(exc, "response", None)
    detail = response.text[:limit] if response is not None else str(exc)
    return _redact(detail)


def _jira_request_kwargs(email: str, token: str, auth_type: str) -> dict:
    from utils import jira_client
    kwargs = jira_client.request_kwargs(email, token, auth_type)
    kwargs["headers"] = {**kwargs["headers"], "Content-Type": "application/json"}
    return kwargs


def _adf(text: str) -> dict:
    """Atlassian Document Format. Jira Cloud (REST v3) rejects a plain-string
    `description` with HTTP 400; v2 (Server/DC) takes the plain string."""
    paragraphs = [{"type": "paragraph", "content": [{"type": "text", "text": line}]}
                  for line in str(text or "").splitlines() if line.strip()]
    return {"type": "doc", "version": 1,
            "content": paragraphs or [{"type": "paragraph", "content": []}]}


def find_existing_jira_issue(case_id: str, project_key: str, base_url: str, api_version: str,
                              request_kwargs: dict) -> tuple:
    """Searches for an issue this pipeline already created for case_id in
    project_key, via its stable [<case_id>] summary marker — catches a
    duplicate from a PREVIOUS run against a different feature_dir, which
    jira_upload_result.json (scoped to one feature_dir) can't see.

    Returns (issue_key, search_ok):
      ("KEY-1", True)  — found an issue whose summary really carries the marker
      ("",      True)  — searched successfully, no such issue: safe to create
      ("",      False) — the search itself FAILED, so "no duplicate" is
                         unproven. Callers still create (blocking the whole
                         upload on a flaky search is worse), but record the
                         uncertainty on the result so a duplicate that lands
                         this way is traceable instead of silent. The old
                         signature returned a bare "" for both of the last
                         two, making them indistinguishable.

    Never raises.

    Two Jira API details this handles:
      - ENDPOINT. `GET /rest/api/3/search` is removed on Jira Cloud (replaced
        by `/rest/api/3/search/jql`); Server/DC v2 only has `/search`. Cloud
        is tried first on api_version 3 and falls back on 404/410, so both
        deployment types work without configuration.
      - `summary ~ "..."` is a TEXT match, not equality: Jira tokenizes the
        term, so the brackets in "[PROJ-68308-TC-001]" are not honored and a
        near-miss id can come back. Since a hit here leads to a PUT that
        OVERWRITES that issue, the returned summary is re-checked for the
        literal marker before it's accepted — matching the wrong issue would
        otherwise silently rewrite an unrelated ticket.
    """
    import requests

    def _jql_escape(value: str) -> str:
        # case_id comes from the Excel sheet's "Test Case ID" column and
        # project_key from a CLI arg — neither is escaped otherwise, so a
        # value containing `"` could break out of this quoted JQL literal
        # and widen/alter the search. Escape backslash and double-quote per
        # JQL string-literal rules.
        return str(value).replace("\\", "\\\\").replace('"', '\\"')

    marker = _marker(case_id)
    jql = f'project = "{_jql_escape(project_key)}" AND summary ~ "{_jql_escape(marker)}"'
    # maxResults > 1: the text match can return near-misses, and the real
    # marker-carrying issue is not necessarily ranked first.
    params = {"jql": jql, "maxResults": 10, "fields": "summary"}

    endpoints = ([f"{base_url}/rest/api/{api_version}/search/jql",
                  f"{base_url}/rest/api/{api_version}/search"]
                 if str(api_version) == "3" else
                 [f"{base_url}/rest/api/{api_version}/search"])

    for index, url in enumerate(endpoints):
        try:
            resp = requests.get(url, params=params, timeout=15, **request_kwargs)
            # Index, not `url is not endpoints[-1]`: identity comparison on
            # strings depends on interning, so if the two endpoint URLs ever
            # became equal (a base_url edit, a future api_version branch) the
            # `is not` could read False on the FIRST attempt and fall through
            # to raise_for_status instead of trying the next endpoint.
            is_last_endpoint = index == len(endpoints) - 1
            if resp.status_code in (404, 410) and not is_last_endpoint:
                continue  # this deployment doesn't have that endpoint — try the next
            resp.raise_for_status()
            issues = resp.json().get("issues", []) or []
        except (requests.RequestException, ValueError):
            continue

        for issue in issues:
            summary = ((issue.get("fields") or {}).get("summary") or "")
            if marker in summary and issue.get("key"):
                return issue["key"], True
        return "", True  # search worked; nothing carries this exact marker

    return "", False  # every endpoint failed — duplicate status unknown


def upload_structured_steps(base_url: str, request_kwargs: dict, issue_key: str,
                             steps: list, tool: str) -> str:
    """Uploads structured test steps (see _parse_numbered_steps) to Xray or
    Zephyr Scale's own dedicated steps API — instead of this module's
    native-Jira default of folding steps into the plain description — so
    a real Xray/Zephyr project gets an actually-populated Test Steps screen.

    UNVERIFIED against a live Xray/Zephyr instance (same caveat as
    coding_agent.fetch_test_cycle_keys' Zephyr endpoint) — these are the
    commonly-documented shapes for each tool's Cloud REST API as of this
    writing, but confirm against YOUR instance's own API docs before
    trusting this in production; a wrong endpoint/field name fails
    gracefully (returns an error string, doesn't raise) rather than
    silently reporting success.

    Returns "" on success, or an error string — never raises."""
    import requests

    if not steps:
        return ""  # nothing structured to upload — Test Step wasn't a numbered list
    try:
        if tool == "xray":
            resp = requests.post(
                f"{base_url}/rest/raven/2.0/api/test/{issue_key}/step",
                timeout=15, **request_kwargs,
                json=[{"action": s["action"], "data": s.get("data", ""), "result": s.get("result", "")}
                      for s in steps],
            )
        elif tool == "zephyr":
            resp = requests.post(
                f"{base_url}/rest/atm/1.0/testcase/{issue_key}/teststeps",
                timeout=15, **request_kwargs,
                json={"mode": "OVERWRITE", "items": [
                    {"inline": {"description": s["action"], "testData": s.get("data", ""),
                                 "expectedResult": s.get("result", "")}}
                    for s in steps
                ]},
            )
        else:
            return f"unknown JIRA_TEST_MANAGEMENT_TOOL '{tool}' — expected 'xray' or 'zephyr'"
        resp.raise_for_status()
        return ""
    except requests.RequestException as exc:
        # Redacted: this string is persisted as the result's "steps_warning".
        return f"structured steps upload to {tool} failed: {_error_detail(exc)}"


def create_jira_test_issues(cases: list, project_key: str, issue_type: str = "Test") -> list:
    """Creates or updates one Jira issue per case via the real JIRA REST API
    v2/v3 (auto-detected the same way as testcase_writer.fetch_jira_story).

    For each case: searches for an existing issue with this case's stable
    marker in project_key first (see find_existing_jira_issue) — if found,
    updates it (PUT) instead of creating a duplicate; a case whose content
    changed since its last upload gets the update, one that's unchanged
    still gets a cheap search-and-skip rather than a redundant write.

    Requires JIRA_BASE_URL, JIRA_API_TOKEN (+ JIRA_EMAIL for basic auth) in
    your environment (see .env.example) — falls back to a clearly-marked
    placeholder result per case if credentials are missing, so the pipeline
    doesn't crash — it just tells you plainly it didn't actually upload
    anything instead of silently doing so.

    Returns a list of {test_case_id, jira_key, action, error} — action is
    "created"|"updated"|"error"; jira_key is None (with error set) for any
    case that failed; callers should not assume every entry succeeded.

    If JIRA_TEST_MANAGEMENT_TOOL is "xray" or "zephyr" (unset/"native" skips
    this entirely), also parses Test Step/Test Data/Expected Result into
    structured steps (see _parse_numbered_steps) and uploads them via
    upload_structured_steps — a failure there is recorded in the result's
    "steps_warning" field WITHOUT flipping the case to "error", since the
    issue itself was still successfully created/updated.
    """
    import requests

    base_url, email, token, api_version, auth_type = _jira_auth_config()
    if not base_url or not token or (auth_type == "basic" and not email):
        missing = "JIRA_BASE_URL/JIRA_API_TOKEN" + (" (+ JIRA_EMAIL for basic auth)" if auth_type == "basic" else "")
        print(f"⚠️  JIRA credentials missing ({missing}) — no issues created. "
              "Set these in .env to actually upload test cases to JIRA.")
        return [{"test_case_id": c.get("Test Case ID"), "jira_key": None, "action": "error",
                  "error": "[PLACEHOLDER] JIRA credentials not configured"} for c in cases]

    request_kwargs = _jira_request_kwargs(email, token, auth_type)
    tool = os.environ.get("JIRA_TEST_MANAGEMENT_TOOL", "native").strip().lower()

    results = []
    for case in cases:
        case_id = case.get("Test Case ID")
        fields = _case_to_jira_fields(case, project_key, issue_type)
        existing_key, search_ok = (
            find_existing_jira_issue(case_id, project_key, base_url, api_version, request_kwargs)
            if case_id else ("", True)
        )
        try:
            if existing_key:
                # Only fields Jira accepts on edit: `project` and `issuetype` are
                # create-only, and sending them typically draws a 400 (UNVERIFIED
                # against a live Jira - set _EDITABLE_FIELDS to match your instance).
                resp = requests.put(
                    f"{base_url}/rest/api/{api_version}/issue/{existing_key}",
                    timeout=15,
                    json={"fields": {k: v for k, v in fields.items() if k in _EDITABLE_FIELDS}},
                    **request_kwargs,
                )
                resp.raise_for_status()
                result = {"test_case_id": case_id, "jira_key": existing_key,
                          "action": "updated", "error": None}
            else:
                resp = requests.post(
                    f"{base_url}/rest/api/{api_version}/issue",
                    timeout=15, json={"fields": fields}, **request_kwargs,
                )
                resp.raise_for_status()
                try:
                    jira_key = (resp.json() or {}).get("key")
                except (ValueError, AttributeError):
                    jira_key = None
                if not jira_key:
                    # A 2xx with no key cannot be linked, snapshotted or given steps;
                    # counting it created hid a failed upload behind jira_key None.
                    results.append({"test_case_id": case_id, "jira_key": None, "action": "error",
                                    "error": "JIRA create returned no issue key, so the "
                                             "issue could not be confirmed."})
                    continue
                result = {"test_case_id": case_id, "jira_key": jira_key,
                          "action": "created", "error": None}
                if not search_ok:
                    # Created WITHOUT a working duplicate check — if an issue
                    # for this case already existed from a previous run, this
                    # is a duplicate. Recorded so it's findable later rather
                    # than indistinguishable from a verified-new create.
                    result["duplicate_check"] = (
                        "failed — could not search JIRA for an existing issue with this "
                        "case's marker, so this create may have produced a duplicate")

            if tool in ("xray", "zephyr") and result.get("jira_key"):
                steps = _parse_numbered_steps(
                    case.get("Test Step", ""), case.get("Expected Result", ""), case.get("Test Data", ""))
                steps_warning = upload_structured_steps(base_url, request_kwargs, result["jira_key"], steps, tool)
                if steps_warning:
                    result["steps_warning"] = steps_warning
                    result["partial"] = True    # issue exists, its structured steps do not
            results.append(result)
        except requests.RequestException as exc:
            # _error_detail truncates AND redacts credential-looking values
            # (see _redact) — this string is persisted to
            # jira_upload_result.json, otherwise treated as a safe artifact.
            detail = _error_detail(exc)
            results.append({"test_case_id": case_id, "jira_key": existing_key or None, "action": "error",
                              "error": f"JIRA issue create/update failed: {detail}"})
    return results


def run(sheet_path: Path, feature_dir: Path, project_key: str,
        issue_type: str = "") -> Path:
    issue_type = issue_type or os.environ.get("JIRA_TEST_ISSUE_TYPE", "Test")
    all_cases = read_test_case_sheet(sheet_path)

    # Same review gate the orchestrator already enforces on the whole sheet
    # before this stage runs — checked again here so a standalone CLI
    # invocation (skipping the orchestrator entirely) can't upload
    # never-reviewed test cases to JIRA.
    unreviewed = [c.get("Test Case ID") for c in all_cases if (c.get("Status") or "").strip() != "Reviewed"]
    if unreviewed:
        raise ValueError(
            f"{len(unreviewed)} row(s) in '{sheet_path}' are not marked 'Reviewed' "
            f"(Test Case ID(s): {unreviewed}) — refusing to upload unreviewed test "
            f"cases to JIRA. Review the sheet first — set every row's Status to "
            f"exactly 'Reviewed' — then re-run."
        )

    prior_results = _load_prior_results(feature_dir)
    # A case already uploaded from THIS feature_dir, whose sheet content is
    # UNCHANGED since then, needs neither a create nor an update call —
    # create_jira_test_issues() would still do one search per case to check
    # for cross-feature_dir duplicates, so skip it here entirely for the
    # common re-run case (nothing changed, sheet re-read after a partial
    # failure elsewhere in the pipeline, etc).
    unchanged, to_upload = [], []
    for case in all_cases:
        prior = prior_results.get(case.get("Test Case ID"))
        current_fields = _case_to_jira_fields(case, project_key, issue_type)
        if prior and prior.get("jira_key") and prior.get("_fields_snapshot") == current_fields:
            unchanged.append(prior)
        else:
            to_upload.append(case)

    if not to_upload:
        print(f"      All {len(all_cases)} case(s) already uploaded and unchanged — nothing to do.")
        new_results = []
    else:
        print(f"      Uploading {len(to_upload)}/{len(all_cases)} case(s) "
              f"({len(unchanged)} unchanged since a prior upload)...")
        new_results = create_jira_test_issues(to_upload, project_key, issue_type)
        # Snapshot each successfully uploaded case's fields so the NEXT run
        # can tell "unchanged" from "changed" without re-fetching from JIRA.
        fields_by_case_id = {c.get("Test Case ID"): _case_to_jira_fields(c, project_key, issue_type)
                              for c in to_upload}
        for r in new_results:
            # No snapshot for a partial upload (its structured steps failed): the
            # next run then sees the case as changed and retries it.
            if r.get("jira_key") and not r.get("partial"):
                r["_fields_snapshot"] = fields_by_case_id.get(r.get("test_case_id"))

    combined = unchanged + new_results
    out_path = artifact_path(feature_dir, _RESULT_FILENAME)
    write_json(out_path, combined)

    created = sum(1 for r in new_results if r.get("action") == "created")
    updated = sum(1 for r in new_results if r.get("action") == "updated")
    failed = sum(1 for r in new_results if r.get("action") == "error")
    partial = sum(1 for r in new_results if r.get("partial"))
    print(f"      {created} created, {updated} updated, {len(unchanged)} unchanged"
          + (f", {failed} failed" if failed else "")
          + (f", {partial} with structured steps NOT uploaded (will be retried next run)"
             if partial else ""))
    if failed:
        print(f"      ⚠️  {failed} case(s) failed to upload — see {out_path}. The stage is "
              f"recorded as done; re-run with --force jira_uploader to retry them "
              f"(rows already uploaded and unchanged are skipped).")

    # 100% of attempted rows failing is a strong signal of a systemic API/
    # auth outage, not per-row flakiness — raise so the runner's existing
    # retry/halt logic actually engages instead of treating this as
    # a "successful" result full of per-row errors.
    if to_upload and failed == len(to_upload):
        raise RuntimeError(
            f"All {failed} JIRA uploads failed — likely a systemic API/auth outage, "
            f"not per-row issues. See {out_path} for per-row error detail."
        )

    return out_path


def _cli():
    """Standalone entry point — upload a reviewed sheet's test cases to
    JIRA without going through the rest of the pipeline. Needs no LLM/
    AI_PROVIDER key at all (see module docstring)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", required=True, help="Reviewed test case .xlsx path.")
    parser.add_argument("--project-key", required=True, help="Target JIRA project key.")
    parser.add_argument("--issue-type", default="",
                         help="JIRA issue type name for test issues (default: JIRA_TEST_ISSUE_TYPE "
                              "env var, or 'Test').")
    parser.add_argument("--feature-dir", required=True)
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)

    out_path = run(Path(args.sheet), feature_dir, args.project_key, args.issue_type)
    print(f"\n✅ Upload results written: {out_path}")


if __name__ == "__main__":
    _cli()
