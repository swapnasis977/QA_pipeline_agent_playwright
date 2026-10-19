"""A heal that keeps the NUMBER of assertions but changes what they check.

The failure these prevent: a product bug is mislabelled "fixable", the healer
"fixes" it by swapping `toHaveText('Saved')` for `toBeVisible()`. The count of
`expect(` calls is unchanged, so the old guard let it through and the defect was
reported healed. Equally: `.catch(e => ...)` and `try {}` swallow a failure but
escaped a regex that only knew `.catch(() =>`.
"""

from utils.heal_guard import weakened

BASE = """
test('PROJ-1-TC-001 saves', async ({ page }) => {
  await page.getByRole('button', { name: 'Save' }).click();
  await expect(page.getByTestId('status')).toHaveText('Saved');
});
"""


def test_swapping_the_expected_value_for_a_weaker_matcher_is_refused():
    after = BASE.replace(".toHaveText('Saved')", ".toBeVisible()")
    assert any("no longer appear" in r and "toHaveText('Saved')" in r for r in weakened(BASE, after))


def test_changing_the_expected_text_is_refused():
    after = BASE.replace("'Saved'", "'Saving'")
    assert weakened(BASE, after)


def test_reformatting_an_assertion_is_not_a_change():
    after = BASE.replace(".toHaveText('Saved')", '.toHaveText( "Saved" )')
    assert weakened(BASE, after) == []


def test_a_locator_only_fix_is_allowed():
    after = BASE.replace("name: 'Save'", "name: 'Save changes'")
    assert weakened(BASE, after) == []


def test_every_spelling_of_a_swallowed_error_is_refused():
    for swallow in ("await x.click().catch(e => {});",
                    "await x.click().catch(function () {});",
                    "await x.click().catch(() => null);"):
        after = BASE.replace("});\n", swallow + "\n});\n", 1) if swallow else BASE
        assert any("swallowed" in r for r in weakened(BASE, after)), swallow


def test_a_try_block_around_a_step_is_refused():
    after = BASE.replace("  await page.getByRole",
                         "  try { await page.getByRole").replace(".click();", ".click(); } catch {}", 1)
    assert any("try/catch" in r for r in weakened(BASE, after))


def test_a_hard_wait_is_refused():
    after = BASE.replace("  await expect", "  await page.waitForTimeout(3000);\n  await expect")
    assert any("waitForTimeout" in r for r in weakened(BASE, after))


def test_force_true_is_refused_without_a_stated_reason():
    after = BASE.replace(".click();", ".click({ force: true });")
    assert any("force: true" in r for r in weakened(BASE, after))


def test_force_true_is_allowed_when_the_reason_is_written_down():
    after = BASE.replace(
        "  await page.getByRole('button', { name: 'Save' }).click();",
        "  // qa-allow-force: real <select> hidden behind a styled widget\n"
        "  await page.getByRole('button', { name: 'Save' }).click({ force: true });")
    assert weakened(BASE, after) == []


def test_an_empty_reason_does_not_unlock_force():
    after = BASE.replace(
        "  await page.getByRole('button', { name: 'Save' }).click();",
        "  // qa-allow-force:\n"
        "  await page.getByRole('button', { name: 'Save' }).click({ force: true });")
    assert any("force: true" in r for r in weakened(BASE, after))
