"""A declared route that did not land must not become evidence about a screen.

THE FAILURE. Contextual Information's dialog has two elements with id
`#btnContinue`; the first, in DOM order, is a hidden "Resume Current
Registration" Cancel button. The route's `css=#btnContinue` step clicked that one,
"succeeded", and the crawl went on to snapshot the page it never left. That
snapshot was filed under "Image Editing Tool", the Planner then found the screen's
controls missing, and 135 elements arrived unresolved.

Three things went wrong in a row, and each is pinned here:
  1. a `css=` step took the first match in DOM order, not the visible one;
  2. nothing checked that the screen the route promises was actually there;
  3. the crawler recorded whatever it was handed.

Also covered, because they came out of the same run:
  - a shared page object re-asked the model to repair byte-identical content
    18 times (see the skip in agents/coding_agent.py);
  - split spec files were named `_part3`, which meant a different file on every
    run — they are now named by the case ids they hold.

Run with:  python -m pytest tests/test_route_must_land.py -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import coding_agent  # noqa: E402
from qa_pipeline.brain import explore  # noqa: E402
from utils import dom_inspector, route_map  # noqa: E402

# --- 1. duplicate ids ------------------------------------------------------

class _El:
    def __init__(self, visible):
        self._visible = visible

    def is_visible(self):
        return self._visible


class _Matches:
    def __init__(self, elements):
        self._elements = elements

    def count(self):
        return len(self._elements)

    def nth(self, i):
        return self._elements[i]

    @property
    def first(self):
        return self._elements[0]


class _Page:
    def __init__(self, elements):
        self._elements = elements

    def locator(self, selector):
        return _Matches(self._elements)


def test_css_step_clicks_the_visible_duplicate_not_the_first_in_dom_order():
    hidden_cancel, visible_continue = _El(False), _El(True)
    page = _Page([hidden_cancel, visible_continue])
    assert dom_inspector._first_visible(page, "#btnContinue") is visible_continue


def test_when_nothing_is_visible_it_still_returns_the_first_match():
    only = _El(False)
    assert dom_inspector._first_visible(_Page([only]), "#x") is only


# --- 2. the route must prove it landed ------------------------------------

def test_route_map_keeps_the_expect_selector(tmp_path, monkeypatch):
    path = tmp_path / "routes.json"
    path.write_text(json.dumps({"tool": {"clicks": ["Open"], "expect": "#ieSyncView"}}))
    monkeypatch.setenv("APP_ROUTE_MAP", str(path))
    assert route_map.load(force=True)["tool"]["expect"] == "#ieSyncView"


def test_a_route_without_expect_is_trusted_as_before():
    assert dom_inspector._route_landed(object(), {"name": "r", "expect": ""}) is True


class _NeverThere:
    def locator(self, selector):
        raise RuntimeError("not on this page")


def test_a_route_whose_marker_is_missing_did_not_land():
    assert dom_inspector._route_landed(_NeverThere(), {"name": "r", "expect": "#x"}) is False


# --- 3. the crawler refuses an unlanded route -----------------------------

class _Ledger:
    def __init__(self):
        self.saved = []

    def screen_matching(self, *a, **k):
        return None

    def upsert_many(self, cards):
        self.saved.extend(cards)
        return {"added": len(cards), "updated": 0, "kept_existing": 0}


def _crawl_with(monkeypatch, snapshot):
    monkeypatch.setattr(dom_inspector, "inspect", lambda context, **k: snapshot)
    ledger = _Ledger()
    report = explore.crawl(ledger, [{"screen": "Image Editing Tool",
                                     "context": "image editing tool"}], verbose=False)
    return ledger, report


_DASHBOARD = {"logged_in": True, "elements": [
    {"tag": "button", "role": "button", "name": "Dashboard", "locatorType": "role",
     "locator": "page.getByRole('button', { name: \"Dashboard\" })"}]}


def test_a_route_that_fell_back_to_keywords_is_not_recorded(monkeypatch):
    ledger, report = _crawl_with(monkeypatch, {**_DASHBOARD, "route_failed": "image-editing-tool"})
    assert ledger.saved == []
    assert report["visited"] == [] and "keyword" in report["failed"][0]["reason"]


def test_a_route_whose_marker_never_appeared_is_not_recorded(monkeypatch):
    ledger, report = _crawl_with(monkeypatch, {**_DASHBOARD, "route_unverified": "image-editing-tool"})
    assert ledger.saved == []
    assert "expected element" in report["failed"][0]["reason"]


# --- 4. one repair attempt per distinct content ----------------------------

def test_split_files_are_named_by_the_cases_they_contain():
    cases = [{"Test Case ID": "PROJ-68294-AUTO-TC-014"}, {"Test Case ID": "PROJ-68294-AUTO-TC-019"}]
    label = coding_agent._part_label(cases, 3)
    assert label == "tc014-019"
    assert coding_agent._insert_part_suffix(
        "tests/e2e/adjustments.spec.ts", 3, label) == "tests/e2e/adjustments_tc014-019.spec.ts"


def test_a_split_without_case_ids_falls_back_to_the_counter():
    assert coding_agent._part_label([], 2) == "part2"
    assert coding_agent._insert_part_suffix("tests/e2e/a.spec.ts", 2) == "tests/e2e/a_part2.spec.ts"


# --- 5. snapshot cache ------------------------------------------------------

def test_a_cached_route_snapshot_skips_the_browser_walk(monkeypatch):
    route = {"name": "image-editing-tool", "url": "", "clicks": ["x"], "aliases": [], "expect": ""}
    monkeypatch.setattr(route_map, "match", lambda text, routes=None: route)
    monkeypatch.setattr(dom_inspector, "_get_page", lambda: object())
    monkeypatch.setattr(dom_inspector, "_dismiss_blocking_dialogs", lambda page: False)

    def _walk(*a, **k):
        raise AssertionError("must not re-walk the route on a cache hit")

    monkeypatch.setattr(dom_inspector, "_navigate_declared_route", _walk)
    dom_inspector._route_snapshots["image-editing-tool"] = (
        dom_inspector.time.monotonic(), {"elements": [{"name": "Brightness"}]})

    first = dom_inspector.inspect("image editing tool", cache_ttl=600)
    assert first["cached"] is True and first["elements"][0]["name"] == "Brightness"
    dom_inspector._route_snapshots.clear()
