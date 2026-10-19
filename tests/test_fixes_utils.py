"""Regression tests for the utils/ review fixes. Each one opens with the real failure it prevents.

Browser-dependent ones skip when no Chromium is installed.
"""

import json

import pytest

from utils import dom_inspector, dom_navigation, dom_snapshot, env_config, jira_client, route_map


# ---------------------------------------------------------------- MCP snapshot parsing

def test_a_state_token_before_the_ref_does_not_lose_the_ref():
    # `- checkbox "X" [checked] [ref=e3]` parsed with ref=None, so the element
    # could be seen but never clicked again.
    (el,) = dom_snapshot._parse_mcp_snapshot('- checkbox "Remember me" [checked] [ref=e3]')
    assert el["ref"] == "e3" and el["name"] == "Remember me"


def test_a_name_with_escaped_quotes_is_read_whole():
    (el,) = dom_snapshot._parse_mcp_snapshot(r'- button "Say \"hi\"" [ref=e4]')
    assert el["name"] == 'Say "hi"' and el["ref"] == "e4"


def test_mcp_role_locators_are_only_issued_when_they_hit_one_element():
    # getByRole('button', {name:'Save'}) also matches "Save draft" (strict-mode
    # violation at run time); two identical "Close" can never be told apart.
    els = dom_snapshot._parse_mcp_snapshot(
        '- button "Save" [ref=e1]\n- button "Save draft" [ref=e2]\n'
        '- button "Close" [ref=e3]\n- button "Close" [ref=e4]')
    by = {e["ref"]: e for e in els}
    assert json.loads(by["e1"]["locator"]) == {"role": "button", "name": "Save", "exact": True}
    assert json.loads(by["e2"]["locator"]) == {"role": "button", "name": "Save draft"}
    assert by["e3"]["locator"] is None and by["e4"]["locatorType"] is None


# ---------------------------------------------------------------- compact serializer

def test_compact_drops_what_the_prompt_never_needs_and_leaves_the_original_alone():
    snap = {"url": "u", "title": "", "raw_snapshot": "x" * 4000, "elements": [
        {"tag": "button", "role": "button", "type": None, "name": "Save", "id": None,
         "testId": None, "locator": json.dumps({"role": "button", "name": "Save"}),
         "locatorType": "role", "ref": "e1"},
        {"tag": "input", "role": "textbox", "type": "text", "name": "Age", "id": "age",
         "testId": None, "locator": "#age", "locatorType": "id"},
        {"tag": "button", "role": "button", "type": None, "name": "Close", "id": None,
         "testId": None, "locator": "div > button", "locatorType": "css"},
        {"tag": "button", "role": "button", "type": None, "name": "Close", "id": None,
         "testId": None, "locator": "div > button", "locatorType": "css"},
    ]}
    before = json.dumps(snap, sort_keys=True)
    out = dom_snapshot.compact(snap)
    assert json.dumps(snap, sort_keys=True) == before
    assert "raw_snapshot" not in out and "title" not in out
    save, age, close = out["elements"]
    assert save == {"role": "button", "name": "Save", "locatorType": "role"}
    assert "id" not in age and age["locator"] == "#age"
    assert close["n"] == 2
    assert len(json.dumps(out)) < len(json.dumps(snap)) / 4


# ---------------------------------------------------------------- IDP detection / login

@pytest.mark.parametrize("url,expected", [
    ("https://login.microsoftonline.com/t/oauth2", True),
    ("https://sts.corp.com/adfs/ls", True),
    ("https://tests.corp/app", False),          # "sts." bare substring used to hit
    ("https://posts.example.com/home", False),
    ("https://app.example.com/home?next=sts.corp", False),   # query is not the host
    ("https://acme.okta.com/login", True),
])
def test_only_the_host_decides_whether_we_are_still_at_the_idp(url, expected):
    assert dom_navigation.is_idp_url(url) is expected


