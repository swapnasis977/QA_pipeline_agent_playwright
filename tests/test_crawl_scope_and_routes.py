"""Which screens a run crawls, and by which path it reaches them.

TWO PROBLEMS, both surfaced by watching a Vendor run crawl the
Identification screen and select "Member" on the way to the capture device.

1. SCOPE. `stage_explore` crawled every screen the profile declares, on every
   run, regardless of the story. A Vendor registration has nothing to do with
   the Identification module, so that crawl was pure cost — and, worse, it made
   the run log read as though the pipeline had wandered off. The CLI's own
   `explore` command has always accepted a screen filter
   (`_explore_targets(profile, screen_args)`); the pipeline stage simply never
   passed one.

2. ROUTE. Reaching the capture device means choosing a transaction type first,
   and the route map hard-codes "Member":

       "registration-capture": { "clicks": [..., "css=#transactionType_select",
                                         "Member", "css=#btnContinue"] }

   So every capture-screen card in the ledger is evidence gathered under the
   Member flow, whatever story is running. This project's own design docs say
   transaction types differ in their mandatory fields and in whether Category
   carries over, so grounding a Vendor case on Member evidence is an
   assumption nobody checked. A `registration-capture-vendor` route already
   existed in routes.json, complete and unused, because no declared screen
   named its alias.

Run with:  python -m pytest tests/test_crawl_scope_and_routes.py -q
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.cli import _explore_targets  # noqa: E402
from utils import route_map  # noqa: E402


class _Profile:
    def __init__(self, screens):
        self._screens = screens

    def get(self, key, default=None):
        return self._screens if key == "app.screens" else default


_SCREENS = [
    {"name": "Dashboard", "context": "dashboard home screen"},
    {"name": "Registration", "context": "registration context screen"},
    {"name": "Capture Device", "context": "registration capture screen"},
    {"name": "Identification", "context": "identification capture screen"},
]


# --- 1. scope -------------------------------------------------------------

def test_no_filter_still_crawls_every_declared_screen():
    """The default must not change — a first run needs the whole map."""
    targets = _explore_targets(_Profile(_SCREENS))
    assert [t["screen"] for t in targets] == [
        "Dashboard", "Registration", "Capture Device", "Identification"]


def test_a_filter_narrows_the_crawl_but_keeps_each_screens_context():
    """The context is what `route_map.match` searches for a declared route.
    Dropping it — as a naive filter would — turns a precise declared route
    into blind keyword navigation, which is how screens went UNREACHABLE."""
    targets = _explore_targets(_Profile(_SCREENS), ["Dashboard", "Capture Device"])

    assert [t["screen"] for t in targets] == ["Dashboard", "Capture Device"]
    assert [t["context"] for t in targets] == [
        "dashboard home screen", "registration capture screen"]


def test_the_filter_is_case_and_space_insensitive():
    targets = _explore_targets(_Profile(_SCREENS), ["capture device"])
    assert [t["screen"] for t in targets] == ["Capture Device"]


def test_a_screen_the_profile_never_declared_is_still_crawlable():
    """An operator naming an ad-hoc screen must not be silently dropped."""
    targets = _explore_targets(_Profile(_SCREENS), ["Some New Screen"])
    assert [t["screen"] for t in targets] == ["Some New Screen"]
    assert targets[0]["context"] == "Some New Screen"


# --- 2. route -------------------------------------------------------------

def _routes():
    """The real loader, not raw JSON — `match` consumes the normalized shape
    `load` produces, and this is the exact path the pipeline reads."""
    # A frozen copy of a real route map that has two transaction types (Member and
    # Vendor), kept under tests/fixtures so these cases do not depend on any
    # application's workspace folder existing.
    os.environ["APP_ROUTE_MAP"] = str(_ROOT / "tests" / "fixtures" / "route_map_with_vendor_route.json")
    try:
        return route_map.load(force=True)
    finally:
        os.environ.pop("APP_ROUTE_MAP", None)


def test_a_vendor_context_takes_the_vendor_route():
    """The bug in one assertion: a Vendor capture crawl must not be
    routed through the Member transaction type."""
    route = route_map.match("vendor capture screen", _routes())

    assert route is not None
    assert "Vendor" in route["clicks"]
    assert "Member" not in route["clicks"]


def test_the_generic_capture_context_is_unchanged():
    """The existing Member route stays exactly as it was for callers that
    ask for the generic capture screen."""
    route = route_map.match("registration capture screen", _routes())
    assert "Member" in route["clicks"]


def test_the_profile_declares_the_vendor_capture_screen():
    """A route nothing names is a route that never runs — which is why this
    one sat complete and unused in routes.json."""
    project = json.loads((_ROOT / "tests" / "fixtures" / "profile_screens_with_vendor.json")
                         .read_text(encoding="utf-8"))
    contexts = [s.get("context", "") for s in project["app"]["screens"]]
    assert any(route_map.match(c, _routes()) and
               "Vendor" in (route_map.match(c, _routes()) or {}).get("clicks", [])
               for c in contexts), \
        "no declared screen reaches the vendor capture route"
