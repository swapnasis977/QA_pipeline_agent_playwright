"""A JIRA attachment cannot choose where it is written.

THE FAILURE. `dest_dir / filename` used the externally controlled name
verbatim: a name starting with dot-dot climbs out of the folder, `a:b?.png` is
illegal on Windows, and two attachments with the same name silently overwrote
each other.
Also: an SSO redirect (HTTP 200, HTML body) crashed the whole hierarchy walk
with an uncaught JSONDecodeError, and a bad token looked like "issue not found".

Run with:  python -m pytest tests/test_jira_attachment_filenames.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest import mock

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import jira_client as jc  # noqa: E402


class _Download:
    content = b"png"

    def raise_for_status(self):
        pass


def _client():
    client = jc.JiraClient.__new__(jc.JiraClient)
    client._kwargs = {}
    client.base_url, client.api_version = "https://jira.example", "2"
    return client


def test_a_traversal_filename_stays_inside_the_folder(tmp_path):
    with mock.patch.object(jc.requests, "get", return_value=_Download()):
        saved = _client().download_attachment(
            {"content": "u", "filename": "..\..\evil:name?.png"}, tmp_path)
    assert saved.parent == tmp_path and saved.is_file()
    assert not any(c in saved.name for c in '<>:"/\|?*')


def test_two_attachments_with_the_same_name_do_not_overwrite_each_other(tmp_path):
    attachment = {"content": "u", "filename": "shot.png"}
    with mock.patch.object(jc.requests, "get", return_value=_Download()):
        first = _client().download_attachment(attachment, tmp_path)
        second = _client().download_attachment(attachment, tmp_path)
    assert first != second and first.is_file() and second.is_file()


class _Response:
    def __init__(self, status, body="", json_error=False):
        self.status_code, self._body, self._json_error = status, body, json_error

    def json(self):
        if self._json_error:
            raise ValueError("not json")
        return self._body


def test_an_html_login_page_with_http_200_degrades_instead_of_crashing(capsys):
    with mock.patch.object(jc.requests, "get", return_value=_Response(200, json_error=True)):
        assert _client()._get("/rest/api/2/issue/PROJ-1") is None
    assert "non-JSON" in capsys.readouterr().out


def test_a_refused_token_is_reported_not_mistaken_for_a_missing_issue(capsys):
    with mock.patch.object(jc.requests, "get", return_value=_Response(401)):
        assert _client()._get("/rest/api/2/issue/PROJ-1") is None
    assert "refused" in capsys.readouterr().out


def test_a_trailing_slash_on_the_base_url_is_dropped(monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.example/ ")
    assert jc.auth_config()[0] == "https://jira.example"


def test_jira_cloud_gets_an_adf_description_and_server_gets_text(monkeypatch):
    from agents import jira_uploader
    case = {"Test Case ID": "T-1", "Name": "n", "Description": "first\nsecond"}
    monkeypatch.delenv("JIRA_API_VERSION", raising=False)
    monkeypatch.setenv("JIRA_BASE_URL", "https://acme.atlassian.net/")
    cloud = jira_uploader._case_to_jira_fields(case, "P", "Test")["description"]
    assert cloud["type"] == "doc" and len(cloud["content"]) == 2
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.corp.example/")
    assert isinstance(jira_uploader._case_to_jira_fields(case, "P", "Test")["description"], str)