class _Loc:
    def __init__(self, n):
        self._n = n

    def count(self):
        return self._n

    def nth(self, _i):
        return self

    def is_visible(self):
        return True


class _Page:
    def __init__(self, url, passwords):
        self.url, self._pw = url, passwords

    def locator(self, _sel):
        return _Loc(self._pw)

    def inner_text(self, _sel):
        return "Incorrect password"


def test_an_app_login_form_that_rejected_the_credentials_is_not_a_success():
    # The app serves its own login form at its own URL; "no IDP marker in the URL"
    # used to mean success even though the password box was still on screen.
    assert dom_inspector._looks_logged_in(_Page("https://app.example.com/login", 1)) is False
    assert dom_inspector._looks_logged_in(_Page("https://app.example.com/home", 0)) is True


# ---------------------------------------------------------------- keyword navigation

def test_filler_words_do_not_use_up_the_keyword_budget_and_destructive_words_are_never_keywords():
    kws = dom_navigation.extract_keywords(
        "Verify that the user can Remove an item when Registration Capture is open")
    assert "Registration" in kws and "Capture" in kws
    assert not {"Verify", "that", "user", "when", "Remove"} & set(kws)
    assert dom_navigation.is_destructive("Sign out") and dom_navigation.is_destructive("Delete all")
    assert not dom_navigation.is_destructive("Registration")


def test_a_route_alias_must_match_a_whole_word():
    # alias "adm" matched inside "html2".
    routes = {"r": {"name": "Tool", "url": "", "clicks": ["x"], "aliases": ["adm"], "expect": ""}}
    assert route_map.match("open the html2 page", routes) is None
    assert route_map.match("open the adm page", routes)["name"] == "Tool"


# ---------------------------------------------------------------- env_config

def test_an_exported_plain_var_beats_the_prefixed_one_and_a_partial_fallback_is_named(
        monkeypatch, tmp_path, capsys):
    # The shell/CI said APP_BASE_URL explicitly; the LOCAL_ value used to overwrite it.
    # And a central run with only CENTRAL_APP_BASE_URL silently took the plain password.
    env_file = tmp_path / ".env"
    env_file.write_text("APP_TEST_USER=fromfile\n", encoding="utf-8")
    env_config._resolved.clear()
    monkeypatch.setenv("TEST_ENV", "central")
    monkeypatch.setenv("APP_BASE_URL", "https://exported")
    monkeypatch.setenv("CENTRAL_APP_BASE_URL", "https://central")
    monkeypatch.setenv("APP_TEST_USER", "fromfile")
    monkeypatch.setenv("CENTRAL_APP_TEST_USER", "central-user")
    monkeypatch.setenv("APP_TEST_PASSWORD", "plain-secret")
    monkeypatch.delenv("CENTRAL_APP_TEST_PASSWORD", raising=False)
    env_config.resolve_app_env(env_files=[env_file])
    out = capsys.readouterr().out
    import os
    assert os.environ["APP_BASE_URL"] == "https://exported"
    assert os.environ["APP_TEST_USER"] == "central-user"      # came from the file: prefixed wins
    assert "CENTRAL_APP_TEST_PASSWORD" in out                  # fallback is named...
    assert "plain-secret" not in out                           # ...never the value
    env_config._resolved.clear()


# ---------------------------------------------------------------- jira client

class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code, self._p = status, payload or {}

    def json(self):
        return self._p


def _client(version="3"):
    c = jira_client.JiraClient.__new__(jira_client.JiraClient)
    c._kwargs, c.base_url, c.api_version = {}, "https://jira.example", version
    return c


def test_children_past_the_first_page_are_not_dropped_on_cloud(monkeypatch):
    # search/jql paginates by nextPageToken and returns no `total`; the old loop stopped after page 1.
    pages = [{"issues": [{"key": "A-1"}], "nextPageToken": "t2"},
             {"issues": [{"key": "A-2"}], "isLast": True}]
    seen = []

    def fake_get(url, params=None, **_kw):
        seen.append(dict(params or {}))
        return _Resp(200, pages[len(seen) - 1])

    monkeypatch.setattr(jira_client.requests, "get", fake_get)
    got = _client().children_of("A-0")
    assert [i["key"] for i in got] == ["A-1", "A-2"]
    assert seen[1]["nextPageToken"] == "t2"


