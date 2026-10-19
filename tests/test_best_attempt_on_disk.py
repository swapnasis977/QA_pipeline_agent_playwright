"""The best generation attempt must be the one left on disk.

THE FAILURE. `repair_loop.run` returns the BEST attempt, but every attempt
writes its files as it goes. When attempt 3 regressed, `generated_files.json`
described attempt 2's code while the workspace held attempt 3's, so the
Executor ran code the run's own record disowned.

A second flaw sat in the ranking itself: attempts were ordered by error count
alone, so a generation that produced NO spec (one error: "no spec was
produced") beat a real attempt with three locator errors, and the empty list
was returned as "best".

Run with:  python -m pytest tests/test_best_attempt_on_disk.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline import verifiers  # noqa: E402
from qa_pipeline.orchestrator.stages import (  # noqa: E402
    _restore_best_attempt,
    empty_generation_result,
)
from qa_pipeline.verifiers import repair_loop  # noqa: E402


def _failing(*errors):
    return verifiers.VerifyResult.failed("gate_code", list(errors))


def test_a_real_attempt_with_errors_beats_a_generation_that_produced_nothing():
    empty = verifiers.combine("gate_code", [empty_generation_result(["TC-1"], [])])
    real = verifiers.combine("gate_code", [_failing("a", "b", "c")])
    assert repair_loop._score(real) < repair_loop._score(empty)


def test_passing_still_beats_failing_and_fewer_errors_still_win():
    ok = verifiers.combine("gate_code", [verifiers.VerifyResult.passed("gate_code")])
    two = verifiers.combine("gate_code", [_failing("a", "b")])
    three = verifiers.combine("gate_code", [_failing("a", "b", "c")])
    assert repair_loop._score(ok) < repair_loop._score(two) < repair_loop._score(three)


def test_the_loop_returns_the_real_attempt_not_the_empty_last_one():
    attempts = iter([[{"path": "tests/a.spec.ts", "kind": "spec", "content": "x" * 40}], []])

    def produce(_repair):
        return next(attempts)

    def verify(files):
        if not files:
            return [empty_generation_result(["TC-1"], [])]
        return [_failing("a", "b", "c")]

    outcome = repair_loop.run(produce, verify, max_repairs=1, verbose=False)
    assert outcome.artifact and outcome.artifact[0]["path"] == "tests/a.spec.ts"


def test_restoring_rewrites_the_best_attempts_files_over_a_regression(tmp_path):
    spec = tmp_path / "tests" / "a.spec.ts"
    spec.parent.mkdir(parents=True)
    spec.write_text("// the REGRESSED last attempt " + "z" * 20, encoding="utf-8")
    best = [{"path": "tests/a.spec.ts", "kind": "spec", "content": "// the best attempt " + "b" * 20}]
    ctx = SimpleNamespace(repo_dir=tmp_path)

    assert _restore_best_attempt(ctx, best, last_produced=[{"path": "tests/a.spec.ts"}]) == 1
    assert "best attempt" in spec.read_text(encoding="utf-8")


def test_nothing_is_rewritten_when_the_last_attempt_was_the_best(tmp_path):
    best = [{"path": "tests/a.spec.ts", "kind": "spec", "content": "// ok " + "b" * 40}]
    ctx = SimpleNamespace(repo_dir=tmp_path)
    assert _restore_best_attempt(ctx, best, last_produced=best) == 0
    assert not (tmp_path / "tests" / "a.spec.ts").exists()


def test_restoring_never_writes_outside_the_repo(tmp_path):
    best = [{"path": "../escape.ts", "kind": "spec", "content": "// nope " + "b" * 40}]
    ctx = SimpleNamespace(repo_dir=tmp_path / "repo")
    (tmp_path / "repo").mkdir()
    assert _restore_best_attempt(ctx, best, last_produced=[]) == 0
    assert not (tmp_path / "escape.ts").exists()


# --- targeted repair --------------------------------------------------------

from qa_pipeline import verifiers as _verifiers  # noqa: E402
from qa_pipeline.orchestrator.stages import _failing_scope  # noqa: E402


def _spec(path, *ids):
    return {"path": path, "kind": "spec", "test_case_ids": list(ids), "content": "x" * 40}


_OK = _verifiers.VerifyResult.passed("gate_code")
_BAD = _verifiers.VerifyResult.failed("gate_code", ["does not compile"])


def test_only_the_rejected_specs_and_their_cases_are_repaired():
    files = [_spec("tests/a.spec.ts", "TC-1"), _spec("tests/b.spec.ts", "TC-2", "TC-3"),
             _spec("tests/c.spec.ts", "TC-4")]
    failing, ids = _failing_scope(files, [_OK, _BAD, _OK])
    assert [f["path"] for f in failing] == ["tests/b.spec.ts"]
    assert ids == ["TC-2", "TC-3"]


def test_a_clean_batch_has_nothing_to_repair():
    files = [_spec("tests/a.spec.ts", "TC-1")]
    assert _failing_scope(files, [_OK]) is None


def test_a_failing_helper_file_falls_back_to_regenerating_everything():
    helper = {"path": "src/pages/X.ts", "kind": "page", "test_case_ids": []}
    assert _failing_scope([_spec("tests/a.spec.ts", "TC-1"), helper], [_OK, _BAD]) is None


def test_a_failing_spec_with_no_case_ids_cannot_be_targeted():
    assert _failing_scope([_spec("tests/a.spec.ts")], [_BAD]) is None


def test_verdicts_that_do_not_line_up_with_the_files_are_not_trusted():
    assert _failing_scope([_spec("tests/a.spec.ts", "TC-1")], [_BAD, _BAD]) is None
    assert _failing_scope([_spec("tests/a.spec.ts", "TC-1")], None) is None


# --- a repair must never leave the suite smaller ----------------------------

from qa_pipeline.orchestrator.stages import _merge_repair  # noqa: E402


def test_a_rejected_file_survives_when_its_regeneration_failed():
    """Observed: the model quota ran out mid-repair, the rejected specs had
    already been deleted, and the suite shrank from 25 cases to 7."""
    kept = _spec("tests/ok.spec.ts", "TC-1")
    rejected = _spec("tests/bad.spec.ts", "TC-2", "TC-3")
    files, delete_paths, restore = _merge_repair([kept, rejected], [rejected], partial=[])
    assert {f["path"] for f in files} == {"tests/ok.spec.ts", "tests/bad.spec.ts"}
    assert delete_paths == [] and restore == [rejected]


def test_a_rejected_file_is_replaced_by_a_regenerated_one_covering_its_cases():
    kept = _spec("tests/ok.spec.ts", "TC-1")
    rejected = _spec("tests/bad.spec.ts", "TC-2", "TC-3")
    renamed = _spec("tests/bad_tc002-003.spec.ts", "TC-2", "TC-3")
    files, delete_paths, restore = _merge_repair([kept, rejected], [rejected], [renamed])
    assert {f["path"] for f in files} == {"tests/ok.spec.ts", "tests/bad_tc002-003.spec.ts"}
    assert delete_paths == ["tests/bad.spec.ts"] and restore == []


def test_a_regenerated_file_at_the_same_path_is_never_deleted():
    rejected = _spec("tests/bad.spec.ts", "TC-2")
    fixed = _spec("tests/bad.spec.ts", "TC-2")
    fixed["content"] = "y" * 40
    files, delete_paths, _restore = _merge_repair([rejected], [rejected], [fixed])
    assert delete_paths == [] and files[0]["content"] == "y" * 40


def test_a_partial_regeneration_does_not_drop_the_case_it_skipped():
    rejected = _spec("tests/bad.spec.ts", "TC-2", "TC-3")
    only_one = _spec("tests/bad_tc002.spec.ts", "TC-2")
    files, delete_paths, restore = _merge_repair([rejected], [rejected], [only_one])
    assert restore == [rejected] and delete_paths == []
    assert {f["path"] for f in files} == {"tests/bad.spec.ts", "tests/bad_tc002.spec.ts"}
