"""The locator the Planner grounded is the one the code must use.

THE DEFECT THIS FILE EXISTS FOR, traced end to end on a real run:

  1. The ledger held the button, crawled from the live app:
       page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")
  2. The Planner resolved it correctly — 31/31 elements grounded — and emitted
       {"logical_name": "Registration. Click to access Registration",
        "card_id": "element:dashboard/registration. click to access registration module",
        "locator": "page.locator(\\"section > section > ...\\")"}
  3. The Coding Agent wrote:
       getByRole('button', { name: 'Registration. Click to access Registration' })
     — the logical_name pasted into a getByRole, while the verified selector
     sat in the very next field of the same object.
  4. No gate noticed: `gate_code` takes (file, ledger, repo_root, screen,
     snapshot_elements) and has never been given the plan, so nothing ever
     asked "did you use the locator we grounded?"

The pipeline computed the right answer, dropped it, guessed, and had no check
that would notice. Three defects, one seam.

Also covered here: the Planner corrupting `logical_name`. The card is named
"...Click to access Registration module"; the plan said "...Click to access
Registration". The fuzzy resolver still matched, so the RIGHT locator was
attached under a WRONG name — and the name is what the Coding Agent then
built a selector from.

Run with:  python -m pytest tests/test_planned_locator_is_the_contract.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import locator_check  # noqa: E402

_REAL = 'page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(1)")'
_GUESSED = "getByRole('button', { name: 'Registration. Click to access Registration' })"


def _plan(locator=_REAL, name="Registration. Click to access Registration module"):
    return {
        "test_case_id": "VENDOR-TC-001",
        "target_file": "tests/e2e/vendor-registration.spec.ts",
        "elements": [{"logical_name": name, "screen": "Dashboard",
                      "card_id": "element:dashboard/registration", "locator": locator}],
    }


def _file(content):
    return {"path": "tests/e2e/vendor-registration.spec.ts", "kind": "spec",
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


# --- 1 & 2. the plan's locator is enforced, not suggested -----------------

def test_code_that_uses_the_planned_locator_passes():
    result = locator_check.check_against_plan(
        _file(f"const tile = {_REAL};\nawait tile.click();\n"), [_plan()])
    assert result.ok is True


def test_code_that_invents_a_selector_for_a_planned_element_fails():
    """The exact observed failure: a getByRole composed from logical_name
    while the grounded locator went unused."""
    result = locator_check.check_against_plan(
        _file(f"await page.{_GUESSED}.click();\n"), [_plan()])

    assert result.ok is False
    joined = " ".join(result.errors)
    assert "Registration" in joined
    # The repair must hand over the locator it should have used.
    assert any("section > section" in r for r in result.repairs)


def test_a_page_object_call_is_accepted():
    """A spec that delegates to a page object contains no locator of its own.
    That is the CONVENTION this project wants, so it must not be failed —
    the check only fires on a locator the file actually spells out."""
    result = locator_check.check_against_plan(
        _file("await register.clickRegistrationTile();\n"), [_plan()])
    assert result.ok is True


def test_an_element_the_plan_never_grounded_is_not_policed():
    """No planned locator means nothing to contradict."""
    result = locator_check.check_against_plan(
        _file(f"await page.{_GUESSED}.click();\n"), [_plan(locator="")])
    assert result.ok is True


def test_a_file_serving_no_planned_case_is_skipped():
    other = _file("await page.getByRole('button', { name: 'Anything' }).click();")
    other["test_case_ids"] = ["SOME-OTHER-TC-009"]
    assert locator_check.check_against_plan(other, [_plan()]).ok is True


# --- 3. the Planner must not corrupt the resolved name -------------------

def test_the_resolved_cards_real_name_replaces_the_models_paraphrase():
    """`logical_name` is what the Coding Agent reads. When the resolver
    matches a card fuzzily, the CARD's name is the truth — carrying the
    model's near-miss forward is what produced a third spelling of one
    button and the selector that failed every run."""
    element = {"logical_name": "Registration. Click to access Registration",
               "screen": "Dashboard"}
    card = {"id": "element:dashboard/registration",
            "value": {"logical_name": "Registration. Click to access Registration module",
                      "locator": _REAL}}

    locator_check.apply_resolved_card(element, card)

    assert element["logical_name"] == "Registration. Click to access Registration module"
    assert element["locator"] == _REAL
    assert element["card_id"] == "element:dashboard/registration"


def test_a_card_with_no_name_leaves_the_plans_name_alone():
    element = {"logical_name": "Save", "screen": "Registration"}
    locator_check.apply_resolved_card(element, {"id": "c1", "value": {"locator": "#save"}})
    assert element["logical_name"] == "Save"
    assert element["locator"] == "#save"


# --- 4. the accusation must name ONE element -----------------------------
#
# The name match was a plain `label in name` substring test, so every element
# whose name merely CONTAINED the locator's label was accused. On one real
# VENDOR run that produced eleven findings, all false, and cost a full
# extra generation round — every run, because the plan reproduces the same
# names each time. Each case below is one of those findings.

def _multi_plan(*names_and_locators):
    return {
        "test_case_id": "VENDOR-TC-001",
        "target_file": "tests/e2e/vendor-registration.spec.ts",
        "elements": [{"logical_name": n, "screen": "Registration",
                      "card_id": f"element:registration/{n.lower()}", "locator": loc}
                     for n, loc in names_and_locators],
    }


def test_a_one_character_label_is_not_charged_against_every_name_containing_it():
    """`getByRole('link', { name: '1' })` is a paginator. It was charged
    against BOTH 'CheckOIMS_1' and 'addressInfoLine1' because '1' is a
    substring of each."""
    plan = _multi_plan(("CheckOIMS_1", "page.locator('#CheckOIMS_1')"),
                       ("addressInfoLine1", "page.locator('#addressInfoLine1')"))
    result = locator_check.check_against_plan(
        _file("await page.locator('#CheckOIMS_1').check();\n"
              "await page.locator('#addressInfoLine1').fill('1 Test St');\n"
              "await page.getByRole('link', { name: '1' }).click();\n"), [plan])
    assert result.ok is True, result.errors


def test_an_exact_name_match_settles_which_element_a_locator_addresses():
    """'Register' is itself a planned element, grounded to exactly the
    expression the file uses. It was still charged against 'Registration.
    Click to access Registration module' and 'To continue with the
    registration', both of which merely contain the letters 'register'."""
    plan = _multi_plan(
        ("Register", "page.getByText('Register', { exact: true })"),
        ("Registration. Click to access Registration module", _REAL),
        ("To continue with the registration", 'page.locator("#btnContinue")'),
    )
    result = locator_check.check_against_plan(
        _file("await page.getByText('Register', { exact: true }).click();\n"), [plan])
    assert result.ok is True, result.errors


