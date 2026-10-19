"""Locator verification must agree with what Playwright would actually resolve.

THE FAILURES (read from `verify_against_snapshot`):
  * containment ran BOTH ways, so a real "Save" button corroborated an invented
    `getByRole('button', { name: 'Save changes' })` — a false pass that fails at
    run time;
  * with no element of the wanted ROLE it fell back to every element, so a
    button named "Save" corroborated a `link` locator;
  * a regex name (`/^Save$/i`) was compared as the literal text "^save$", so a
    correct locator came back "unverified" and was sent for a pointless repair;
  * the same element arriving twice (two cases on one screen, a pooled snapshot)
    made every unique locator look ambiguous.

Run with:  python -m pytest tests/test_locator_matches_like_playwright.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import locator_verifier as lv  # noqa: E402

_SNAPSHOT = [
    {"role": "button", "name": "Save", "locator": "page.getByRole('button', { name: 'Save' })"},
    {"role": "button", "name": "Cancel", "locator": "page.getByRole('button', { name: 'Cancel' })"},
]


def _verify(source, elements=None):
    return lv.verify_file(source, _SNAPSHOT if elements is None else elements)


def test_a_longer_invented_name_is_not_corroborated_by_a_shorter_real_one():
    result = _verify("await page.getByRole('button', { name: 'Save changes' }).click();")
    assert len(result["unverified"]) == 1


def test_a_shorter_name_inside_the_real_one_still_verifies():
    elements = [{"role": "button", "name": "Save changes", "locator": "x"}]
    assert _verify("page.getByRole('button', { name: 'Save' })", elements)["unverified"] == []


def test_a_role_with_no_elements_is_not_satisfied_by_another_role():
    result = _verify("await page.getByRole('link', { name: 'Save' }).click();")
    assert len(result["unverified"]) == 1


def test_a_regex_name_is_matched_as_a_pattern():
    result = _verify("await page.getByRole('button', { name: /^Save$/i }).click();")
    assert result["unverified"] == []


def test_a_regex_that_matches_two_elements_is_ambiguous():
    result = _verify("await page.getByRole('button', { name: /a/i }).click();")
    assert result["unverified"] and result["unverified"][0].get("ambiguous_matches")


def test_a_regex_that_matches_nothing_is_unverified():
    assert len(_verify("page.getByRole('button', { name: /^Delete$/i })")["unverified"]) == 1


def test_the_same_element_seen_twice_is_not_ambiguous():
    doubled = _SNAPSHOT + _SNAPSHOT
    assert _verify("page.getByRole('button', { name: 'Save', exact: true })", doubled)["unverified"] == []
