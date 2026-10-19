"""Tests for the ledger's manual correction path.

Before `demote` existed, the only way a card's confidence could fall was the
Self-Healer replacing its locator at run time. A card that a HUMAN proved
wrong — including one sourced from a recording, sitting at 0.95 — stayed
authoritative forever and kept being handed to generated code. That is not a
hypothetical: the committed NSWDCJ recording carries a Surname edit-icon
locator that targets the wrong sub-element.

Run with:  python -m pytest tests/test_trust_demote.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.brain import cards, trust  # noqa: E402
from qa_pipeline.brain.store import Ledger  # noqa: E402


def _recorded_card():
    return cards.element(
        screen="update identity",
        logical_name="editbasicInfoLastname editable demographicAdditionalInfo",
        locator="page.locator('#editbasicInfoLastname > .editable > .demographicAdditionalInfo')",
        source="recorded",
        evidence="recording: NSWDCJ-IDENT-MANAGE")


def test_a_recorded_card_can_ground_code_before_it_is_disproved():
    card = _recorded_card()
    assert trust.can_ground_locator(card), (
        "a recorded locator is above the grounding floor — that is the whole "
        "premise this test is about to qualify")


def test_demote_drops_a_card_below_the_grounding_floor():
    card = _recorded_card()
    demoted = trust.demote(card, "targets the label wrapper, not the edit icon; "
                                 "clicking it leaves the input disabled", "2026-09-04T00:00:00Z")
    assert not trust.can_ground_locator(demoted), (
        "a disproved card must stop being usable to ground generated code")
    assert demoted["confidence"] <= trust.DISPROVED_CONFIDENCE


def test_demote_keeps_the_original_source_rather_than_rewriting_history():
    """What happened is 'a recording said this and the recording was wrong'.

    Rewriting the source to 'inferred' would lose that, and the next reader
    would have no idea a human demonstration had ever asserted it.
    """
    demoted = trust.demote(_recorded_card(), "wrong sub-element", "2026-09-04T00:00:00Z")
    assert demoted["source"] == "recorded"
    assert demoted["disproved"] is True
    assert "wrong sub-element" in demoted["disproved_reason"]


def test_demote_does_not_mutate_the_original_card():
    card = _recorded_card()
    trust.demote(card, "wrong", "2026-09-04T00:00:00Z")
    assert card.get("disproved") is None
    assert card["confidence"] == trust.BASE_CONFIDENCE["recorded"]


def test_ledger_demote_persists_and_removes_it_from_resolution(tmp_path):
    ledger = Ledger(tmp_path)
    card = _recorded_card()
    ledger.upsert(card)
    ledger.save()

    # Resolvable while trusted.
    assert ledger.resolve_element(
        "editbasicInfoLastname editable demographicAdditionalInfo",
        "update identity", groundable_only=True) is not None

    demoted = ledger.demote(card["id"], "targets the wrong sub-element")
    assert demoted is not None
    ledger.save()

    # A fresh Ledger reading the same file must see the demotion.
    reloaded = Ledger(tmp_path).load()
    assert reloaded.get(card["id"])["disproved"] is True
    assert reloaded.resolve_element(
        "editbasicInfoLastname editable demographicAdditionalInfo",
        "update identity", groundable_only=True) is None, (
        "a disproved card must not come back as a groundable resolution")


def test_ledger_demote_on_an_unknown_id_returns_none(tmp_path):
    assert Ledger(tmp_path).demote("element:nope/nothing", "n/a") is None
