"""Two defects a spec can carry while passing every other gate.

1. CLEANUP THAT ONLY RUNS ON SUCCESS. VENDOR-TC-001 registers a real
   identity at step 9 and deletes it at steps 16-20 — in the MAIN BODY. Fail
   anywhere between, and the identity is never removed. On this project that
   is not a tidiness problem: the R100 biometric mock is single-shot, so a
   leaked registration makes the NEXT run return MATCH FOUND, and the test skips
   itself. One mid-test failure silently disables the test for every run
   after it. `coding_agent.skill.md` already says teardown belongs in
   `test.afterEach`; nothing enforced it.

2. DATA TYPED BUT NEVER CHECKED. The same spec fills Surname, First Name,
   Middle Name and DOB, then asserts only that a Submit button is visible.
   The case's own step 13 says "Verify the entered Surname, First Name, and
   Date of Birth are shown on the Review screen". A test that enters values
   and never reads them back cannot fail for the reason it exists.

Reported differently on purpose. (1) is decidable — a hook is present or it
is not — and is an error. (2) is a WARNING: some filled values legitimately
go unasserted (a deletion comment, a password), so blocking on it would
manufacture false failures. It names the variables and lets a human judge.

Run with:  python -m pytest tests/test_cleanup_and_assertion_coverage.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import spec_hygiene  # noqa: E402


def _spec(content, kind="spec"):
    return {"path": "tests/e2e/x.spec.ts", "kind": kind,
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


_CREATES = """
const ts = Date.now();
const surname = `Vendor${ts}`;
await page.locator('#basicInfoLastname').fill(surname);
await page.locator('#btnSubmit').click();
"""


# --- 1. cleanup must survive a mid-test failure ---------------------------

def test_data_creation_without_a_teardown_hook_is_rejected():
    result = spec_hygiene.check(_spec(_CREATES), [])
    assert result.ok is False
    assert any("afterEach" in e or "teardown" in e.lower() for e in result.errors)


def test_an_after_each_hook_satisfies_it():
    code = "test.afterEach(async ({ page }) => { await cleanup(page); });\n" + _CREATES
    assert spec_hygiene.check(_spec(code), []).ok is True


def test_an_after_all_hook_also_satisfies_it():
    code = "test.afterAll(async () => { await cleanup(); });\n" + _CREATES
    assert spec_hygiene.check(_spec(code), []).ok is True


def test_a_spec_that_creates_nothing_needs_no_teardown():
    """A read-only test has nothing to clean up; demanding a hook would be
    noise on the majority of specs."""
    code = "await expect(page.locator('#total')).toHaveText('42');\n"
    assert spec_hygiene.check(_spec(code), []).ok is True


def test_deleting_inside_the_body_does_not_count_as_teardown():
    """The exact shape of the defect: cleanup steps live in the test body, so
    they are skipped the moment an earlier step fails."""
    code = _CREATES + "await test.step('Step 19 — Delete Identity', async () => {});\n"
    assert spec_hygiene.check(_spec(code), []).ok is False


# --- 2. values typed but never read back ---------------------------------

def test_a_filled_value_that_is_never_asserted_warns():
    code = ("test.afterEach(async () => {});\n"
            "const ts = Date.now();\n"
            "const surname = `S${ts}`;\n"
            "const firstName = `F${ts}`;\n"
            "await page.locator('#a').fill(surname);\n"
            "await page.locator('#b').fill(firstName);\n"
            "await expect(page.getByText(surname)).toBeVisible();\n")
    result = spec_hygiene.check(_spec(code), [])

    assert result.ok is True, "a judgement call must not block the run"
    joined = " ".join(result.warnings)
    assert "firstName" in joined
    assert "surname" not in joined, "surname IS asserted — do not report it"


def test_everything_asserted_warns_about_nothing():
    code = ("test.afterEach(async () => {});\n"
            "const surname = `S${Date.now()}`;\n"
            "await page.locator('#a').fill(surname);\n"
            "await expect(page.getByText(surname)).toBeVisible();\n")
    result = spec_hygiene.check(_spec(code), [])
    assert not [w for w in result.warnings if "never asserted" in w]


def test_a_literal_fill_is_not_policed():
    """`pressSequentially('some comment')` has no variable to track, and a
    deletion comment is not something a test should assert on."""
    code = ("test.afterEach(async () => {});\n"
            "const surname = `S${Date.now()}`;\n"
            "await page.locator('#a').fill(surname);\n"
            "await expect(page.getByText(surname)).toBeVisible();\n"
            "await page.locator('#c').pressSequentially('a cleanup note');\n")
    result = spec_hygiene.check(_spec(code), [])
    assert not [w for w in result.warnings if "never asserted" in w]


def test_page_objects_are_exempt_from_both():
    code = _CREATES
    assert spec_hygiene.check(_spec(code, kind="page"), []).ok is True
