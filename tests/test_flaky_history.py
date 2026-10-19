"""Flaky history must tell a pattern from a one-off, and must never fail a run.

The failure this prevents: `execute` named the flaky cases of ONE run and forgot them, so a
case that flaked every other run looked the same as one that flaked once.
"""

from qa_pipeline import flaky_history as fh


def _case(cid, status="passed"):
    return {"test_case_id": cid, "status": status}


def test_a_case_that_flakes_twice_in_five_runs_is_chronic_and_a_one_off_is_not(tmp_path):
    path = tmp_path / "flaky_history.json"
    for run in range(4):
        flaky = ["A-1"] if run in (1, 3) else (["B-2"] if run == 0 else [])
        fh.record(path, [_case("A-1"), _case("B-2")], flaky, {"A-1": "net::ERR_TOO_MANY_REDIRECTS"})
    rows = fh.chronic(fh.record(path, [_case("A-1"), _case("B-2")], []))
    assert [r[0] for r in rows] == ["A-1"]
    assert rows[0][1] == 2 and "REDIRECTS" in rows[0][3]


def test_skipped_cases_are_not_counted_as_runs(tmp_path):
    path = tmp_path / "h.json"
    history = fh.record(path, [_case("S-1", "skipped")], [])
    assert "S-1" not in history


def test_an_unwritable_history_file_does_not_fail_the_run(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    # the parent is a FILE, so mkdir/write must fail; record() still returns
    assert fh.record(blocker / "h.json", [_case("A-1")], []) is not None
