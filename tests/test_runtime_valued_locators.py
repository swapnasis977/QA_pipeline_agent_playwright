"""A locator naming a value the test itself minted is not a claim about the app.

THE DEFECT, observed on a real VENDOR run. The spec generates a unique
surname per run and asserts the registration landed:

    const surname = `VendorSurname${Date.now()}`;
    ...
    await expect(page.getByText(surname, { exact: false })).toBeVisible();

That assertion is the whole point of the test — it is how the case proves the
record was created rather than merely that a button was clicked. But the
locator extractor only parses a STRING LITERAL name, so `name` came back
empty, `_locator_label` fell back to the entire raw expression, and
`check_file` looked "getByText(surname, { exact: false })" up in the ledger as
if it were an element's accessible name. Nothing crawled contains a value
minted this run, so it could only fail — and it failed EVERY run, because the
regenerated attempt writes the same necessary assertion again. One extra full
generation round, permanently, for correct code.

Un-checkable, like a raw CSS chain — not wrong.

Run with:  python -m pytest tests/test_runtime_valued_locators.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import locator_check  # noqa: E402
from utils import locator_verifier  # noqa: E402


class _EmptyLedger:
    """Knows nothing, so every lookup is a miss — the state in which the
    defect bit. A dynamic name must not even be asked about."""

    def __init__(self):
        self.asked = []

    def find_element(self, screen, logical_name):
        return None

    def resolve_element(self, label, screen="", groundable_only=True):
        self.asked.append(label)
        return None


def _file(content):
    return {"path": "tests/e2e/vendor-registration.spec.ts", "kind": "spec",
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


# --- extraction ----------------------------------------------------------

def test_a_variable_argument_is_marked_dynamic_not_named():
    found = locator_verifier.extract_locators(
        "await expect(page.getByText(surname, { exact: false })).toBeVisible();")
    assert len(found) == 1
    assert found[0]["name"] == ""
    assert found[0]["dynamic_name"] is True


def test_a_string_literal_argument_is_not_dynamic():
    found = locator_verifier.extract_locators(
        "await page.getByText('NO MATCH FOUND', { exact: true }).click();")
    assert found[0]["name"] == "NO MATCH FOUND"
    assert found[0]["dynamic_name"] is False


def test_a_role_name_built_at_run_time_is_dynamic():
    found = locator_verifier.extract_locators(
        "await page.getByRole('row', { name: rowLabel }).click();")
    assert found[0]["name"] == ""
    assert found[0]["dynamic_name"] is True


def test_a_role_with_no_name_option_is_not_dynamic():
    """No name constraint at all is a different thing from an unparsed one,
    and the two used to be indistinguishable — both leave `name` empty."""
    found = locator_verifier.extract_locators("await page.getByRole('button').click();")
    assert found[0]["name"] == ""
    assert found[0]["dynamic_name"] is False


# --- the gate ------------------------------------------------------------

def test_a_run_time_value_is_a_warning_not_a_gate_failure():
    ledger = _EmptyLedger()
    result = locator_check.check_file(
        _file("const surname = `S${Date.now()}`;\n"
              "await expect(page.getByText(surname, { exact: false })).toBeVisible();\n"),
        ledger, screen="Manage Identity", allow_crawl=False)

    assert result.ok is True, result.errors
    assert any("run time" in w for w in result.warnings), result.warnings


def test_the_ledger_is_never_asked_about_a_run_time_value():
    """The lookup itself was the bug: the whole expression was passed in as
    an element name, so the ledger answered about a string no crawl could
    ever hold."""
    ledger = _EmptyLedger()
    locator_check.check_file(
        _file("await page.getByText(surname, { exact: false }).click();\n"),
        ledger, screen="Manage Identity", allow_crawl=False)
    assert ledger.asked == []


def test_a_literal_name_the_ledger_does_not_know_still_fails():
    """The fix must not silence the check it lives inside."""
    ledger = _EmptyLedger()
    result = locator_check.check_file(
        _file("await page.getByRole('button', { name: 'Invented' }).click();\n"),
        ledger, screen="Dashboard", allow_crawl=False)
    assert result.ok is False
    assert "Invented" in ledger.asked


# --- a locator with no name at all ---------------------------------------

def test_a_role_only_locator_is_not_looked_up_as_an_element_name():
    """`getByRole('row')` constrains the role and nothing else, so there is
    no name for a name-keyed ledger to answer about. `_locator_label` fell
    back to the raw expression and looked "getByRole('row')" up as an
    accessible name — it missed, and failed the gate. `verify_against_
    snapshot` already treats an unnamed locator as un-corroborable; the two
    checks disagreed."""
    ledger = _EmptyLedger()
    result = locator_check.check_file(
        _file("const row = page.getByRole('row').first();\nawait row.click();\n"),
        ledger, screen="Manage Identity Results", allow_crawl=False)

    assert result.ok is True, result.errors
    assert ledger.asked == []
    assert any("only the role" in w for w in result.warnings), result.warnings