def test_a_suffix_of_a_name_is_a_different_element_not_a_truncation():
    """'MATCH FOUND' was charged against 'NO MATCH FOUND' — an element with
    the opposite meaning. The defect this check exists for is a TRUNCATED
    name (a prefix); a leading word can invert the meaning entirely."""
    plan = _multi_plan(
        ("NO MATCH FOUND", "page.getByText('NO MATCH FOUND', { exact: true })"))
    result = locator_check.check_against_plan(
        _file("await page.getByText('MATCH FOUND', { exact: true }).isVisible();\n"),
        [plan])
    assert result.ok is True, result.errors


def test_each_wrong_locator_is_reported_even_when_an_earlier_one_matched():
    """The loop ran element-outer/locator-inner and broke after its first
    hit, so a second invented locator in the same file went unexamined."""
    plan = _multi_plan(
        ("Registration. Click to access Registration module", _REAL),
        ("Manage Identity. Click to access Manage Identity module",
         'page.locator("section > section > div:nth-of-type(1) > button:nth-of-type(3)")'),
    )
    result = locator_check.check_against_plan(
        _file("await page.getByRole('button', "
              "{ name: 'Registration. Click to access Registration' }).click();\n"
              "await page.getByRole('button', "
              "{ name: 'Manage Identity. Click to access Manage Identity' }).click();\n"),
        [plan])
    assert result.ok is False
    joined = " ".join(result.errors)
    assert "Registration" in joined and "Manage Identity" in joined
    assert len(result.errors) == 2, result.errors
