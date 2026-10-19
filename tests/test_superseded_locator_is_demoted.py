"""A locator a heal disproved must be demoted — the RIGHT card, by locator.

When the Self-Healer swaps locator X for locator Y and the verification re-run
then passes, that is proof: X is wrong, Y is right. `writeback.from_heal`
exists to record exactly that, and it demotes the superseded card so it stops
grounding future code.

It looked up the card to demote like this:

    stale = ledger.resolve_element(old, groundable_only=False)

`old` is a LOCATOR STRING — `page.getByRole('button', { name: 'X' })` — and
`resolve_element` matches on `logical_name`. It was searching element NAMES for
a selector: a category error that either finds nothing (the disproved locator
keeps its confidence forever) or fuzzy-matches some unrelated card and demotes
that one instead.

This is why the Registration tile stayed broken across every run of this project.
The ledger held it twice — `recorded` at 0.95 with a `getByRole` that fails at
replay, and `crawled` at 0.85 with a CSS selector that works — so the WRONG
entry outranked the right one, and the heal that proved it wrong could never
demote it.

Run with:  python -m pytest tests/test_superseded_locator_is_demoted.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from qa_pipeline.brain import cards, open_ledger, writeback  # noqa: E402

_BAD = "page.getByRole('button', { name: 'Registration. Click to access' })"
_GOOD = 'page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")'


@pytest.fixture
def ledger(tmp_path):
    led = open_ledger(tmp_path)
    led.upsert(cards.element(
        screen="Dashboard tiles", logical_name="Registration. Click to access",
        locator=_BAD, source="recorded", confidence=0.95))
    led.upsert(cards.element(
        screen="Dashboard", logical_name="Registration. Click to access Registration module",
        locator=_GOOD, source="crawled", confidence=0.85))
    led.save()
    return led


def _heal(old, new, verified=True):
    return [{"test_case_id": "VENDOR-TC-001", "file": "tests/e2e/x.spec.ts",
             "old_locator": old, "new_locator": new,
             "verified": verified, "patch_applied": True, "healed": verified}]


def _card_with(led, locator):
    return next((c for c in led.by_type("element")
                 if (c.get("value") or {}).get("locator") == locator), None)


def test_the_disproved_locator_leaves_circulation(ledger):
    """ONE ELEMENT, ONE CARD. The card that carried the disproved locator is
    updated in place: its locator becomes the proven one and its source
    becomes `confirmed`. Keeping a demoted twin is impossible anyway — a
    card's id derives from screen+logical_name, so a second card for the same
    element computes the same id and simply overwrites the first."""
    tally = writeback.from_heal(ledger, _heal(_BAD, _GOOD))

    assert _card_with(ledger, _BAD) is None, "the bad locator must be gone"
    healed = [c for c in ledger.by_type("element")
              if (c.get("value") or {}).get("logical_name") == "Registration. Click to access"]
    assert healed and (healed[0].get("value") or {}).get("locator") == _GOOD
    assert healed[0].get("superseded") == _BAD
    assert tally["superseded"] == 1


def test_the_working_locator_is_left_at_full_confidence(ledger):
    """Only the disproved card moves. Demoting its neighbour would throw away
    the very locator the heal just proved correct."""
    writeback.from_heal(ledger, _heal(_BAD, _GOOD))

    untouched = [c for c in ledger.by_type("element")
                 if (c.get("value") or {}).get("logical_name")
                 == "Registration. Click to access Registration module"]
    assert untouched and untouched[0]["confidence"] == 0.85


def test_the_demoted_card_stops_grounding_code(ledger):
    """The point of demoting: `resolve_element` must stop handing it out."""
    writeback.from_heal(ledger, _heal(_BAD, _GOOD))

    resolved = ledger.resolve_element("Registration. Click to access", "Dashboard tiles")
    assert resolved is None or (resolved.get("value") or {}).get("locator") != _BAD


def test_an_unverified_heal_demotes_nothing(ledger):
    """A patch that was never re-run green proves nothing about the old
    locator, and must not be allowed to disprove it."""
    tally = writeback.from_heal(ledger, _heal(_BAD, _GOOD, verified=False))

    assert _card_with(ledger, _BAD)["confidence"] == 0.95
    assert tally["superseded"] == 0
    assert tally["ignored_unverified"] == 1


def test_an_unknown_old_locator_demotes_nothing(ledger):
    """No card carries this locator. Demoting a fuzzy near-match instead —
    which is what a name-based lookup did — would disprove an innocent card."""
    writeback.from_heal(ledger, _heal("page.getByTestId('nothing-like-this')", _GOOD))

    assert _card_with(ledger, _BAD)["confidence"] == 0.95
    assert _card_with(ledger, _GOOD)["confidence"] == 0.85


# --- the half that had never fired: what a VERIFIED heal teaches ----------
#
# No heal has ever passed its re-run on this project, so this path had never
# executed against the real ledger. It writes:
#
#     logical_name = new         <- the LOCATOR STRING as the element's name
#     screen       = "unknown"
#     source       = "confirmed" <- the highest trust tier
#
# A card no plan can ever ask for (no plan says logical_name:
# "page.getByRole(...)"), sitting at the top of the trust ladder. Pure
# pollution, and it would have started on the first successful heal.

_CSS = 'page.locator("#btnContinue")'
_ROLE = 'page.getByRole("button", { name: "Continue" })'


@pytest.fixture
def css_ledger(tmp_path):
    led = open_ledger(tmp_path)
    led.upsert(cards.element(screen="Capture", logical_name="Continue button",
                             locator=_CSS, source="crawled", confidence=0.85))
    led.save()
    return led


def test_a_locator_that_shares_no_text_with_its_name_is_still_demoted(css_ledger):
    """The lookup must key on the LOCATOR. A name-based search only worked
    when the selector happened to embed the element's name — never true for
    an #id or nth-child selector."""
    tally = writeback.from_heal(css_ledger, _heal(_CSS, _ROLE))

    assert _card_with(css_ledger, _CSS) is None
    assert tally["superseded"] == 1


def test_the_fix_is_learned_against_the_real_element_not_a_new_stub(css_ledger):
    """The heal proved that 'Continue button' on 'Capture' is now reached by
    the new locator. That is what the ledger should learn — not a nameless
    card called after a selector."""
    writeback.from_heal(css_ledger, _heal(_CSS, _ROLE))

    learned = _card_with(css_ledger, _ROLE)
    assert learned is not None
    value = learned.get("value") or {}
    assert value.get("logical_name") == "Continue button"
    assert value.get("screen", "").lower() == "capture"


def test_no_card_is_ever_named_after_a_selector(css_ledger):
    writeback.from_heal(css_ledger, _heal(_CSS, _ROLE))

    for card in css_ledger.by_type("element"):
        name = (card.get("value") or {}).get("logical_name") or ""
        assert not any(t in name for t in ("page.", "getBy", "locator(")), \
            f"card named after a selector: {name!r}"


def test_an_unattributable_fix_pollutes_nothing(css_ledger):
    """No card carried the old locator, so there is no element to attribute
    the new one to. Inventing a stub at 'confirmed' is worse than learning
    nothing."""
    before = len(css_ledger.by_type("element"))
    writeback.from_heal(css_ledger, _heal('page.locator("#nothing")', _ROLE))

    assert len(css_ledger.by_type("element")) == before
