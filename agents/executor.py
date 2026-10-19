"""
Agent 4: Executor Agent
IN:  Automation suite (repo test dir), execution environment config
OUT: Run results JSON (pass/fail per case), logs, failure list

This agent doesn't call the LLM at all — it's a thin wrapper around your
existing test runner (Playwright CLI). No new tool, no extra token cost,
matches the "reuse what you already have" principle from the design.
"""

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import read_json, write_json
from utils.run_layout import artifact_path

#: Observed in practice (Windows): a file the Coding Agent just wrote via
#: write_text() can be transiently invisible to a FRESHLY SPAWNED npx/node
#: process's own file-collection pass — Playwright reports "No tests found"
#: (an empty suite) even though the file demonstrably exists on disk and
#: collects fine moments later with no code change at all. Same root cause
#: class as coding_agent._run_with_retry's OSError note (AV real-time
#: scanning briefly locking a just-created file). A single short-delay
#: retry, gated on the target file(s) actually existing, catches this
#: without masking a REAL "no tests found" (e.g. wrong path, bad testMatch)
#: — those still fail identically on the retry and surface normally.
_EMPTY_SUITE_RETRY_DELAY_SEC = 2

#: Distinguishes "the test environment itself is broken" (missing npx, bad
#: repo_dir, timeout, Playwright crashed before producing a report) from
#: "Playwright ran and produced real pass/fail results" (report exists,
#: whether or not everything passed) — a caller retrying/alerting on the
#: former should NOT treat it the same as a genuine test failure. Additive:
#: existing "error"/"returncode" keys are unchanged, this is a new key only.
_INFRA = "infra"
_TEST_RESULTS = "test_results"

#: Default subprocess timeout (seconds) for the Playwright run — overridable
#: per-call via run()'s timeout_seconds param, or globally via the
#: EXECUTOR_TIMEOUT_SECONDS env var (an explicit timeout_seconds argument
#: takes precedence over the env var).
_DEFAULT_TIMEOUT_SECONDS = 1800


def _run_capturing(cmd, *, cwd, timeout, env=None):
    """`subprocess.run(capture_output=True, text=True)` with two Windows fixes.

    1. UTF-8 decoding with `errors="replace"`: `text=True` alone decodes with the
       console code page (cp1252), so one non-ASCII character in Playwright's
       output raised UnicodeDecodeError, which no caller caught — it escaped
       `run()` and killed the stage.
    2. After a TIMEOUT, processes the killed `npx.cmd` left behind (node
       workers, browsers) are cleaned up. `subprocess.run` only kills the direct
       child, so they survived, holding file locks and ports for the next run.

    3. The child runs in its OWN process group/session and the WHOLE TREE is killed on a
       timeout or interrupt. `subprocess.run` kills only the direct child; Playwright's
       workers and the browsers they launched are grandchildren, and matching them by
       command line (see `_kill_leftovers`) misses any whose command line does not
       mention the workspace, which is most browsers (they carry a temp `--user-data-dir`).

    Raises subprocess.TimeoutExpired exactly like `subprocess.run`.
    """
    options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", **options)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except BaseException as exc:    # TimeoutExpired, KeyboardInterrupt, anything else
        _kill_tree(proc)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=10)      # drain the pipes so nothing is left holding them
        if isinstance(exc, subprocess.TimeoutExpired):
            _kill_leftovers(cwd)              # belt and braces for anything that escaped the tree
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _kill_tree(proc) -> None:
    """Kills `proc` and every process it started."""
    if proc.poll() is not None and os.name != "nt":
        return
    try:
        if os.name == "nt":
            # /T = the whole tree, /F = force. Present on every supported Windows.
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30, check=False)
        else:
            import signal
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        pass
    with contextlib.suppress(OSError):
        proc.kill()                           # at minimum the direct child


