"""A teardown hook must be somewhere Playwright will actually register it.

THE DEFECT, caught by running the generated suite for real. The Coding Agent
was told to add `test.afterEach` cleanup, and did — INSIDE the `test()` body:

    test('VENDOR-TC-001 ...', async ({ page }) => {
      let identityDeleted = false;
      test.afterEach(async ({ page: afterPage }) => { ... });   // <-- here
      await test.step('Step 1 ...', ...);

Playwright registers hooks while COLLECTING a file. By the time a test body
runs, collection is over, so it refuses:

    Error: Playwright Test did not expect test.afterEach() to be called here.

Every gate passed it. It type-checks — `test.afterEach` is a real function
with the right signature wherever you call it — so `compile_check` was happy,
and `_missing_teardown` only asks whether the file CONTAINS a teardown hook,
which it does. The spec failed on the first line the Executor reached, having
exercised nothing.

Run with:  python -m pytest tests/test_hook_placement.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import spec_hygiene  # noqa: E402


def _spec(content):
    return {"path": "tests/e2e/vendor-registration.spec.ts", "kind": "spec",
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


_NESTED = """
import { test, expect } from '@fixtures/index';
const surname = `S${Date.now()}`;

test(
  'VENDOR-TC-001 successful registration via R100 (No Match Found)',
  { tag: ['@newfeature'] },
  async ({ page }) => {
    let identityDeleted = false;
    test.afterEach(async ({ page: afterPage }) => {
      if (identityDeleted) return;
      await afterPage.goto('/');
    });
    await expect(page.getByText('Dashboard')).toBeVisible();
  },
);
"""

_TOP_LEVEL = """
import { test, expect } from '@fixtures/index';
const surname = `S${Date.now()}`;
let identityDeleted = false;

test.afterEach(async ({ page }) => {
  if (identityDeleted) return;
  await page.goto('/');
});

test(
  'VENDOR-TC-001 successful registration via R100 (No Match Found)',
  { tag: ['@newfeature'] },
  async ({ page }) => {
    await expect(page.getByText('Dashboard')).toBeVisible();
  },
);
"""

_IN_DESCRIBE = """
import { test, expect } from '@fixtures/index';
const surname = `S${Date.now()}`;

test.describe('Vendor registration', () => {
  let identityDeleted = false;

  test.afterEach(async ({ page }) => {
    if (identityDeleted) return;
    await page.goto('/');
  });

  test('VENDOR-TC-001 registration (No Match Found)', { tag: ['@newfeature'] }, async ({ page }) => {
    await expect(page.getByText('Dashboard')).toBeVisible();
  });
});
"""

_ASYNC_DESCRIBE = _IN_DESCRIBE.replace(
    "test.describe('Vendor registration', () => {",
    "test.describe('Vendor registration', async () => {")


def test_a_hook_inside_a_test_body_fails_the_gate():
    result = spec_hygiene.check(_spec(_NESTED))
    assert result.ok is False
    assert any("INSIDE a `test()` body" in e for e in result.errors), result.errors
    assert any("Move `test.afterEach(...)` OUT" in r for r in result.repairs)


def test_a_top_level_hook_passes():
    assert spec_hygiene.check(_spec(_TOP_LEVEL)).ok is True


def test_a_hook_in_a_sync_describe_passes():
    assert spec_hygiene.check(_spec(_IN_DESCRIBE)).ok is True


def test_an_async_describe_holding_hooks_fails():
    """Playwright names this case in the same error message: only SYNC
    describe callbacks are supported."""
    result = spec_hygiene.check(_spec(_ASYNC_DESCRIBE))
    assert result.ok is False
    assert any("only SYNC describe" in e or "SYNC describe" in e
               for e in result.errors), result.errors


def test_a_bracket_in_a_test_title_does_not_end_the_call_early():
    """The real title is '... biometric capture (No Match Found)'. A paren
    counter that cannot tell code from string closes the call inside the
    title and reads the hook as top-level, missing the defect entirely."""
    assert "(No Match Found)" in _NESTED
    assert spec_hygiene.check(_spec(_NESTED)).ok is False


def test_a_hook_after_a_test_is_still_top_level():
    """Declaration order does not decide nesting — a hook written after the
    test, at file scope, is legal."""
    source = _TOP_LEVEL.replace("test.afterEach", "test.afterAll")
    assert spec_hygiene.check(_spec(source)).ok is True
