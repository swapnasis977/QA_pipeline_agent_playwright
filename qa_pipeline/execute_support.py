"""Helpers `execute` wraps around one Playwright run: live progress, merging re-runs into one
report, and separating flaky failures from real ones.

Kept out of `execute_suite.py` so that module stays the thin command it is. Everything here is
plain files and plain dicts: no model, no network, nothing that needs a browser to test.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Errors that say the ENVIRONMENT hiccupped (page never loaded, connection dropped), as opposed
#: to an assertion that disagreed with the application. `--rerun-failed infra` retries only these.
INFRA_ERROR = re.compile(
    r"page\.goto|net::ERR_|ERR_TOO_MANY_REDIRECTS|ECONNRESET|ETIMEDOUT|ENOTFOUND|"
    r"Target (?:page|closed)|browser has been closed|Navigation failed",
    re.I)

RERUN_MODES = ("none", "infra", "all")


# --------------------------------------------------------------------------- re-runs / flaky

def rerun_candidates(cases: list, mode: str) -> list[str]:
    """Case ids worth ONE more attempt, in report order, without repeats.

    "all" retries every failed case that has an id; "infra" only those whose error looks like the
    environment, not the application. A skipped case is never retried.
    """
    if mode not in ("infra", "all"):
        return []
    ids: list[str] = []
    for case in cases or []:
        if case.get("status") in ("passed", "skipped") or not case.get("test_case_id"):
            continue
        message = _ANSI.sub("", str(case.get("error_message") or ""))
        if mode == "infra" and not INFRA_ERROR.search(message):
            continue
        if case["test_case_id"] not in ids:
            ids.append(case["test_case_id"])
    return ids


def merge_rerun(first: dict, second: dict) -> tuple[dict, list[str]]:
    """The first result with every case that PASSED on the re-run marked passed + flaky.

    A case that fails again stays failed with its original message, so a real defect is never
    hidden by the re-run. Returns (merged result, ids that turned green).
    """
    again = {c.get("test_case_id"): c for c in second.get("cases") or [] if c.get("test_case_id")}
    flaky: list[str] = []
    merged = []
    for case in first.get("cases") or []:
        redo = again.get(case.get("test_case_id"))
        if case.get("status") not in ("passed", "skipped") and redo and redo.get("status") == "passed":
            case = {**case, "status": "passed", "flaky": True,
                    "attempts": int(case.get("attempts") or 1) + 1, "error_message": ""}
            flaky.append(case["test_case_id"])
        merged.append(case)
    return {**first, "cases": merged}, flaky


# --------------------------------------------------------------------------- allure results

def _attachment_sources(node) -> list[str]:
    """Every attachment file name mentioned anywhere in one Allure result."""
    found: list[str] = []
    if isinstance(node, dict):
        if isinstance(node.get("source"), str):
            found.append(node["source"])
        for value in node.values():
            found += _attachment_sources(value)
    elif isinstance(node, list):
        for value in node:
            found += _attachment_sources(value)
    return found


def dedupe_allure_results(results_dir) -> int:
    """Keeps ONE result per test (the newest) so a re-run REPLACES the earlier attempt in the report.

    Without this a targeted re-run is appended to the results already there, and the report lists
    the same test twice (once red, once green) with totals that match no single run. Returns how
    many older results were removed, with their attachment files.
    """
    folder = Path(results_dir)
    newest: dict[str, tuple[int, Path, dict]] = {}
    stale: list[tuple[Path, dict]] = []
    for path in folder.glob("*-result.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = str(data.get("historyId") or data.get("fullName") or data.get("name") or path.name)
        start = int(data.get("start") or 0)
        kept = newest.get(key)
        if kept is None:
            newest[key] = (start, path, data)
        elif start >= kept[0]:
            stale.append((kept[1], kept[2]))
            newest[key] = (start, path, data)
        else:
            stale.append((path, data))
    for path, data in stale:
        for source in _attachment_sources(data):
            (folder / source).unlink(missing_ok=True)
        path.unlink(missing_ok=True)
    return len(stale)


def cases_from_allure(results_dir) -> list[dict]:
    """(name, status) of every finished result: what a run got through before it was cut short."""
    cases = []
    for path in Path(results_dir).glob("*-result.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        cases.append({"name": str(data.get("name") or ""), "status": str(data.get("status") or "")})
    return cases


# --------------------------------------------------------------------------- live progress

class ProgressWatcher(threading.Thread):
    """Prints one plain line per test as its Allure result appears, plus a running total.

    `execute` otherwise says nothing until the whole run ends, which can be an hour. This only
    READS the results folder; it never touches the run, and it does nothing at all when the
    folder does not exist (a mocked run, or `--no-allure` setups without the reporter).
    """

    LABEL = {"passed": "PASS  ", "failed": "FAIL  ", "broken": "BROKEN", "skipped": "SKIP  "}

    def __init__(self, results_dir, out=print, interval: float = 3.0):
        super().__init__(daemon=True)
        self.folder = Path(results_dir)
        self.out = out
        self.interval = interval
        self.counts: dict[str, int] = {}
        # Results already on disk belong to an EARLIER run (a targeted re-run keeps them): not news.
        self._seen: set[str] = {p.name for p in self.folder.glob("*-result.json")} if self.folder.is_dir() else set()
        self._halt = threading.Event()

    def poll(self) -> int:
        """Reads the folder once; returns how many new results were reported."""
        if not self.folder.is_dir():
            return 0
        new = 0
        for path in sorted(self.folder.glob("*-result.json"), key=lambda p: p.stat().st_mtime):
            if path.name in self._seen:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue                    # still being written: next poll
            self._seen.add(path.name)
            status = str(data.get("status") or "?")
            self.counts[status] = self.counts.get(status, 0) + 1
            new += 1
            seconds = (int(data.get("stop") or 0) - int(data.get("start") or 0)) / 1000
            message = str((data.get("statusDetails") or {}).get("message") or "")
            first = next((ln.strip() for ln in _ANSI.sub("", message).splitlines() if ln.strip()), "")
            why = f"  -> {first[:90]}" if first and status != "passed" else ""
            self.out(f"      [{self.LABEL.get(status, status):6}] {str(data.get('name') or '')[:70]} "
                     f"({seconds:.0f}s){why}")
        if new:
            total = sum(self.counts.values())
            self.out(f"      ---- finished {total} | pass {self.counts.get('passed', 0)} | "
                     f"fail {self.counts.get('failed', 0)} | broken {self.counts.get('broken', 0)} "
                     f"| skip {self.counts.get('skipped', 0)}")
        return new

    def run(self) -> None:
        while not self._halt.wait(self.interval):
            self.poll()

    def finish(self) -> None:
        """Stops the thread after one last look, so the final results are not lost."""
        self._halt.set()
        self.join(timeout=5)
        self.poll()

