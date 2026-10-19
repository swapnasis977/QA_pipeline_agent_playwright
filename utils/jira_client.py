"""Shared JIRA REST client — auth/version resolution, single-issue fetch,
comments, attachments and issue links.

Previously duplicated across `agents/testcase_writer.py` (fetch_jira_story)
and `agents/jira_uploader.py` (_jira_auth_config / _jira_request_kwargs).
Centralized here so Agent 1's hierarchy walk (Feature -> Epic -> Story ->
linked Test issues) and Agent 3's upload path read the same auth rules
instead of drifting. Pulled from jira_uploader's working logic — see commit
history there for how the auth-type detection was validated against the
self-hosted instance in .env.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import requests
from requests.auth import HTTPBasicAuth

#: Matches a Jira issue key anywhere in a pasted link or bare key —
#: "https://.../browse/PROJ-68308", "PROJ-68308", "...issue/PROJ-68308?...".
_ISSUE_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


def extract_issue_key(link_or_key: str) -> str:
    """Pulls the issue key out of a pasted Jira URL, or returns it unchanged
    if it already looks like a bare key. Returns "" if nothing matches,
    rather than raising — callers treat that as "no entry point given"."""
    match = _ISSUE_KEY_RE.search((link_or_key or "").strip())
    return match.group(1) if match else ""


def auth_config():
    """Same detection rules used across this pipeline's JIRA calls (see
    `agents.testcase_writer.fetch_jira_story`'s docstring for why): v3 only
    for *.atlassian.net Cloud, bearer PAT for everything else unless
    overridden."""
    # No trailing slash: every caller appends "/rest/api/...".
    base_url = os.environ.get("JIRA_BASE_URL", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "")
    token = os.environ.get("JIRA_API_TOKEN", "")
    api_version = os.environ.get("JIRA_API_VERSION") or ("3" if "atlassian.net" in base_url else "2")
    auth_type = os.environ.get("JIRA_AUTH_TYPE", "").lower() or \
        ("basic" if "atlassian.net" in base_url else "bearer")
    return base_url, email, token, api_version, auth_type


def request_kwargs(email: str, token: str, auth_type: str) -> dict:
    headers = {"Accept": "application/json"}
    auth = None
    if auth_type == "bearer":
        headers["Authorization"] = f"Bearer {token}"
    else:
        auth = HTTPBasicAuth(email, token)
    return {"headers": headers, "auth": auth}


class _JiraServiceUnavailableError(Exception):
    """A 429/5xx. The name contains 'ServiceUnavailableError' so
    utils.transient.is_transient treats it as retryable."""

    def __init__(self, message, response=None):
        super().__init__(message)
        self.response = response


def adf_to_text(node) -> str:
    """Flattens Atlassian Document Format (what API v3 returns for comment and
    description bodies) to plain text; a string passes through unchanged."""
    if not node:
        return ""
    if isinstance(node, str):
        return node
    parts: list = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "text":
                parts.append(n.get("text", ""))
            elif n.get("type") == "hardBreak":
                parts.append("\n")
            for child in n.get("content", []) or []:
                walk(child)
            if n.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
                parts.append("\n")
        elif isinstance(n, list):
            for child in n:
                walk(child)

    walk(node)
    return "".join(parts).strip()


class JiraClient:
    """Thin wrapper bound to one base_url/auth pair, built from the shared
    .env block (JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN / JIRA_AUTH_TYPE).
    Returns None/[] on any 4xx rather than raising — a missing linked issue,
    a deleted attachment or a locked-down comments endpoint should degrade
    the hierarchy walk, not abort the whole test-case run."""

    def __init__(self):
        self.base_url, email, token, self.api_version, auth_type = auth_config()
        self._kwargs = request_kwargs(email, token, auth_type)
        self.configured = bool(self.base_url and token and
                                (auth_type == "bearer" or email))

    def _get(self, path: str, **params):
        """One GET, degrading to None (with a printed warning) rather than
        raising. 429/5xx and dropped connections are retried a couple of times
        first via utils.transient.call_with_retry (honours Retry-After): a rate
        limit on one issue used to silently empty that issue out of the walk."""
        from utils.transient import call_with_retry

        def _once():
            resp = requests.get(f"{self.base_url}{path}", params=params or None,
                                timeout=15, **self._kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise _JiraServiceUnavailableError(
                    f"HTTP {resp.status_code}", response=resp)
            return resp

        try:
            resp = call_with_retry(_once, label=f"JIRA GET {path}",
                                   max_retries=2, base_delay=1.0)
        except (requests.RequestException, _JiraServiceUnavailableError) as exc:
            # A timeout/connection drop/persistent 5xx degrades the same as a
            # 4xx (see the class docstring) — a flaky blip on one issue in a
            # multi-story walk must not crash the whole batch. Said out loud,
            # though: it used to return None with no trace.
            print(f"⚠️  JIRA request for {path} failed after retries "
                  f"({type(exc).__name__}: {exc}) — continuing without it.")
            return None
        if resp.status_code in (401, 403):
            # A bad or expired token looked exactly like "issue not found".
            print(f"⚠️  JIRA refused the request (HTTP {resp.status_code}) for {path} — "
                  f"check JIRA_API_TOKEN / JIRA_AUTH_TYPE; continuing without it.")
            return None
        if resp.status_code >= 400:
            return None
        try:
            return resp.json()
        except ValueError:
            # An SSO/login page returned with HTTP 200 is HTML, not JSON.
            print(f"⚠️  JIRA returned a non-JSON page for {path} (a login redirect?) — "
                  f"continuing without it.")
            return None

    def get_issue(self, key: str) -> dict | None:
        if not key:
            return None
        return self._get(f"/rest/api/{self.api_version}/issue/{key}")

    def get_comments(self, key: str) -> list:
        data = self._get(f"/rest/api/{self.api_version}/issue/{key}/comment")
        comments = (data or {}).get("comments", [])
        # v3 bodies are ADF trees; flatten to text here so no caller has to.
        return [{**c, "body": adf_to_text(c.get("body"))} if isinstance(c, dict) else c
                for c in comments]

    def list_attachments(self, issue: dict) -> list:
        """Attachment metadata already embedded in the issue payload's
        fields.attachment — no extra request needed."""
        return (issue or {}).get("fields", {}).get("attachment", [])

    def download_attachment(self, attachment: dict, dest_dir: Path) -> Path | None:
        """Downloads one attachment into dest_dir, named exactly as in Jira.
        Returns None on failure so the caller can skip it and keep going."""
        url = attachment.get("content")
        filename = attachment.get("filename")
        if not url or not filename:
            return None
        dest_dir.mkdir(parents=True, exist_ok=True)
        # The filename is controlled by whoever attached the file. Keep only
        # its last component and drop characters Windows forbids, so
        # `..\..\x` cannot climb out of dest_dir and two files cannot
        # silently overwrite each other through a shared sanitized name.
        safe = re.sub(r'[<>:"/\|?*\x00-\x1f]+', "_", Path(str(filename).replace("\\", "/")).name) or "attachment"
        dest = dest_dir / safe
        counter = 1
        while dest.exists():
            dest = dest_dir / f"{Path(safe).stem}_{counter}{Path(safe).suffix}"
            counter += 1
        try:
            resp = requests.get(url, timeout=30, **self._kwargs)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
            return dest
        except requests.RequestException:
            return None

    def get_issue_links(self, issue: dict) -> list:
        """Raw `issuelinks` field entries — each has either `inwardIssue` or
        `outwardIssue`, with its own `fields.issuetype.name`/`fields.summary`."""
        return (issue or {}).get("fields", {}).get("issuelinks", [])

    def linked_issues_of_type(self, issue: dict, issue_type_name: str) -> list:
        """Linked issues (either direction) whose own issue type matches —
        used to pull Zephyr 'Test' issues linked to a Story, per
        JIRA_TEST_ISSUE_TYPE in .env."""
        out = []
        for link in self.get_issue_links(issue):
            linked = link.get("inwardIssue") or link.get("outwardIssue")
            if not linked:
                continue
            if linked.get("fields", {}).get("issuetype", {}).get("name") == issue_type_name:
                out.append(linked)
        return out

    def children_of(self, key: str, issue_types: tuple[str, ...] = ()) -> list:
        """Direct and indirect descendants of `key` via the `childIssuesOf`
        JQL function (covers both the modern parent-child hierarchy and
        classic Epic Link), paginated past the API's single-page cap.
        Filters to `issue_types` when given (e.g. ("Epic",) or
        ("Story", "Task")) — omit to get every descendant, Sub-tasks
        included."""
        if not key:
            return []
        jql = f"issuekey in childIssuesOf({key})"
        # ENDPOINT: `GET /rest/api/3/search` is removed on Jira Cloud (replaced
        # by `/rest/api/3/search/jql`); Server/DC v2 only has `/search`. Try
        # Cloud's endpoint first on api_version 3 and fall back on 404/410 —
        # same two-endpoint handling as jira_uploader.find_existing_jira_issue,
        # which this client's docstring says it was pulled from. Without this,
        # `self._get` silently returns None on the 404 (by design) and every
        # Cloud instance's hierarchy walk comes back empty with no warning.
        endpoint = ("search/jql" if self.api_version == "3" else "search")
        out, start_at, page_size, page_token = [], 0, 100, None
        seen_tokens = set()
        while True:
            params = {"jql": jql, "maxResults": page_size, "fields": "issuetype,summary,parent"}
            if endpoint == "search/jql":
                # Cloud's search/jql paginates by nextPageToken and returns no
                # `total`; the startAt/total loop below stopped after page 1,
                # truncating anything past 100 children.
                if page_token:
                    params["nextPageToken"] = page_token
            else:
                params["startAt"] = start_at
            data = self._get(f"/rest/api/{self.api_version}/{endpoint}", **params)
            if data is None and endpoint == "search/jql" and not out:
                # This deployment doesn't have the Cloud endpoint after all —
                # fall back and restart pagination from the top.
                endpoint = "search"
                out, start_at, page_token = [], 0, None
                continue
            if not data:
                break
            page = data.get("issues", [])
            out.extend(page)
            start_at += len(page)
            if endpoint == "search/jql":
                page_token = data.get("nextPageToken")
                if not page or data.get("isLast") or not page_token or page_token in seen_tokens:
                    break
                seen_tokens.add(page_token)
            elif start_at >= data.get("total", 0) or not page:
                break
        if issue_types:
            out = [i for i in out if i.get("fields", {}).get("issuetype", {}).get("name") in issue_types]
        return out

    def parent_key(self, issue: dict) -> str:
        """Epic/parent key via the modern `parent` field (team-managed and
        most Data Center projects), falling back to the classic 'Epic Link'
        custom field name some instances still use instead."""
        fields = (issue or {}).get("fields", {})
        parent = fields.get("parent")
        if parent:
            return parent.get("key", "")
        for field_key, value in fields.items():
            if field_key.startswith("customfield_") and isinstance(value, str) \
                    and _ISSUE_KEY_RE.fullmatch(value):
                return value
        return ""