def _playwright_command(repo_dir, npx) -> list:
    """`node <workspace>/node_modules/@playwright/test/cli.js test` when present,
    else `npx playwright test`.

    Going through node directly skips `npx.cmd`, which on Windows is run by
    cmd.exe: there an unquoted `--grep=A|B` is read as a PIPE, so the first
    grouped heal verification ran the wrong thing. It is also one process fewer
    to clean up after a timeout. Behaviour is otherwise identical.
    """
    cli = Path(repo_dir) / "node_modules" / "@playwright" / "test" / "cli.js"
    node = shutil.which("node")
    if node and cli.is_file():
        return [node, str(cli), "test"]
    return [npx, "playwright", "test"]


def _setup_failure(report: dict) -> str:
    """The error text of a failed `*.setup.ts` test, or "" when setup is fine."""
    found = []

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs") or []:
                if not str(spec.get("file") or suite.get("file") or "").endswith(".setup.ts"):
                    continue
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    last = results[-1] if results else {}
                    if last.get("status") in ("failed", "timedOut", "interrupted"):
                        message = str((last.get("error") or {}).get("message") or "").strip()
                        found.append(message.splitlines()[0][:300] if message else
                                     f"{spec.get('title') or 'setup'} {last.get('status')}")
            walk(suite.get("suites"))

    walk(report.get("suites"))
    return ("Sign-in/setup failed, so no test could run: " + found[0]) if found else ""


def _fresh_auth_state(repo_dir, max_age_seconds: float = 25 * 60) -> bool:
    """Whether the saved sign-in (`.auth/user.json`) is recent enough to reuse."""
    state = Path(repo_dir) / ".auth" / "user.json"
    try:
        return state.is_file() and (time.time() - state.stat().st_mtime) < max_age_seconds
    except OSError:
        return False


