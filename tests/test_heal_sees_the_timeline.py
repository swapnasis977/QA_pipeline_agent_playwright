"""The Self-Healer must see how far the test got before it failed.

`agents/self_healer.py`'s own docstring has always claimed:

    IN:  Cases marked "fixable_test_issue" by Reviewer, failure trace/screenshot

No trace ever reached it. It received the broken file, the failure text and one
DOM snapshot — nothing about the RUN. So it could not tell "failed on the very
first assertion" from "passed twelve steps and failed on the thirteenth", and
those demand opposite diagnoses.

That gap produced a confidently wrong diagnosis on a real run. The test failed
at step 1 of 20, and the Reviewer and Healer concluded:

    "This positional CSS selector does not resolve on the dashboard ... the
     refactor to VendorRegistrationPage introduced a regression in the
     selector."

There was no refactor regression. The session was not authenticated, so the
page was never the dashboard and no locator could have matched. "Step 1 of 20
failed, 13.6s" is the signal that points at page state rather than at a
selector — and it was sitting unread in run_results.json.

Playwright's trace is a zip, but its most useful content — the action timeline
— is already in the JSON report as plain text.

Run with:  python -m pytest tests/test_heal_sees_the_timeline.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from utils import failure_dom  # noqa: E402


def _report(*steps, status="failed"):
    return {"suites": [{"file": "e2e/x.spec.ts", "specs": [
        {"title": "VENDOR-TC-001 register", "file": "e2e/x.spec.ts", "tests": [
            {"results": [{"status": status, "duration": 23000,
                          "steps": [{"title": t, "duration": d} for t, d in steps],
                          "error": {"message": "element(s) not found"}}]}]}]}]}


def test_the_timeline_names_the_steps_that_ran():
    tl = failure_dom.extract_timeline(
        _report(("Step 1 — Log in", 13649), ("Step 2 — Click tile", 300)),
        {"VENDOR-TC-001"})

    entry = tl["VENDOR-TC-001"]
    assert entry["steps_completed"] == 2
    assert "Step 1" in entry["last_step"] or "Step 2" in entry["last_step"]


def test_failing_on_the_first_step_is_distinguishable():
    """The signal that would have redirected the whole diagnosis."""
    tl = failure_dom.extract_timeline(
        _report(("Step 1 — Log in and verify the Dashboard", 13649)),
        {"VENDOR-TC-001"})

    entry = tl["VENDOR-TC-001"]
    assert entry["steps_completed"] == 1
    assert entry["failed_early"] is True


def test_failing_deep_into_the_flow_is_not_early():
    steps = [(f"Step {n} — thing", 100) for n in range(1, 14)]
    tl = failure_dom.extract_timeline(_report(*steps), {"VENDOR-TC-001"})

    entry = tl["VENDOR-TC-001"]
    assert entry["steps_completed"] == 13
    assert entry["failed_early"] is False


def test_only_the_requested_cases_are_returned():
    tl = failure_dom.extract_timeline(
        _report(("Step 1 — x", 10)), {"SOME-OTHER-TC-002"})
    assert tl == {}


def test_a_passing_case_is_not_reported():
    tl = failure_dom.extract_timeline(
        _report(("Step 1 — x", 10), status="passed"), {"VENDOR-TC-001"})
    assert tl == {}


def test_an_empty_report_is_safe():
    assert failure_dom.extract_timeline({}, {"VENDOR-TC-001"}) == {}
    assert failure_dom.extract_timeline(None, set()) == {}


def test_the_healer_accepts_it():
    import inspect

    from agents import self_healer
    assert "action_timeline" in inspect.signature(self_healer.run).parameters
