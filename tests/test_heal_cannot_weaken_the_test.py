"""A heal may change how a step finds an element, never what the test proves.

THE FAILURE. A real product bug gets classified as a fixable test issue; the
healer "fixes" it by weakening the assertion (`toBe(80)` -> `toBeTruthy()`), the
re-run goes green, and the case is reported healed. The defect is now hidden
behind a passing test. The rule lived only in the skill, i.e. as a request.

Run with:  python -m pytest tests/test_heal_cannot_weaken_the_test.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils.heal_guard import weakened  # noqa: E402

_SPEC = """
import { test, expect } from '@fixtures';
test('TC-1 score shown', async ({ page }) => {
  await page.getByRole('button', { name: 'Compare' }).click();
  await expect(page.locator('#score')).toHaveText('80');
  await expect(page.locator('#status')).toBeVisible();
});
test('TC-2 reset', async ({ page }) => {
  await expect(page.locator('#reset')).toBeEnabled();
});
"""


def test_a_new_locator_for_the_same_assertions_is_a_legitimate_heal():
    healed = _SPEC.replace("getByRole('button', { name: 'Compare' })", "locator('#btnCompare')")
    assert weakened(_SPEC, healed) == []


def test_removing_an_assertion_is_refused():
    healed = _SPEC.replace("  await expect(page.locator('#status')).toBeVisible();\n", "")
    assert any("removes 1 assertion" in r for r in weakened(_SPEC, healed))


def test_turning_a_value_check_into_a_truthy_check_is_refused():
    healed = _SPEC.replace("toHaveText('80')", "toBeTruthy()")
    assert any("toBeTruthy" in r for r in weakened(_SPEC, healed))


def test_force_clicks_and_swallowed_errors_are_refused():
    forced = _SPEC.replace(".click()", ".click({ force: true })")
    swallowed = _SPEC.replace(".click()", ".click().catch(() => {})")
    assert any("force: true" in r for r in weakened(_SPEC, forced))
    assert any("swallowed error" in r for r in weakened(_SPEC, swallowed))


def test_dropping_or_skipping_a_test_is_refused():
    assert any("drops 1 test" in r for r in weakened(_SPEC, _SPEC.split("test('TC-2")[0]))
    skipped = _SPEC.replace("test('TC-2", "test.skip('TC-2")
    assert any("test.skip" in r for r in weakened(_SPEC, skipped))


def test_adding_assertions_is_fine():
    assert weakened(_SPEC, _SPEC + "\n  await expect(page.locator('#x')).toBeVisible();\n") == []


def test_a_commented_out_assertion_does_not_count_as_one():
    commented = _SPEC.replace("  await expect(page.locator('#status')).toBeVisible();",
                              "  // await expect(page.locator('#status')).toBeVisible();")
    assert any("removes 1 assertion" in r for r in weakened(_SPEC, commented))
