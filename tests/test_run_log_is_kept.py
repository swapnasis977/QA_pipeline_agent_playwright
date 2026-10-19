"""A run's console output must survive in a file.

The failure this prevents: ~290 `print` calls and no log, so a run that scrolled off the
terminal - or ran in CI - left nothing to read afterwards. And a log must never be able
to break the run it is recording.
"""

import sys

from utils.runlog import tee_to


def test_everything_printed_is_kept_with_timestamps(tmp_path, capsys):
    log = tmp_path / "_internal" / "run.log"
    with tee_to(log):
        print("first line")
        print("to stderr", file=sys.stderr)
        sys.stdout.write("partial ")
        sys.stdout.write("line\n")

    text = log.read_text(encoding="utf-8")
    assert "first line" in text and "to stderr" in text and "partial line" in text
    assert text.count("first line") == 1
    # the console still got it
    assert "first line" in capsys.readouterr().out


def test_streams_are_restored_afterwards(tmp_path):
    before = (sys.stdout, sys.stderr)
    with tee_to(tmp_path / "run.log"):
        assert sys.stdout is not before[0]
    assert (sys.stdout, sys.stderr) == before


def test_a_log_that_cannot_be_opened_does_not_stop_the_run(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with tee_to(blocker / "run.log") as opened:     # a path UNDER a file: cannot be created
        print("still runs")
    assert opened is None