def test_a_rate_limited_request_is_retried_then_degrades_with_a_warning(monkeypatch, capsys):
    calls = []

    def fake_get(*_a, **_k):
        calls.append(1)
        return _Resp(429)

    monkeypatch.setattr(jira_client.requests, "get", fake_get)
    monkeypatch.setattr("utils.transient.random.uniform", lambda *_a: 0)
    assert _client()._get("/rest/api/3/issue/A-1") is None
    assert len(calls) == 3                      # 1 try + 2 retries
    assert "JIRA request" in capsys.readouterr().out

    calls.clear()
    seq = [_Resp(503), _Resp(200, {"key": "A-1"})]
    monkeypatch.setattr(jira_client.requests, "get", lambda *_a, **_k: seq.pop(0))
    assert _client()._get("/rest/api/3/issue/A-1") == {"key": "A-1"}


def test_v3_comment_bodies_come_back_as_text_not_adf(monkeypatch):
    adf = {"type": "doc", "content": [{"type": "paragraph",
                                         "content": [{"type": "text", "text": "hello"}]}]}
    monkeypatch.setattr(jira_client.requests, "get",
                        lambda *_a, **_k: _Resp(200, {"comments": [{"body": adf}]}))
    assert _client().get_comments("A-1")[0]["body"] == "hello"


# ---------------------------------------------------------------- recording parser

def test_an_in_app_email_field_is_kept_and_only_the_login_prelude_is_skipped():
    from utils import recording_parser as rp

    text = """
    await page.getByLabel('Email').fill('me@x.com');
    await page.getByLabel('Password').fill('pw');
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.getByRole('button', { name: 'New Registration' }).click();
    await page.getByLabel('Email address').fill('a@b.com');
    await page.getByRole('button', { name: 'Continue' }).click();
    """
    parsed = rp.parse_recording(text)
    names = [s["name"] for s in parsed["steps"]]
    assert names == ["New Registration", "Email address", "Continue"]
    assert len(parsed["skipped_login"]) == 3


def test_a_name_containing_parentheses_is_parsed_not_dropped():
    from utils import recording_parser as rp

    parsed = rp.parse_recording(
        "await page.getByRole('button', { name: 'Save (draft)' }).click();")
    assert [s["name"] for s in parsed["steps"]] == ["Save (draft)"]
    assert parsed["dropped"] == []


# ---------------------------------------------------------------- final suite

def test_opposite_states_are_never_merged_and_no_epic_is_baked_in(tmp_path):
    from utils import final_suite as fs
    from utils import master_sheet as ms

    def row(name):
        return {"Name": name, "Description": "", "Execution Type (as written)": "Automated",
                "Automation Status": ms.AUTOMATABLE, "Automation ID": "",
                "Test Case ID": f"S-{abs(hash(name)) % 9999}", "Expected Result": "1. ok",
                "Test Step": "1. do", "Story": "PROJ-1", "Story title": "", "_source": {}}

    a = "Verify the Save button is disabled when the form is empty"
    b = "Verify the Save button is enabled when the form is valid"
    assert fs.opposing_states(a, b)
    assert len(fs.select([row(a), row(b)])["suite"]) == 2

    import openpyxl
    suite = fs.select([row("Verify Zoom In scales up")])["suite"]
    out = fs.write_final_sheet(suite, tmp_path / "f.xlsx", epic="ABC-1")
    ws = openpyxl.load_workbook(out).active
    headers = [c.value for c in ws[1]]
    assert ws.cell(row=2, column=headers.index("JIRA Ref (Epic/Story)") + 1).value == "ABC-1 / PROJ-1"


