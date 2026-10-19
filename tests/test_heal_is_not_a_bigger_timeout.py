"""A patch that only widens timeouts is refused before it reaches disk.

THE DEFECT, observed end to end on VENDOR-TC-001. The spec asserted
`getByText('01/01/1990')` on the Review screen. The value was on screen the
whole time — inside an `<input>`, where `getByText` can never match it,
because it reads text nodes and not input values.

The Reviewer, which is handed no DOM, guessed: "the Review screen almost
certainly renders the date in a different format". The Self-Healer acted on
that reason and did the only thing available to it with no diagnosis:

  attempt 1  timeout 20000 -> 30000, plus waitForLoadState('networkidle')
  attempt 2  timeout 30000 -> 45000

Both were applied. Both re-ran the full suite. Both failed identically. Two
heals and two executions bought exactly nothing, and MAX_HEAL_ATTEMPTS was
gone. A timeout is a symptom; no timeout can make `getByText` match an input.

So this is enforced deterministically rather than asked for in the skill —
this is the failure mode the agent falls into precisely WHEN a prompt rule is
least likely to hold, i.e. when it has no idea what is wrong.

Run with:  python -m pytest tests/test_heal_is_not_a_bigger_timeout.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents.self_healer import _is_timeout_only_change  # noqa: E402

_BEFORE = """
await test.step('Step 13 — verify Review', async () => {
  await expect(page.getByText('01/01/1990')).toBeVisible({ timeout: 20000 });
});
""".strip()


def _with_extra_line(source: str, line: str) -> str:
    """Insert a statement as the step body's last line.

    Not a `.replace("});", ...)`: that substring also occurs inside
    `{ timeout: 20000 });`, so a naive replace corrupts the assertion and the
    test stops testing what it claims to.
    """
    lines = source.splitlines()
    return "\n".join(lines[:-1] + [f"  {line}"] + lines[-1:])


def test_raising_a_timeout_is_refused():
    after = _BEFORE.replace("timeout: 20000", "timeout: 30000")
    assert _is_timeout_only_change(_BEFORE, after) is True


def test_raising_a_timeout_and_adding_a_wait_is_still_refused():
    """Attempt 1's exact shape: a bigger number plus a networkidle wait."""
    after = _with_extra_line(_BEFORE.replace("timeout: 20000", "timeout: 30000"),
                             "await page.waitForLoadState('networkidle');")
    assert _is_timeout_only_change(_BEFORE, after) is True


def test_adding_waitfortimeout_alone_is_refused():
    after = _with_extra_line(_BEFORE, "await page.waitForTimeout(3000);")
    assert _is_timeout_only_change(_BEFORE, after) is True


def test_the_real_fix_is_allowed():
    """Asserting the input's value — what should have happened."""
    after = _BEFORE.replace(
        "expect(page.getByText('01/01/1990')).toBeVisible({ timeout: 20000 })",
        "expect(page.locator('#basicInfoDOB')).toHaveValue('01/01/1990')")
    assert _is_timeout_only_change(_BEFORE, after) is False


def test_adding_a_blur_is_allowed():
    """Pattern 6 — the app commits on `change`, so the field needs a blur."""
    after = _with_extra_line(_BEFORE, "await page.locator('#basicInfoDOB').blur();")
    assert _is_timeout_only_change(_BEFORE, after) is False


def test_changing_a_locator_is_allowed():
    after = _BEFORE.replace("getByText('01/01/1990')", "getByTestId('dob-review')")
    assert _is_timeout_only_change(_BEFORE, after) is False


def test_an_identical_patch_is_not_reported_as_timeout_only():
    """No change at all is a different complaint, handled elsewhere — this
    check must not swallow it."""
    assert _is_timeout_only_change(_BEFORE, _BEFORE) is False


def test_empty_content_is_not_reported_as_timeout_only():
    assert _is_timeout_only_change("", _BEFORE) is False
    assert _is_timeout_only_change(_BEFORE, "") is False


def test_a_timeout_raise_bundled_with_a_real_change_is_allowed():
    """Raising a timeout is only refused when it is the WHOLE patch. A
    genuine fix that also adjusts a wait must still get through."""
    after = _BEFORE.replace(
        "timeout: 20000", "timeout: 30000").replace(
        "getByText('01/01/1990')", "locator('#basicInfoDOB')")
    assert _is_timeout_only_change(_BEFORE, after) is False
