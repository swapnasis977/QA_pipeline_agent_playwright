"""A run that leaves tests failing must not exit 0.

The failure this prevents: 20 red tests, exit code 0, a green CI job. The
healer's own `still_failing` list was no help - it only covers cases the healer
attempted, so a genuine bug, a known flake or an environment failure (all red)
were never counted.
"""

from types import SimpleNamespace

from qa_pipeline.cli import EXIT_TESTS_FAILING, _result_exit_code
from qa_pipeline.orchestrator.stages import unresolved_failures


def _ctx(cases, healed=()):
    return SimpleNamespace(state={
        "executor": {"ran": True, "cases": cases},
        "self_healer": {"healed": list(healed)},
    })


def _args(**kw):
    base = {"allow_failures": False, "until": None, "only": None}
    return SimpleNamespace(**{**base, **kw})


def _profile(fail_on=True):
    return SimpleNamespace(get=lambda key, default=None:
                           fail_on if key == "gates.fail_on_test_failures" else default)


def test_a_genuine_bug_the_healer_never_touched_still_counts_as_failing():
    ctx = _ctx([{"test_case_id": "A-1", "status": "failed", "file": "a.spec.ts"}])
    assert unresolved_failures(ctx) == ["A-1"]


def test_a_healed_case_no_longer_counts():
    ctx = _ctx([{"test_case_id": "A-1", "status": "failed"},
                {"test_case_id": "A-2", "status": "failed"}], healed=["A-1"])
    assert unresolved_failures(ctx) == ["A-2"]


def test_passed_and_skipped_cases_never_count():
    ctx = _ctx([{"test_case_id": "A-1", "status": "passed"},
                {"test_case_id": "A-2", "status": "skipped"}])
    assert unresolved_failures(ctx) == []


def test_a_failing_row_with_no_id_in_its_title_is_still_counted():
    ctx = _ctx([{"test_case_id": "", "status": "failed", "file": "tests/e2e/x.spec.ts"}])
    assert unresolved_failures(ctx) == ["tests/e2e/x.spec.ts"]


def test_a_run_that_never_executed_has_no_failures_to_report():
    assert unresolved_failures(SimpleNamespace(state={"executor": {"ran": False}})) == []


def test_failing_tests_exit_6():
    ctx = _ctx([{"test_case_id": "A-1", "status": "failed"}])
    assert _result_exit_code(ctx, _args(), _profile()) == EXIT_TESTS_FAILING == 6


def test_a_green_suite_exits_0():
    ctx = _ctx([{"test_case_id": "A-1", "status": "passed"}])
    assert _result_exit_code(ctx, _args(), _profile()) == 0


def test_the_operator_can_opt_out_by_flag_or_profile():
    ctx = _ctx([{"test_case_id": "A-1", "status": "failed"}])
    assert _result_exit_code(ctx, _args(allow_failures=True), _profile()) == 0
    assert _result_exit_code(ctx, _args(), _profile(fail_on=False)) == 0


def test_a_deliberately_partial_run_is_not_judged():
    ctx = _ctx([{"test_case_id": "A-1", "status": "failed"}])
    assert _result_exit_code(ctx, _args(until="coding_agent"), _profile()) == 0
