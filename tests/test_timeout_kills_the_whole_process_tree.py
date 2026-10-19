"""A timed-out Playwright run must not leave workers or browsers behind.

The failure this prevents: `subprocess.run(timeout=...)` kills only the direct child.
Playwright's workers and the browsers they launched are GRANDchildren; they survived,
holding file locks and ports, and the next run (or the next heal) started against a
half-dead environment. Matching leftovers by command line missed most browsers.

Uses real processes, so it proves the behaviour rather than a mock of it.
"""

import os
import subprocess
import sys
import time

import pytest

from agents import executor

PARENT = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
open(sys.argv[1], "w").write(str(child.pid))
time.sleep(120)
"""


def _alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _wait_until_dead(pid: int, seconds: float = 10) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.2)
    return not _alive(pid)


def test_a_timeout_kills_the_grandchildren_too(tmp_path):
    pid_file = tmp_path / "grandchild.pid"

    with pytest.raises(subprocess.TimeoutExpired):
        executor._run_capturing([sys.executable, "-c", PARENT, str(pid_file)],
                                cwd=str(tmp_path), timeout=4)

    grandchild = int(pid_file.read_text())
    try:
        assert _wait_until_dead(grandchild), "the grandchild process survived the timeout"
    finally:
        if _alive(grandchild):                      # never leave a stray process behind a test
            subprocess.run(["taskkill", "/PID", str(grandchild), "/F"] if os.name == "nt"
                           else ["kill", "-9", str(grandchild)], capture_output=True)


def test_a_normal_run_still_returns_its_output_and_exit_code(tmp_path):
    result = executor._run_capturing(
        [sys.executable, "-c", "import sys; print('héllo'); sys.stderr.write('warn'); sys.exit(3)"],
        cwd=str(tmp_path), timeout=30, env={**os.environ, "PYTHONIOENCODING": "utf-8"})

    assert result.returncode == 3
    assert "héllo" in result.stdout and "warn" in result.stderr
