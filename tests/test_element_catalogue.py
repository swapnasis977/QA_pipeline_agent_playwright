"""The Planner must be given the ledger's real element vocabulary.

WHY. On a live Vendor run the Planner — which received only the test case
and a POM summary, never the ledger — invented element names from the case's
prose ("Post code field") and invented a selector to match (`#Vendortype`,
which does not exist; the real element is `#Vendortype_select`). The gate
rejected them, the repair loop paid to guess again with no new information,
and the Coding Agent produced zero files across four attempts.

Four of the unresolved elements had no accessible name AT ALL on the running
app (`#CheckOIMS_1`, `#Company`, `#addressInfoLine1`, `#addressInfoZipCode`:
no label, no placeholder, no aria-label), so no amount of re-recording could
have let a prose-driven Planner address them. Only the selector can.

Run with:  python -m pytest tests/test_element_catalogue.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.brain import catalogue  # noqa: E402


class _FakeLedger:
    """Minimal stand-in: `collect` only needs load() and by_type()."""

    def __init__(self, cards):
        self._cards = cards

    def load(self):
        return None

    def by_type(self, card_type):
        return [c for c in self._cards if c.get("type") == card_type]


def _element(name, locator, screen, source="recorded", confidence=0.95, role=""):
    return {
        "type": "element",
        "id": f"element:{screen}/{name}",
        "source": source,
        "confidence": confidence,
        "value": {"logical_name": name, "locator": locator, "screen": screen, "role": role},
    }


def test_groundable_elements_are_grouped_by_screen():
    ledger = _FakeLedger([
        _element("basicInfoLastname", "#basicInfoLastname", "Profile Details"),
        _element("addressInfoZipCode", "#addressInfoZipCode", "Profile Details"),
        _element("btnContinue", "#btnContinue", "Context Information"),
    ])
    grouped = catalogue.collect(ledger)
    assert set(grouped) == {"Profile Details", "Context Information"}
    assert ("addressInfoZipCode", "#addressInfoZipCode", "") in grouped["Profile Details"]
    assert len(grouped["Profile Details"]) == 2


def test_ungroundable_sources_are_excluded():
    """A design_doc element is not vocabulary the Planner may use — the Coding
    Agent refuses it downstream, so offering it here only moves the failure."""
    ledger = _FakeLedger([
        _element("Real Field", "#real", "Screen A"),
        _element("Imagined Field", "#imagined", "Screen A",
                 source="design_doc", confidence=0.55),
    ])
    names = [e[0] for e in catalogue.collect(ledger)["Screen A"]]
    assert names == ["Real Field"]


def test_render_carries_the_selector_for_unnamed_elements():
    """The whole point: an element with no human-readable name is still
    addressable, because its selector is in the text."""
    ledger = _FakeLedger([_element("addressInfoZipCode", "#addressInfoZipCode", "Profile Details")])
    text = catalogue.render(ledger)
    assert "#addressInfoZipCode" in text
    assert "Profile Details" in text


def test_render_forbids_invention():
    ledger = _FakeLedger([_element("btnNext", "#btnNext", "Profile Details")])
    text = catalogue.render(ledger).lower()
    assert "only elements that exist" in text
    assert "do not invent" in text or "do not invent an element" in text


def test_role_is_carried_and_unknown_is_stated():
    """A Coding Agent called selectOption() on `#Vendortype_select` — an
    <a>, not a <select> — because it read the NAME. The role is what stops
    that, and "UNKNOWN" must be explicit rather than silently absent."""
    ledger = _FakeLedger([
        _element("Vendortype_select", "#Vendortype_select", "Profile Details"),
        _element("Category dropdown", "#cat", "Profile Details", role="combobox"),
    ])
    text = catalogue.render(ledger)
    assert "role=UNKNOWN" in text
    assert "role=combobox" in text
    assert "selectOption" in text


def test_empty_ledger_renders_nothing():
    """An empty catalogue must leave the Planner exactly as it was, rather
    than handing it an instruction it cannot follow."""
    assert catalogue.render(_FakeLedger([])) == ""


def test_named_screens_are_rendered_before_the_rest():
    ledger = _FakeLedger([
        _element("a", "#a", "Zzz Screen"),
        _element("b", "#b", "Profile Details"),
    ])
    text = catalogue.render(ledger, screens=["Profile Details"])
    assert text.index("Profile Details") < text.index("Zzz Screen")


def test_budget_truncates_rather_than_overflowing():
    ledger = _FakeLedger([
        _element(f"field{i}", f"#field{i}", f"Screen {i}") for i in range(200)
    ])
    text = catalogue.render(ledger, char_budget=400)
    assert "omitted to fit the prompt budget" in text
    assert len(text) < 2000
