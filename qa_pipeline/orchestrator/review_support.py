"""Review-stage support: giving every failing case a verdict, saving verdicts the Reviewer skipped, and recording known flakes.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

from agents.artifact_io import read_json, write_json
from agents.bug_reporter import _signature
from qa_pipeline.agents import adapters


def nothing_to_review(run_result: dict, raw_report: dict) -> bool:
    """True when no case failed, so the Reviewer has no verdict to give.

    Deliberately conservative: any non-passing case in either view of the run,
    an unexpected count in the stats, or an infrastructure failure means a
    review IS needed.
    """
    done = ("passed", "skipped", "expected")
    rows = list(adapters._flatten_cases(raw_report or {})) + list(
        (run_result or {}).get("cases") or [])
    if any(c.get("status") not in done for c in rows):
        return False
    stats = {**((raw_report or {}).get("stats") or {}), **((run_result or {}).get("stats") or {})}
    if (stats.get("unexpected") or 0) or (stats.get("flaky") or 0):
        return False
    return not (run_result or {}).get("failure_kind") and bool(rows)


def clear_stale_verdicts(review_path) -> None:
    """An all-green run must not leave a previous run's verdicts where a resume
    (or the Bug Reporter) would read them as current. Compliance findings are
    kept - they are about the code, not about this run's failures."""
    from pathlib import Path
    path = Path(review_path)
    if not path.is_file():
        return
    report = read_json(path, default={}) or {}
    if report.get("failure_classification"):
        report["failure_classification"] = []
        write_json(path, report)


def _persist_added_verdicts(review_path, added: list) -> None:
    """Writes verdicts the Reviewer did not give back into review_report.json.

    `_classify_every_failure` adds a "needs a person to triage" bug for every
    failing case the model skipped - but only into classifications.json. The
    Bug Reporter reads review_report.json, which the Reviewer wrote BEFORE those
    were added, so the very failures that most needed a person never reached
    BUG_LIST.md.
    """
    if not added:
        return
    report = read_json(review_path, default={}) or {}
    existing = report.get("failure_classification")
    existing = existing if isinstance(existing, list) else []
    have = {e.get("test_case_id") for e in existing if isinstance(e, dict)}
    for entry in added:
        if entry.get("test_case_id") not in have:
            existing.append({**entry, "classification": entry.get("verdict")})
    report["failure_classification"] = existing
    write_json(review_path, report)


def _classify_every_failure(report: dict, classifications: list) -> list:
    """Every failing case must come out of the Reviewer with a verdict.

    A failure the model simply skipped was dropped: not healed, not in the bug
    list, not anywhere. Add the missing ones as genuine_bug so a person sees
    them, rather than letting an unreviewed red test vanish.
    """
    classified = {c.get("test_case_id") for c in classifications or []}
    added = []
    for case in adapters._flatten_cases(report or {}):
        case_id = case.get("test_case_id")
        if case.get("status") in ("passed", "skipped", "expected") or not case_id:
            continue
        if case_id in classified:
            continue
        classified.add(case_id)
        added.append(adapters.normalize_classification({
            "test_case_id": case_id, "file": case.get("file", ""),
            "verdict": "genuine_bug", "severity": "n/a",
            "reason": "[the Reviewer returned no verdict for this failing case - "
                      "needs a person to triage] " + str(case.get("error_message") or "")[:300],
            "confidence": 0.3}))
    if added:
        print(f"      ⚠️  {len(added)} failing case(s) got no verdict from the Reviewer — "
              f"listed as bugs for triage: {', '.join(c['test_case_id'] for c in added)}")
    return list(classifications or []) + added


def _merge_known_flaky(ctx, classifications, known_flaky):
    """Folds newly-classified flakes forward so the NEXT run against this
    feature already knows about them instead of re-diagnosing the same
    recurring failure from scratch."""
    # Deduped on the failure's SIGNATURE (digits normalised, ANSI stripped), not the
    # model's free-text reason, which differs on every run: keyed on the prose the
    # list grew by one near-duplicate per run and changed the Reviewer's cached prefix.
    seen = {(e.get("test_case_id"), _signature(e)) for e in known_flaky}
    added = 0
    # A flake is something that FAILED AND THEN PASSED. The model's say-so is not
    # evidence: `_prematch_known_flaky` auto-labels future failures of a recorded
    # case without asking the model again, so one wrong label hides a real
    # regression for good.
    retried_ok = {c.get("test_case_id") for c in (ctx.state.get("executor") or {}).get("cases") or []
                  if c.get("flaky")}
    for entry in classifications:
        if entry.get("verdict") != "known_flaky":
            continue
        if entry.get("test_case_id") not in retried_ok:
            print(f"      Not recording {entry.get('test_case_id')} as known-flaky: it never "
                  f"passed on a retry, so there is no evidence it is a flake.")
            continue
        key = (entry.get("test_case_id"), _signature(entry))
        if key not in seen:
            known_flaky.append(entry)
            seen.add(key)
            added += 1
    if added:
        write_json(ctx.artifact("known_flaky.json"), known_flaky)
        print(f"      Recorded {added} new known-flaky pattern(s) for future runs.")
