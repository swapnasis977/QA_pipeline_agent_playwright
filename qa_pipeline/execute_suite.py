"""`qa-pipeline execute`: run the generated suite as it stands, in parallel, with an Allure report.

No model is called, so no API key is needed. This is the command CI and a QA engineer use to
re-run what already exists; `run` is for generating and healing tests from a story.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from qa_pipeline import execute_support

_ANSI = re.compile(r"\x1b\[[0-9;]*m")

#: Parallel workers when neither the flag nor the profile says. The target application is slow
#: and shares one login, so a modest fixed number is safer than "half the CPU cores".
DEFAULT_WORKERS = 4

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TESTS_FAILING = 6


def resolve_workers(flag, profile) -> int:
    """CLI flag > profile `execution.workers` > DEFAULT_WORKERS."""
    if flag is not None:
        return max(1, int(flag))
    try:
        configured = int(profile.get("execution.workers") or 0)
    except (TypeError, ValueError):
        configured = 0
    return configured if configured > 0 else DEFAULT_WORKERS


def summarise(result: dict) -> tuple[int, int, int]:
    """(passed, failed, skipped) from the normalised run result."""
    cases = result.get("cases") or []
    passed = sum(1 for c in cases if c.get("status") == "passed")
    skipped = sum(1 for c in cases if c.get("status") == "skipped")
    return passed, len(cases) - passed - skipped, skipped


def apply_timeouts(profile) -> None:
    """Sets the run's time limits. An operator's environment variable always wins.

    A whole-suite pass on this slow application outlasts the executor's 30-minute default, which
    kills the run and loses every result, so the default here is 90 minutes. A profile can set its
    own with `execution.timeout_minutes`; Playwright's own ceiling is kept 5 minutes below it so it
    stops itself and still writes the results gathered so far.
    """
    try:
        minutes = float(profile.get("execution.timeout_minutes") or 0)
    except (TypeError, ValueError):
        minutes = 0
    if minutes > 0 and "EXECUTOR_TIMEOUT_SECONDS" not in os.environ:
        os.environ["EXECUTOR_TIMEOUT_SECONDS"] = str(int(minutes * 60))
        os.environ.setdefault("QA_GLOBAL_TIMEOUT_MS", str(int(max(minutes - 5, 1) * 60 * 1000)))
        return
    os.environ.setdefault("EXECUTOR_TIMEOUT_SECONDS", "5400")
    os.environ.setdefault("QA_GLOBAL_TIMEOUT_MS", str(85 * 60 * 1000))


def _first_line(message: str) -> str:
    message = _ANSI.sub("", str(message or "")).strip()
    return next((ln.strip() for ln in message.splitlines() if ln.strip()), "")


def _report_partial(results_dir: Path) -> None:
    """When a run is cut short, say what it DID finish: those results are still on disk."""
    done = execute_support.cases_from_allure(results_dir)
    if not done:
        return
    good = sum(1 for c in done if c["status"] == "passed")
    bad = sum(1 for c in done if c["status"] in ("failed", "broken"))
    print(f"      Before it stopped, {len(done)} test(s) had finished: {good} passed, {bad} failed. "
          f"Their results are in {results_dir}.")


def cmd_execute(args, profile) -> int:
    from qa_pipeline.agents import adapters

    workspace = Path(profile.workspace)
    if not (workspace / "playwright.config.ts").is_file():
        print(f"No generated framework at {workspace}. Run `scaffold` (and `run`) first, "
              f"or point the profile's `workspace` at an existing Playwright project.")
        return EXIT_FAILED

    workers = resolve_workers(args.workers, profile)
    apply_timeouts(profile)
    # One fixed folder, overwritten each time: this is "the latest execution", not a story run.
    feature_dir = Path(profile.context_root) / "SUITE_latest"
    results_dir = workspace / "allure-results"
    print(f"      Executing {workspace.name}: {workers} parallel worker(s)"
          + (f", shard {args.shard}" if args.shard else "")
          + (f", only {', '.join(args.grep)}" if args.grep else "") + " (no model call)")

    watcher = None
    if getattr(args, "progress", False):
        watcher = execute_support.ProgressWatcher(results_dir)
        watcher.start()
    try:
        result = adapters.execute(workspace, feature_dir, workers=workers, shard=args.shard,
                                  grep_ids=args.grep or None, retries=args.retries,
                                  fresh_reports=not args.grep)
    finally:
        if watcher:
            watcher.finish()
    if not result.get("ran"):
        print(f"[FAILED] The suite did not run: {result.get('error') or result.get('failure_kind')}")
        _report_partial(results_dir)
        return EXIT_FAILED

    # Failures get ONE more attempt, alone, so an environment hiccup is not reported as a defect.
    # A case that fails again keeps its original message; only a case that turns green is "flaky".
    flaky: list[str] = []
    first_errors: dict = {}
    again = execute_support.rerun_candidates(result.get("cases"), getattr(args, "rerun_failed", "none"))
    if again:
        print(f"      Re-running {len(again)} failed test(s) once on 1 worker "
              f"to tell flaky from real failures...")
        second = adapters.execute(workspace, feature_dir, workers=1, grep_ids=again, retries=0,
                                  fresh_reports=False, out_filename="rerun_results.json")
        if second.get("ran"):
            first_errors = {c.get("test_case_id"): _first_line(c.get("error_message"))
                            for c in result.get("cases") or [] if c.get("test_case_id")}
            result, flaky = execute_support.merge_rerun(result, second)
        else:
            print(f"      The re-run could not start ({second.get('error') or second.get('failure_kind')}); "
                  f"the original results stand.")

    passed, failed, skipped = summarise(result)
    print(f"      Result: {passed} passed, {failed} failed, {skipped} skipped")
    known = 0
    for case in result.get("cases") or []:
        if case.get("status") in ("passed", "skipped"):
            continue
        name = case.get("test_case_id") or case.get("file")
        if case.get("issue"):
            known += 1
            print(f"        KNOWN ISSUE {name}: {str(case['issue'])[:140]}")
        else:
            print(f"        FAILED {name}: {_first_line(case.get('error_message'))[:140]}")
    if flaky:
        print(f"      Flaky (failed, then passed on the re-run): {', '.join(flaky)}")
    from qa_pipeline import flaky_history
    history = flaky_history.record(profile.context_root / flaky_history.HISTORY_FILE,
                                   result.get("cases"), flaky, first_errors)
    for cid, count, window, error in flaky_history.chronic(history):
        print(f"      CHRONIC FLAKY {cid}: flaked in {count} of its last {window} runs"
              + (f" - last error: {error[:100]}" if error else "")
              + " (a pattern, not a one-off: fix its cause or quarantine it deliberately)")
    app_errors: dict[str, int] = {}
    for case in result.get("cases") or []:
        for message in case.get("app_errors") or []:
            app_errors[message] = app_errors.get(message, 0) + 1
    for message, count in sorted(app_errors.items(), key=lambda kv: -kv[1])[:5]:
        print(f"      App script error seen in {count} test(s): {message[:140]}")
    if known:
        print(f"      {known} of the failures are KNOWN ISSUES annotated in the specs "
              f"(the case and the application disagree); they still count as failed.")

    # Asked for explicitly by running this command: only --no-allure turns the report off.
    if args.allure:
        from agents.executor import generate_allure_report
        replaced = execute_support.dedupe_allure_results(results_dir)
        if replaced:
            print(f"      Allure: {replaced} earlier result(s) replaced by the re-run, "
                  f"so each test appears once.")
        allure = generate_allure_report(workspace)
        if allure.get("error"):
            print(f"      Allure report not generated: {allure['error']}")
        else:
            print(f"      Allure report: {allure.get('report_dir', '')}"
                  f"  (open it with: npx allure open allure-report, inside the workspace)")
    from qa_pipeline.story_bundle import refresh_bundles
    refresh_bundles(profile)
    print(json.dumps({"passed": passed, "failed": failed, "skipped": skipped, "workers": workers,
                      "flaky": len(flaky), "known_issues": known}))

    if failed and not args.allow_failures:
        print("      (exit code 6 means some tests failed; the run itself worked. "
              "Do not chain this command with `||`: it would start a second full run.)")
        return EXIT_TESTS_FAILING
    return EXIT_OK
