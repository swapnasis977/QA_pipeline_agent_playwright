"""An end-to-end spec is checked against every screen it visits, not the first.

THE DEFECT, observed on a real VENDOR run. The case walks Dashboard ->
Context Information -> Capture Device -> Profile Details -> Review -> Manage
Identity Search, twelve screens in all. `stage_code` handed the gate ONE
screen — the first element's — and every locator in the file was resolved
against it.

The ledger holds two different controls whose names are similar:

    element:dashboard/click here to search           'Click here to Search'
    element:manage identity search/search-button     'Search, button'

The spec's `Search, button` belongs to the second. Scoped to 'Dashboard' it
fuzzily matched the FIRST, and the gate reported:

    'Search, button' resolves to a known element, but that element's real
    accessible name is 'Click here to Search'.

Correct code, failed by the gate, on every run — the plan names the same
screens each time, so the repair round was permanent.

Run with:  python -m pytest tests/test_flow_spans_many_screens.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import locator_check  # noqa: E402


def _card(screen, name, locator):
    return {"id": f"element:{screen.lower()}/{name.lower()}", "type": "element",
            "trust": "confirmed", "source": "app_explorer",
            "value": {"screen": screen, "logical_name": name,
                      "accessible_name": name, "locator": locator, "unique": True}}


_DASHBOARD_SEARCH = _card("Dashboard", "Click here to Search",
                          'page.locator("#searchMenu")')
_MANAGE_SEARCH = _card("Manage Identity Search", "Search, button",
                       "page.getByRole('button', { name: 'Search, button' })")


class _TwoScreenLedger:
    """Reproduces `store.Ledger`'s contract for the two calls this gate makes:
    an exact keyed lookup, and a fuzzy lookup that falls back across screens
    when the scoped pool misses."""

    def __init__(self, cards):
        self.cards = cards

    def find_element(self, screen, logical_name):
        wanted = f"element:{(screen or '').lower()}/{(logical_name or '').lower()}"
        return next((c for c in self.cards if c["id"] == wanted), None)

    def resolve_element(self, logical_name, screen="", groundable_only=True):
        wanted = (logical_name or "").lower()
        scoped = [c for c in self.cards
                  if not screen
                  or c["value"]["screen"].lower() == (screen or "").lower()]
        for pool in (scoped, self.cards):
            for card in pool:
                name = card["value"]["logical_name"].lower()
                # The fuzzy behaviour that mattered: 'search, button' and
                # 'click here to search' share the word 'search'.
                if wanted == name or "search" in wanted and "search" in name:
                    return card
        return None

    def unresolved_reason(self, name, screen):
        return "not in the ledger"


def _file(content):
    return {"path": "tests/e2e/vendor-registration.spec.ts", "kind": "spec",
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


_SPEC = ("await page.getByRole('button', { name: 'Search, button' }).click();\n")


def test_one_screen_scoping_is_what_produced_the_false_failure():
    """Kept as the control: with only the entry screen, the wrong card wins.
    This is the behaviour `screens` replaces, asserted so the test above is
    demonstrably testing something."""
    ledger = _TwoScreenLedger([_DASHBOARD_SEARCH, _MANAGE_SEARCH])
    result = locator_check.check_file(_file(_SPEC), ledger,
                                      screen="Dashboard", allow_crawl=False)
    assert result.ok is False
    assert any("Click here to Search" in e for e in result.errors)


def test_a_locator_for_a_later_screen_resolves_to_that_screens_card():
    ledger = _TwoScreenLedger([_DASHBOARD_SEARCH, _MANAGE_SEARCH])
    result = locator_check.check_file(
        _file(_SPEC), ledger, screen="Dashboard",
        screens=["Dashboard", "Context Information", "Manage Identity Search"],
        allow_crawl=False)
    assert result.ok is True, result.errors


def test_an_exact_hit_on_a_later_screen_beats_a_fuzzy_hit_on_the_first():
    """Screen order is preserved, but an EXACT name match anywhere in the
    flow outranks a fuzzy match on the entry screen — otherwise the first
    screen still wins every near-miss."""
    ledger = _TwoScreenLedger([_DASHBOARD_SEARCH, _MANAGE_SEARCH])
    card = locator_check._resolve_in_screens(
        ledger, "Search, button", ["Dashboard", "Manage Identity Search"])
    assert card["id"] == _MANAGE_SEARCH["id"]


def test_a_name_no_visited_screen_knows_still_fails():
    """Widening the search must not turn an invented locator into a pass."""
    ledger = _TwoScreenLedger([_DASHBOARD_SEARCH, _MANAGE_SEARCH])
    result = locator_check.check_file(
        _file("await page.getByRole('button', { name: 'Invented' }).click();\n"),
        ledger, screen="Dashboard",
        screens=["Dashboard", "Manage Identity Search"], allow_crawl=False)
    assert result.ok is False