def _kill_leftovers(cwd) -> None:
    """Best effort, Windows only: stop node/browser processes started from `cwd`."""
    if os.name != "nt":
        return
    needle = str(Path(cwd).resolve()).replace("'", "''")
    script = ("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
              f"$_.CommandLine.Contains('{needle}') -and $_.Name -match 'node|chrome|msedge' }} "
              "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=30, check=False)


def run(repo_dir: Path, env_config: dict, feature_dir: Path,
        test_files: list = None, out_filename: str = "run_results.json",
        workers: int = None, shard: str = None, timeout_seconds: float = None,
        grep_ids: list = None, reuse_auth: bool = False,
        retries: int = None, fresh_reports: bool = False) -> dict:
    """Runs Playwright and returns its JSON report plus `failure_kind`.

    `retries`       None keeps the config's own policy; 0 forces ONE attempt per
                    test. A heal's verification run uses 0: with a retry a patch
                    that merely got lucky on attempt 2 would count as fixed.
    `fresh_reports` wipes allure-results/ and playwright-report/ first, so the
                    run's reports describe THIS run, not an accumulation of old
                    ones. Only a full first run sets it; a heal re-run of two
                    cases must not delete the report of the other forty.
    """
    # env_config is OVERLAID on the real environment, not used as the whole
    # environment. Passing `{**env_config}` with the default env_config={}
    # handed the child an empty environment — no PATH, no SystemRoot — so
    # npx could never be found and every run failed for the wrong reason.
    env = {**os.environ, **env_config}

    if timeout_seconds is None:
        timeout_seconds = float(os.environ.get("EXECUTOR_TIMEOUT_SECONDS", _DEFAULT_TIMEOUT_SECONDS))

    # Resolve npx explicitly: on Windows it's npx.CMD, and CreateProcess does
    # not apply PATHEXT, so a bare "npx" raises FileNotFoundError even when
    # Node is correctly installed.
    npx = shutil.which("npx")
    feature_dir.mkdir(parents=True, exist_ok=True)
    out_path = artifact_path(feature_dir, out_filename)

    if npx is None:
        report = {
            "error": "npx not found on PATH — install Node.js/Playwright, or run "
                     "with --skip-execution if you have no live test environment.",
            "returncode": None, "failure_kind": _INFRA,
        }
        write_json(out_path, report)
        return report

    # Checked before subprocess.run, which raises NotADirectoryError (Windows)
    # rather than anything descriptive when cwd doesn't exist.
    if not Path(repo_dir).is_dir():
        report = {
            "error": f"repo_dir '{repo_dir}' does not exist — point --repo-dir at "
                     f"your Playwright project, or run with --skip-execution.",
            "returncode": None, "failure_kind": _INFRA,
        }
        write_json(out_path, report)
        return report

    # No --reporter flag here on purpose: Playwright's CLI --reporter option
    # REPLACES playwright.config.ts's reporter array rather than adding to
    # it — passing --reporter=json would silently drop the allure-playwright
    # reporter configured there. Both reporters run only when the CLI is left
    # to use the config as-is, so the JSON report is read from ITS output
    # FILE afterward instead of parsed from stdout.
    # WHERE THE SCAFFOLD ACTUALLY WRITES IT. playwright.config.ts configures
    # `['json', { outputFile: 'test-results/results.json' }]`, so looking for
    # `playwright-report.json` at the repo root found nothing on every single
    # run: a real run with real, parseable failures was reported as
    # "No playwright-report.json produced" with failure_kind=infra, the stage
    # summary said "0 passed, 0 failed", and the Reviewer was handed an empty
    # result for a run that had plenty to say. The root path is still checked
    # second so a workspace scaffolded before this, or one a team has
    # repointed by hand, keeps working.
    json_report_candidates = [
        Path(repo_dir) / "test-results" / "results.json",
        Path(repo_dir) / "playwright-report.json",
    ]
    for candidate in json_report_candidates:
        candidate.unlink(missing_ok=True)  # a stale file would look like a fresh result

    if fresh_reports:
        for stale in ("allure-results", "playwright-report"):
            shutil.rmtree(Path(repo_dir) / stale, ignore_errors=True)

    cmd = _playwright_command(repo_dir, npx)
    if test_files:
        cmd += test_files
    if grep_ids:
        # Only these cases. A verification re-run used to execute the whole spec
        # file (up to 11 tests, 80-490 s) to learn about one or two of them.
        import re as _re
        # `(?![\w-])` anchors the id: "...-TC-1" must not also run "...-TC-10".
        cmd.append("--grep=" + "|".join(_re.escape(str(i)) + r"(?![\w-])" for i in grep_ids if i))
    if retries is not None:
        cmd.append(f"--retries={int(retries)}")
    if reuse_auth and _fresh_auth_state(repo_dir):
        # Skip the sign-in setup project: its saved session is minutes old, and
        # re-running it cost ~30 s on every single heal verification.
        cmd.append("--no-deps")
    if workers:
        cmd.append(f"--workers={workers}")
    if shard:
        # Playwright's own distributed-run mechanism ("1/3" = this is shard 1
        # of 3) — splits the WHOLE suite across separate Executor invocations
        # (e.g. separate machines/CI jobs), each producing its own
        # run_results.json (pass distinct out_filename per shard, same as
        # the Self-Healer's per-case heal_rerun_*.json convention). This
        # process still only runs ITS shard; merging shards' reports back
        # into one is the caller's responsibility, same as any other CI
        # sharding setup — Playwright doesn't merge for you either.
        cmd.append(f"--shard={shard}")

    def _run_once():
        try:
            result = _run_capturing(cmd, cwd=repo_dir, env=env, timeout=timeout_seconds)
            # Resolved into a fresh local, NOT by assigning to a name defined
            # in the enclosing scope. Assigning `json_report_path` inside this
            # closure made it local for the whole function, so when NO
            # candidate existed the next line read an unbound local and raised
            # UnboundLocalError — a NameError subclass, caught by neither
            # `except` below, so it escaped run() and killed the caller. That
            # is precisely the "Playwright died before writing a report" case
            # the else-branch below was written to report, which meant that
            # branch had never executed once.
            json_report_path = next(
                (c for c in json_report_candidates if c.exists()), None)
            if json_report_path is not None:
                # read_json returns the default on a corrupt/truncated
                # report — which is what a Playwright process killed mid-write
                # leaves behind, and is an INFRA failure (no usable results),
                # not a test failure.
                rep = read_json(json_report_path, default=None)
                if isinstance(rep, dict):
                    rep["failure_kind"] = _TEST_RESULTS
                    setup_error = _setup_failure(rep)
                    if setup_error:
                        # The sign-in/setup project failed, so every test that
                        # depends on it was skipped with NO result. The run
                        # then looked like "0 cases, nothing failed" - a broken
                        # login reported as a clean run. It is an environment
                        # failure: say so, with the setup's own message.
                        rep["failure_kind"] = _INFRA
                        rep["error"] = setup_error
                else:
                    rep = {"raw_stdout": result.stdout, "error": result.stderr,
                           "failure_kind": _INFRA}
            else:
                # No report file at all usually means Playwright failed before
                # running any test (config error, install issue) — surface stderr.
                rep = {"error": result.stderr or (
                    "No JSON report produced. Looked in "
                    + ", ".join(str(c.relative_to(repo_dir)) for c in json_report_candidates)
                    + " - check the `json` reporter's outputFile in playwright.config.ts."),
                       "raw_stdout": result.stdout, "failure_kind": _INFRA}
            rep["returncode"] = result.returncode
            return rep
        except subprocess.TimeoutExpired:
            return {"error": f"Execution timed out after {timeout_seconds} second(s).", "returncode": None,
                    "failure_kind": _INFRA}
        except OSError as exc:
            # Catches FileNotFoundError AND NotADirectoryError/PermissionError —
            # all the ways a bad cwd or unusable executable surfaces per-platform.
            return {"error": f"Could not execute '{npx}' in '{repo_dir}': {exc}",
                    "returncode": None, "failure_kind": _INFRA}

    report = _run_once()

    # An empty suite ("No tests found") while every requested test_file
    # genuinely exists on disk is a transient collection race, not a real
    # failure — see _EMPTY_SUITE_RETRY_DELAY_SEC's docstring. Retried ONCE;
    # a second empty suite is treated as a real result (wrong path/testMatch
    # misconfiguration), not retried again.
    if (report.get("failure_kind") == _TEST_RESULTS and not report.get("suites")
            and test_files and all((Path(repo_dir) / f).is_file() for f in test_files)):
        time.sleep(_EMPTY_SUITE_RETRY_DELAY_SEC)
        retry_report = _run_once()
        if retry_report.get("suites"):
            retry_report["_empty_suite_retry"] = True
            report = retry_report

    # COLLECTED NOTHING, AND SAID WHY: that is a broken run, not a clean one.
    #
    # Playwright writes a perfectly valid JSON report for "No tests found" —
    # `suites: []`, `stats.expected: 0`, and the reason in top-level
    # `errors`. Because a report EXISTS, the classification above called it
    # test_results, so `ran` was True with zero cases: `stage_execute`
    # recorded "0 passed", the Reviewer was handed nothing, and the pipeline
    # exited 0. A bad target_file regex, a testMatch that stopped matching or
    # a renamed spec all land exactly here — the run verified nothing and
    # reported success.
    #
    # Deliberately AFTER the retry above, which exists for the transient
    # collection race and needs to see this same state as a retryable
    # test_results first. Requiring `suites` to be empty keeps it safe: a run
    # that collected anything at all has suites, whatever else went wrong.
    if (report.get("failure_kind") == _TEST_RESULTS and not report.get("suites")
            and report.get("errors")):
        first = report["errors"][0] if isinstance(report["errors"], list) else {}
        message = (first or {}).get("message") if isinstance(first, dict) else ""
        report["failure_kind"] = _INFRA
        report["error"] = report.get("error") or (
            f"Playwright collected no tests and reported: "
            f"{(message or 'no further detail').strip()[:400]}")

    write_json(out_path, report)
    return report


def generate_allure_report(repo_dir: Path, clean: bool = True, timeout_seconds: int = None) -> dict:
    """Generates a viewable HTML report from allure-results (written by the
    allure-playwright reporter configured in playwright.config.ts) via the
    allure-commandline dev dependency. Requires a JRE on PATH — the CLI is
    a thin Java wrapper.

    Returns {"report_dir": <path>} on success, or {"error": ...} — mirrors
    run()'s shape so callers can check for "error" the same way.
    """
    npx = shutil.which("npx")
    if npx is None:
        return {"error": "npx not found on PATH — cannot run allure-commandline."}

    results_dir = Path(repo_dir) / "allure-results"
    if not results_dir.is_dir():
        return {"error": f"No {results_dir} — run the Executor first "
                         f"(allure-playwright must be in playwright.config.ts's reporter list)."}

    report_dir = Path(repo_dir) / "allure-report"
    # Args are RELATIVE names only — cwd=repo_dir below already puts the
    # subprocess inside repo_dir, so passing repo_dir-PREFIXED paths here
    # too would double it up (e.g. "<workspace>/<workspace>/allure-report"),
    # confirmed in practice.
    cmd = [npx, "allure", "generate", "allure-results"]
    if clean:
        cmd.append("--clean")
    cmd += ["-o", "allure-report"]

    # A large allure-results directory (hundreds/thousands of result files)
    # cold-starting the JRE-backed CLI can plausibly exceed a fixed 120s —
    # make it configurable, same convention as run()'s EXECUTOR_TIMEOUT_SECONDS.
    timeout = timeout_seconds or int(os.environ.get("ALLURE_TIMEOUT_SECONDS", 120))
    try:
        result = _run_capturing(cmd, cwd=repo_dir, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"allure generate timed out after {timeout}s — "
                         f"set ALLURE_TIMEOUT_SECONDS to raise the limit for a large results set."}
    except OSError as exc:
        return {"error": f"Could not run allure-commandline: {exc}"}
    if result.returncode != 0:
        return {"error": f"allure generate failed (exit {result.returncode}): "
                         f"{result.stderr or result.stdout}"}

    return {"report_dir": str(report_dir), "index_html": str(report_dir / "index.html")}


def _cli():
    """Standalone entry point — run the suite (or a subset of test files)
    against a repo without going through the rest of the pipeline."""
    import argparse

    from utils.env_config import resolve_app_env

    resolve_app_env()  # the `npx playwright test` subprocess below inherits this process's env

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--test-files", nargs="*", default=None,
                         help="Specific spec file(s) to run; omit to run the whole suite.")
    parser.add_argument("--out-filename", default="run_results.json")
    parser.add_argument("--workers", type=int, default=None,
                         help="Passed through as Playwright's --workers=N (parallel test "
                              "workers). Omit to use playwright.config.ts's own default.")
    parser.add_argument("--shard", default=None,
                         help="Passed through as Playwright's --shard=X/Y for distributed "
                              "runs (e.g. '1/3'). Omit to run the whole suite in one process.")
    parser.add_argument("--allure", action="store_true",
                         help="Also generate an Allure HTML report after the run.")
    parser.add_argument("--timeout-seconds", type=float, default=None,
                         help="Subprocess timeout for the Playwright run (default: "
                              "EXECUTOR_TIMEOUT_SECONDS env var, or 1800).")
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)

    report = run(Path(args.repo_dir), env_config={}, feature_dir=feature_dir,
                 test_files=args.test_files, out_filename=args.out_filename,
                 workers=args.workers, shard=args.shard, timeout_seconds=args.timeout_seconds)
    print(f"\n✅ Executor finished (returncode={report.get('returncode')}, "
          f"failure_kind={report.get('failure_kind')}). "
          f"Results: {artifact_path(feature_dir, args.out_filename)}")

    if args.allure:
        allure_result = generate_allure_report(Path(args.repo_dir))
        print(json.dumps(allure_result, indent=2))

    # A CI step wrapping this must go red when the run did, not just print it.
    stats = report.get("stats") or {}
    if report.get("failure_kind") == _INFRA or stats.get("unexpected"):
        raise SystemExit(1)


if __name__ == "__main__":
    _cli()
