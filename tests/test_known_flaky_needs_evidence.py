"""A "known flaky" label must be backed by a test that actually flaked.

The failure this prevents: the Reviewer model calls a real regression "flaky",
the label is saved, and from then on `_prematch_known_flaky` auto-labels every
future failure of that case without asking the model again - so one wrong guess
hides the bug permanently.
"""

from types import SimpleNamespace

from qa_pipeline.orchestrator import stages


def _ctx(tmp_path, cases):
    return SimpleNamespace(
        state={"executor": {"cases": cases}},
        artifact=lambda name: stages.artifact_path(tmp_path, name))


def test_a_flaky_label_with_no_retry_pass_is_not_recorded(tmp_path):
    known = []
    ctx = _ctx(tmp_path, [{"test_case_id": "A-1", "status": "failed", "flaky": False}])

    stages._merge_known_flaky(
        ctx, [{"test_case_id": "A-1", "verdict": "known_flaky", "reason": "x"}], known)

    assert known == []
    assert not (tmp_path / "_internal" / "known_flaky.json").exists()


def test_a_case_that_failed_then_passed_is_recorded(tmp_path):
    known = []
    ctx = _ctx(tmp_path, [{"test_case_id": "A-1", "status": "passed", "flaky": True}])

    stages._merge_known_flaky(
        ctx, [{"test_case_id": "A-1", "verdict": "known_flaky", "reason": "x"}], known)

    assert [e["test_case_id"] for e in known] == ["A-1"]
