"""Regression tests for the element-matching and plan-collision defects.

Every case in here is a REAL failure observed against the NSWDCJ application
on 2026-09-03, not a synthetic example. They are the cheapest possible proof
that the two most expensive bugs of that session cannot come back:

  1. `similarity()` matched on a raw character substring, so the card "register"
     scored 0.95 against "...submit the registration" — it sits inside the word
     "registration". Three unrelated controls resolved to one card because of it.
  2. Nothing compared a plan's resolutions to EACH OTHER, so those three
     controls all carrying the same locator passed every per-element check.

Run with:  python -m pytest tests/test_locator_matching.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.brain.store import _FUZZY_THRESHOLD, similarity  # noqa: E402
from qa_pipeline.verifiers.locator_check import _check_card_collisions  # noqa: E402


def _matches(wanted: str, name: str) -> bool:
    """True when the resolver would accept `name` as the card for `wanted`."""
    return similarity(wanted, name) >= _FUZZY_THRESHOLD


# ---------------------------------------------------------------------------
# similarity() — must NOT match
# ---------------------------------------------------------------------------

def test_short_card_name_does_not_match_inside_a_longer_word():
    """The exact collision: 'register' is a substring of 'registration'.

    A plan asking for the Submit button must not resolve to the 'Register' card
    just because the letters e-n-r-o-l appear inside the word 'registration'.
    """
    assert not _matches("submit button to create and submit the registration", "register")


def test_success_modal_does_not_match_the_register_card():
    assert not _matches("registration successful modal", "register")


def test_continue_button_does_not_match_the_register_card():
    assert not _matches("continue (after registration success)", "register")


def test_single_word_buried_in_a_concatenated_blob_still_refused():
    """The older regression this guard was originally written for.

    'recapture' appears verbatim inside a banner's concatenated text, but the
    banner is not the button — matching it laundered an invented locator
    through the grounding gate.
    """
    assert not _matches(
        "recapture",
        "non compliant image. 1 attempts remaining. recapture reconnect reset")


# ---------------------------------------------------------------------------
# similarity() — must STILL match
# ---------------------------------------------------------------------------

def test_short_name_matches_across_a_non_alphanumeric_boundary():
    """'register' IS 'register/update' — '/' is a boundary, not more letters."""
    assert _matches("register", "register/update")


def test_multi_word_phrase_matches_its_own_verbose_label():
    """The gap that left the Planner with no legitimate Submit candidate.

    'button' is a generic word, so 'Submit button' counts as a ONE-word
    request and used to be capped at ~0.42 against its own real label — under
    the threshold, so the Submit button could not resolve to itself.
    """
    assert _matches("submit button",
                    "submit button to create and submit the registration")


def test_short_request_matches_long_accessible_name():
    assert _matches(
        "start capture button",
        "start capture button. click to capture the face and iris of the candidate")


def test_identical_names_match():
    assert similarity("manage identity", "manage identity") == 1.0


# ---------------------------------------------------------------------------
# plan collision detection
# ---------------------------------------------------------------------------

def test_one_card_serving_controls_on_two_screens_is_an_error():
    """The observed defect, as the Planner actually emitted it."""
    elements = [
        {"logical_name": "Submit button to create and submit the registration",
         "screen": "Review", "card_id": "element:capture/register",
         "locator": "page.getByText('Register', { exact: true })"},
        {"logical_name": "Registration successful modal",
         "screen": "Success Modal", "card_id": "element:capture/register",
         "locator": "page.getByText('Register', { exact: true })"},
        {"logical_name": "Continue (after registration success)",
         "screen": "Success Modal", "card_id": "element:capture/register",
         "locator": "page.getByText('Register', { exact: true })"},
    ]
    errors, repairs, warnings = _check_card_collisions("SANITY2-TC-005", elements)
    assert errors, "three controls on two screens sharing one card must fail the gate"
    assert "cannot be on two screens" in errors[0]
    assert repairs, "a failing gate must hand the Planner a repair instruction"


def test_relabelled_control_on_one_screen_is_only_a_warning():
    """The legitimate case the rule must not break.

    This application relabels the same #startCapture button to 'Recapture'
    after a non-compliant image. A plan naming both is correct, so it may not
    be an error — one element genuinely serves two names on ONE screen.
    """
    elements = [
        {"logical_name": "Start Capture button", "screen": "Capture Device",
         "card_id": "element:capture device/startcapture",
         "locator": "page.locator('#startCapture')"},
        {"logical_name": "Recapture button", "screen": "Capture Device",
         "card_id": "element:capture device/startcapture",
         "locator": "page.locator('#startCapture')"},
    ]
    errors, repairs, warnings = _check_card_collisions("REG-TC-001", elements)
    assert not errors, "a relabelled control on one screen must not fail the gate"
    assert warnings, "it should still be surfaced for review"


def test_distinct_elements_produce_no_findings():
    elements = [
        {"logical_name": "Surname", "screen": "Profile Details",
         "card_id": "element:personal details/surname", "locator": "#basicInfoLastname"},
        {"logical_name": "First Name", "screen": "Profile Details",
         "card_id": "element:personal details/first name", "locator": "#basicInfoFirstName"},
    ]
    errors, repairs, warnings = _check_card_collisions("REG-TC-001", elements)
    assert not errors and not warnings


def test_unresolved_elements_are_ignored_by_the_collision_check():
    """Elements with no card_id have not resolved to anything yet — they are
    the other checks' business, and must not be reported as colliding."""
    elements = [
        {"logical_name": "Something new", "screen": "A"},
        {"logical_name": "Something else new", "screen": "B"},
    ]
    errors, repairs, warnings = _check_card_collisions("TC", elements)
    assert not errors and not warnings
