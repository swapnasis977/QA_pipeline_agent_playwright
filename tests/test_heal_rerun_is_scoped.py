"""A heal's verification re-run must cost what it verifies.

THE FAILURE. After patching one case, the Self-Healer re-ran the WHOLE spec file
(up to 11 tests, 80-490 s on this app) and the sign-in setup project (~30 s)
every time, twice per group. Measured: a heal stage that should take minutes
needed most of an hour.

Run with:  python -m pytest tests/test_heal_rerun_is_scoped.py -q
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import executor  # noqa: E402


class _Done:
    returncode, stdout, stderr = 0, "", ""


def _capture(monkeypatch, tmp_path, **kwargs):
    seen = {}
    monkeypatch.setattr(executor.shutil, "which", lambda _n: "npx")

    def fake_run(cmd, **_kw):
        seen["cmd"] = list(cmd)
        return _Done()

    monkeypatch.setattr(executor, "_run_capturing", fake_run)   # the seam; subprocess is an implementation detail
    executor.run(tmp_path, env_config={}, feature_dir=tmp_path / "feature",
                 test_files=["tests/e2e/a.spec.ts"], **kwargs)
    return seen["cmd"]


def test_only_the_healed_cases_are_re_run(monkeypatch, tmp_path):
    cmd = _capture(monkeypatch, tmp_path, grep_ids=["PROJ-1-AUTO-TC-021", "PROJ-1-AUTO-TC-020"])
    grep = next(c for c in cmd if c.startswith("--grep="))
    assert "PROJ-1\-AUTO\-TC\-021|PROJ-1\-AUTO\-TC\-020" in grep or "TC-021" in grep.replace("\\", "")
    assert "tests/e2e/a.spec.ts" in cmd


def test_without_ids_the_whole_file_runs_as_before(monkeypatch, tmp_path):
    assert not any(c.startswith("--grep") for c in _capture(monkeypatch, tmp_path))


def test_a_fresh_saved_sign_in_is_reused(monkeypatch, tmp_path):
    state = tmp_path / ".auth" / "user.json"
    state.parent.mkdir()
    state.write_text("{}", encoding="utf-8")
    assert "--no-deps" in _capture(monkeypatch, tmp_path, reuse_auth=True)


def test_a_stale_sign_in_is_not_reused(monkeypatch, tmp_path):
    state = tmp_path / ".auth" / "user.json"
    state.parent.mkdir()
    state.write_text("{}", encoding="utf-8")
    old = time.time() - 3 * 3600
    os.utime(state, (old, old))
    assert "--no-deps" not in _capture(monkeypatch, tmp_path, reuse_auth=True)


def test_a_missing_sign_in_is_never_skipped(monkeypatch, tmp_path):
    assert "--no-deps" not in _capture(monkeypatch, tmp_path, reuse_auth=True)


def test_reuse_is_opt_in(monkeypatch, tmp_path):
    state = tmp_path / ".auth" / "user.json"
    state.parent.mkdir()
    state.write_text("{}", encoding="utf-8")
    assert "--no-deps" not in _capture(monkeypatch, tmp_path)
