"""The Self-Healer must spend its effort where a patch can help.

THE FAILURES (observed on a live run):
  * Cases whose route stalled before the test began were sent to the healer. Its
    own diagnosis said "the image editing tool was never reached"; a patch to the
    spec cannot fix an application stall, and every attempt re-ran a test that
    waited out its full timeout.
  * The healer edited GENERATED files: it rewrote grounded locators in a page
    object and pointed the route helper's upload at a fixture that did not exist.
    Those files are rebuilt on every run and a hand edit bypasses the ledger.

Run with:  python -m pytest tests/test_healer_scope.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import pom_builder, self_healer  # noqa: E402
from qa_pipeline.orchestrator.stages import _stalled_in_navigation  # noqa: E402


def _report(*tests):
    specs = []
    for title, status, retries, message in tests:
        annotations = [{"type": "route-retry", "description": f"attempt {i + 1}"}
                       for i in range(retries)]
        specs.append({"title": title, "tests": [{"results": [{
            "status": status, "annotations": annotations,
            "error": {"message": message} if message else {}}]}]})
    return {"suites": [{"specs": specs}]}


def test_a_case_whose_route_failed_every_attempt_is_not_a_test_defect():
    report = _report(("PROJ-1-TC-1 slider", "timedOut", 3, "Test timeout of 120000ms exceeded."))
    assert "PROJ-1-TC-1" in _stalled_in_navigation(report)


def test_a_route_step_that_was_never_found_counts_as_a_stall():
    report = _report(("PROJ-1-TC-2 tile", "failed", 0, "Error: Route step not found on the page: X"))
    assert "PROJ-1-TC-2" in _stalled_in_navigation(report)


def test_a_case_that_reached_its_screen_and_failed_an_assertion_is_healable():
    report = _report(("PROJ-1-TC-3 value", "failed", 0, "expect(locator).toHaveValue(expected) failed"),
                     ("PROJ-1-TC-4 recovered", "failed", 1, "expect(locator).toBeVisible() failed"))
    assert _stalled_in_navigation(report) == {}


def test_a_passing_case_is_never_a_stall():
    assert _stalled_in_navigation(_report(("PROJ-1-TC-5 ok", "passed", 3, ""))) == {}


def test_every_generated_file_carries_the_marker_the_healer_checks():
    page = pom_builder.build_page_objects([{"elements": [
        {"screen": "S", "logical_name": "Save", "locator": 'page.locator("#a")'}]}])[0]["content"]
    routes = pom_builder.build_route_helper({"r": {"url": "", "clicks": ["x"]}})["content"]
    assert self_healer.GENERATED_MARKER in page[:600]
    assert self_healer.GENERATED_MARKER in routes[:600]


def test_retry_annotations_on_the_test_object_are_counted_too():
    report = {"suites": [{"specs": [{"title": "PROJ-1-TC-9 slider", "tests": [{
        "annotations": [{"type": "route-retry", "description": f"attempt {i}"} for i in (1, 2, 3)],
        "results": [{"status": "timedOut", "error": {"message": "Test timeout"}}]}]}]}]}
    assert "PROJ-1-TC-9" in _stalled_in_navigation(report)


# --- environment failures are recognised by their message --------------------

from qa_pipeline.orchestrator.stages import environment_failure_reason  # noqa: E402


def test_a_dns_failure_is_the_environments_not_the_tests():
    assert environment_failure_reason("Error: page.goto: net::ERR_NAME_NOT_RESOLVED at https://x")


def test_a_page_that_never_loaded_is_the_environments():
    assert environment_failure_reason("TimeoutError: page.goto: Timeout 30000ms exceeded.")


def test_a_refused_sign_in_is_the_environments():
    assert environment_failure_reason("Error: Sign-in ended on https://idp/auth, which is not the application")
    assert environment_failure_reason("Internal Server Error")


def test_a_real_assertion_failure_is_never_called_environmental():
    assert environment_failure_reason("Error: expect(locator).toHaveValue(expected) failed") == ""
    assert environment_failure_reason("") == ""


def test_three_failed_route_attempts_count_even_with_a_bland_message():
    assert environment_failure_reason("Test timeout of 120000ms exceeded.", route_retries=3)
    assert environment_failure_reason("Test timeout of 120000ms exceeded.", route_retries=1) == ""