def test_a_scope_rule_with_a_bad_decision_is_skipped_not_a_keyerror():
    from utils import final_suite as fs

    scope = {"decisions": dict(fs._DEFAULT_DECISIONS), "default": "new",
             "rules": [{"pattern": "zoom", "decision": "nonsense"}, {"decision": "new"},
                       {"pattern": "zoom", "decision": "blocked"}]}
    rows = [{"Name": "Zoom In", "Execution Type (as written)": "Automated",
             "Automation Status": "x", "Test Case ID": "1"}]
    fs.annotate(rows, scope)
    assert rows[0]["Scope Decision"] == scope["decisions"]["blocked"]


# ---------------------------------------------------------------- explore / pom_builder

def test_a_role_name_that_is_a_substring_of_another_gets_exact_true_not_an_ambiguous_locator():
    from qa_pipeline.brain import explore

    def el(name):
        return {"role": "button", "name": name, "locatorType": "role",
                "locator": json.dumps({"role": "button", "name": name})}

    cards = explore.snapshot_to_cards({"url": "http://h/a", "elements": [el("Save"), el("Save draft")]}, "S")
    locs = {c["value"]["logical_name"]: c["value"]["locator"] for c in cards if c.get("type") == "element"}
    assert "exact: true" in locs["Save"] and "exact: true" not in locs["Save draft"]


def test_describe_can_be_limited_to_the_screens_a_chunk_uses():
    from agents import pom_builder

    pages = [{"class_name": "A", "path": "p/A.ts", "screen": "Home", "members": [("x", "x")]},
             {"class_name": "B", "path": "p/B.ts", "screen": "Edit", "members": [("y", "y")]}]
    assert "p/B.ts" not in pom_builder.describe(pages, screens=["home"])
    assert "p/A.ts" in pom_builder.describe(pages, screens=["home"])
    assert "p/B.ts" in pom_builder.describe(pages) and "p/A.ts" in pom_builder.describe(pages)
    assert pom_builder.describe(pages, screens=[]) == ""


# ---------------------------------------------------------------- MCP client

def test_a_timed_out_coroutine_is_cancelled_not_left_running():
    import asyncio

    from utils import playwright_mcp_client as c

    state = {"cancelled": False}

    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    try:
        with pytest.raises(Exception):
            c._run_coro(slow(), timeout=0.2)
        import time
        time.sleep(0.3)
        assert state["cancelled"]
    finally:
        c.close()


# ---------------------------------------------------------------- browser-backed snapshot

HTML = """
<body>
<nav>%s</nav>
<button id="tooltip73348">Help</button>
<div role="dialog" aria-label="Confirm"><button>Yes please</button></div>
<h5>Dup</h5><h5>Dup</h5>
<div role="status" aria-label="Dup">s</div>
</body>
""" % "".join(f"<a href='/p{i}'>Item {i}</a>" for i in range(40))


@pytest.fixture(scope="module")
def snap():
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(HTML)
            out = {"small": dom_snapshot._snapshot(page, max_elements=10),
                   "full": dom_snapshot._snapshot(page, max_elements=120)}
            browser.close()
            return out
    except Exception as exc:
        pytest.skip(f"no usable Chromium: {exc}")


def test_a_modal_at_the_end_of_the_body_survives_a_full_sidebar(snap):
    # DOM-order truncation cut the dialog first once the menu filled the cap.
    assert any(e["name"] == "Yes please" for e in snap["small"]["elements"])


def test_a_per_render_id_is_not_used_as_the_locator(snap):
    help_el = [e for e in snap["full"]["elements"] if e["name"] == "Help"]
    assert help_el and help_el[0]["locatorType"] != "id"


def test_an_explicit_role_is_not_called_unique_when_getbyrole_sees_headings_too(snap):
    # role=status "Dup" is alone in its role, but this checks the role count is by role, not tag.
    dup = [e for e in snap["full"]["elements"] if e["name"] == "Dup" and e["role"] == "status"]
    assert dup and dup[0]["locatorType"] == "role"
