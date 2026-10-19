"""`execution.workers` in the profile must reach the Executor.

THE FAILURE. The value was never read: `RunContext` took only the `--workers` flag, and
the generated Playwright config falls back to half the CPU cores. A profile that said
`workers: 1` ran with seven, seven tests started in the same minute, the application
timed out under the load, and the failures were blamed on the tests. It also produced a
wrong conclusion that worker count made no difference.

Run with:  python -m pytest tests/test_workers_setting_is_honoured.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.cli import _resolve_workers  # noqa: E402


class _Profile:
    def __init__(self, workers):
        self._workers = workers

    def get(self, key, default=None):
        return self._workers if key == "execution.workers" else default


def test_the_profile_value_is_used_when_no_flag_is_given():
    assert _resolve_workers(SimpleNamespace(workers=None), _Profile(1)) == 1
    assert _resolve_workers(SimpleNamespace(workers=None), _Profile("2")) == 2


def test_the_command_line_flag_wins_over_the_profile():
    assert _resolve_workers(SimpleNamespace(workers=3), _Profile(1)) == 3


def test_nothing_configured_leaves_playwrights_own_default():
    assert _resolve_workers(SimpleNamespace(workers=None), _Profile(None)) is None
    assert _resolve_workers(SimpleNamespace(workers=None), _Profile("many")) is None
