"""The Coding Agent's skill forbids these; the gate must too.

The failure this prevents: a skill is only a request. A generated suite shipped
with `page.waitForTimeout(800)`, `expect(Number(await input.inputValue()))` (a
one-shot snapshot that never retries), `click({ force: true })` on an element that
was meant to be disabled, and a spec with no tag - and every gate waved it through.
"""

from qa_pipeline.verifiers import spec_hygiene

GOOD = """
import { test, expect } from '@fixtures/index';
test('PROJ-1-TC-001 saves', { tag: ['@regression'] }, async ({ page }) => {
  await expect(page.getByTestId('status')).toHaveText('Saved');
});
"""


def _check(source):
    return spec_hygiene.check({"path": "tests/e2e/a.spec.ts", "kind": "spec",
                               "content": source, "test_case_ids": ["PROJ-1-TC-001"]})


def _errors(source):
    return " ".join(_check(source).errors)


def test_a_clean_spec_passes():
    assert _check(GOOD).ok


def test_a_hard_wait_is_rejected():
    assert "hard wait" in _errors(GOOD.replace("  await expect", "  await page.waitForTimeout(800);\n  await expect"))


def test_a_hard_wait_in_a_comment_is_not_a_hard_wait():
    assert _check(GOOD.replace("  await expect", "  // never use page.waitForTimeout(800) here\n  await expect")).ok


def test_asserting_on_a_value_read_first_is_rejected():
    snap = GOOD.replace("await expect(page.getByTestId('status')).toHaveText('Saved');",
                        "expect(Number(await page.getByTestId('n').inputValue())).toBeGreaterThan(1);")
    assert "never retries" in _errors(snap)


def test_expect_await_is_rejected():
    snap = GOOD.replace("await expect(page.getByTestId('status')).toHaveText('Saved');",
                        "expect(await page.getByTestId('n').textContent()).toBe('x');")
    assert "never retries" in _errors(snap)


def test_force_true_needs_a_written_reason():
    forced = GOOD.replace("  await expect", "  await page.getByRole('button').click({ force: true });\n  await expect")
    assert "force: true" in _errors(forced)


def test_force_true_with_a_reason_is_accepted():
    forced = GOOD.replace(
        "  await expect",
        "  // qa-allow-force: sticky header covers its own button\n"
        "  await page.getByRole('button').click({ force: true });\n  await expect")
    assert _check(forced).ok


def test_a_spec_with_tests_and_no_tag_is_rejected():
    assert "no `tag`" in _errors(GOOD.replace(" { tag: ['@regression'] },", ""))


def test_a_tag_on_the_describe_covers_its_tests():
    spec = """
import { test, expect } from '@fixtures/index';
test.describe('Zoom', { tag: ['@regression'] }, () => {
  test('PROJ-1-TC-001 zooms', async ({ page }) => {
    await expect(page.getByTestId('z')).toBeVisible();
  });
});
"""
    assert _check(spec).ok
