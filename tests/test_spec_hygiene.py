"""Defects the ledger cannot catch, because they are about LOGIC, not locators.

`gate_code` verifies every locator against the ledger and a live snapshot. It
passed a spec that could never succeed, because none of its three worst
problems were about a locator being wrong.

From the real VENDOR-TC-001 spec:

1. `exact: true` DROPPED.
       ledger:    page.getByText('MATCH FOUND', { exact: true })
       generated: page.getByText('MATCH FOUND')
   `getByText` defaults to SUBSTRING matching, and "NO MATCH FOUND" contains
   "MATCH FOUND". So on the SUCCESS path the app shows NO MATCH FOUND, the
   check matched it, and the test called `test.skip()` on itself. It could
   essentially never reach registration. One dropped option inverted the test.

2. TWO NAMES, ONE LOCATOR.
       const noMatch    = page.getByText('NO MATCH FOUND', { exact: true });
       const matchFound = page.getByText('NO MATCH FOUND', { exact: true });
       const settled    = noMatch.or(matchFound).first();
   `matchFound` is a copy-paste of `noMatch`, so the `.or()` is a no-op and
   the retry loop waits only for one outcome — burning 4x90s when the other
   one occurs.

3. CONDITIONAL TEST LOGIC. `if`/`.catch(() => false)` branching, which
   `instructions/conventions.md` forbids outright. Reported as a warning, not
   an error: this project's own test cases legitimately say things like "if a
   stale confirmation dialog appears, click Yes", so a blanket rejection would
   fight the requirement. Visible, not blocking.

Run with:  python -m pytest tests/test_spec_hygiene.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.verifiers import spec_hygiene  # noqa: E402


def _spec(content, kind="spec"):
    return {"path": "tests/e2e/vendor-registration.spec.ts", "kind": kind,
            "test_case_ids": ["VENDOR-TC-001"], "content": content}


def _plan(*locators):
    return [{"test_case_id": "VENDOR-TC-001",
             "elements": [{"logical_name": f"e{i}", "screen": "Capture",
                           "locator": loc} for i, loc in enumerate(locators)]}]


# --- 1. a dropped `exact: true` inverts the test -------------------------

def test_dropping_exact_true_is_rejected():
    """The real shape: BOTH outcomes are grounded, and one text is a
    substring of the other. That collision is what makes the missing option
    fatal rather than cosmetic."""
    plans = _plan("page.getByText('MATCH FOUND', { exact: true })",
                  "page.getByText('NO MATCH FOUND', { exact: true })")
    result = spec_hygiene.check(
        _spec("const m = page.getByText('MATCH FOUND');"), plans)

    assert result.ok is False
    assert any("exact" in e for e in result.errors)
    assert any("MATCH FOUND" in r for r in result.repairs)


def test_keeping_exact_true_passes():
    plans = _plan("page.getByText('MATCH FOUND', { exact: true })")
    result = spec_hygiene.check(
        _spec("const m = page.getByText('MATCH FOUND', { exact: true });"), plans)
    assert result.ok is True


def test_a_ledger_locator_without_exact_is_not_policed():
    plans = _plan("page.getByText('Register')")
    result = spec_hygiene.check(_spec("const e = page.getByText('Register');"), plans)
    assert result.ok is True


def test_text_that_is_not_a_substring_risk_is_left_alone():
    """The rule exists because one string contains another. Where no such
    collision is possible the missing option is harmless, and flagging it
    would just train people to ignore the gate."""
    plans = _plan("page.getByText('Totally Unique Banner', { exact: true })")
    result = spec_hygiene.check(
        _spec("const b = page.getByText('Totally Unique Banner');"), plans)
    assert result.ok is True


# --- 2. two names bound to the same locator ------------------------------

def test_two_consts_with_an_identical_locator_are_rejected():
    code = ("const noMatch = page.getByText('NO MATCH FOUND', { exact: true });\n"
            "const matchFound = page.getByText('NO MATCH FOUND', { exact: true });\n"
            "const settled = noMatch.or(matchFound).first();\n")
    result = spec_hygiene.check(_spec(code), _plan())

    assert result.ok is False
    joined = " ".join(result.errors)
    assert "noMatch" in joined and "matchFound" in joined


def test_distinct_locators_pass():
    code = ("const noMatch = page.getByText('NO MATCH FOUND', { exact: true });\n"
            "const matchFound = page.getByText('MATCH FOUND', { exact: true });\n")
    assert spec_hygiene.check(_spec(code), _plan()).ok is True


def test_the_same_name_reassigned_is_not_a_duplicate():
    code = "const a = page.locator('#x');\nconst b = a.first();\n"
    assert spec_hygiene.check(_spec(code), _plan()).ok is True


# --- 3. conditional logic is a warning, not a rejection ------------------

def test_conditional_logic_warns_without_failing():
    code = ("const seen = await d.waitFor().then(() => true).catch(() => false);\n"
            "if (seen) { await d.click(); }\n")
    result = spec_hygiene.check(_spec(code), _plan())

    assert result.ok is True, "must not block — some cases genuinely require it"
    assert result.warnings
    assert any("conditional" in w.lower() for w in result.warnings)


def test_straight_line_code_warns_about_nothing():
    result = spec_hygiene.check(_spec("await page.locator('#x').click();"), _plan())
    assert result.ok is True
    assert not result.warnings


# --- scope ----------------------------------------------------------------

def test_only_specs_are_checked():
    """A page object legitimately holds many locators and no test logic."""
    code = ("const noMatch = page.getByText('X', { exact: true });\n"
            "const same = page.getByText('X', { exact: true });\n")
    assert spec_hygiene.check(_spec(code, kind="page"), _plan()).ok is True


def test_a_collision_the_model_introduced_itself_is_caught():
    """The defect that actually shipped. The plan grounded only
    "NO MATCH FOUND"; the model wrote `getByText('MATCH FOUND')` on its own,
    at the one line no plan could vouch for. Requiring the plan's blessing
    was what let it through — the two texts sitting in the same file, one
    containing the other, is the whole proof needed."""
    plans = _plan("page.getByText('NO MATCH FOUND', { exact: true })")
    code = ("const noMatch = page.getByText('NO MATCH FOUND', { exact: true });\n"
            "const isMatchFound = await page.getByText('MATCH FOUND').isVisible();\n")

    result = spec_hygiene.check(_spec(code), plans)

    assert result.ok is False
    assert any("MATCH FOUND" in e and "exact" in e for e in result.errors)
