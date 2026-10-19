"""Flaky history: which cases keep failing-then-passing, across runs.

`execute` already re-runs each failure once and calls the ones that turn green "flaky", but
that verdict lived only in one run's console output. A case that flakes every second run is
a different problem from one that flaked once, and nobody could tell which was which.

This keeps a small per-case record (`flaky_history.json` next to the runs) and reports the
CHRONIC ones. It only reports: quarantining a test is a human decision, so nothing here
skips or hides a case. Deterministic: no model, no network.
"""

from __future__ import annotations

import json
from pathlib import Path

from utils.timestamps import utc_iso

HISTORY_FILE = "flaky_history.json"
#: How many recent outcomes are kept per case.
KEEP = 10
#: A case is chronic when it flaked at least this many times among its last WINDOW outcomes.
CHRONIC_AT = 2
WINDOW = 5


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def record(path: Path, cases: list, flaky_ids, first_errors: dict | None = None) -> dict:
    """Adds this run's outcome for every case that actually ran. Returns the history."""
    history = _load(path)
    flaky_ids = set(flaky_ids or ())
    for case in cases or []:
        cid = case.get("test_case_id")
        status = case.get("status")
        if not cid or status == "skipped":
            continue
        entry = history.setdefault(cid, {"recent": []})
        outcome = "flaky" if cid in flaky_ids else ("pass" if status == "passed" else "fail")
        entry["recent"] = (entry.get("recent") or [])[-(KEEP - 1):] + [outcome]
        if outcome == "flaky":
            entry["last_flaky_at"] = utc_iso()
            error = (first_errors or {}).get(cid)
            if error:
                entry["last_error"] = error[:200]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(history, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # a report must never fail a run
    return history


def chronic(history: dict) -> list:
    """[(case id, flaky count in the window, window size, last error)], worst first."""
    rows = []
    for cid, entry in (history or {}).items():
        recent = (entry.get("recent") or [])[-WINDOW:]
        count = recent.count("flaky")
        if count >= CHRONIC_AT:
            rows.append((cid, count, len(recent), entry.get("last_error", "")))
    return sorted(rows, key=lambda r: (-r[1], r[0]))
