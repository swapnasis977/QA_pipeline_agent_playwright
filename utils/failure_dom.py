"""
Harvests the failing screen's real DOM out of a Playwright JSON report.

The Self-Healer's hardest input to get right is "what does the failing
screen actually look like". Before this module it re-launched a browser
minutes after the run and tried to navigate BACK to that screen using
keywords — which, when those keywords were the Reviewer's prose, meant it
usually never left the dashboard and then patched a locator against the
wrong page. That is the mechanical cause of a heal that "fixes" something
and still fails.

Playwright is already standing on the right page at the moment of failure.
The scaffold's `src/fixtures/index.ts` `captureFailureDom` auto-fixture attaches
that screen's interactive elements to the report as "failure-dom-snapshot";
this module reads them back out, keyed by Test Case ID, in the SAME shape
utils/dom_inspector._snapshot() produces — so self_healer consumes either
source identically.

Entirely optional: a repo whose fixtures predate the attachment (or a suite
that doesn't use the shared `test` export) simply yields {}, and the
Self-Healer falls back to live re-inspection exactly as before.
"""

import json

from agents.artifact_io import read_text_file

#: Attachment name written by the captureFailureDom fixture. Changing it
#: requires changing it in fixtures.ts too — they are one contract.
ATTACHMENT_NAME = "failure-dom-snapshot"


def _match_case_id(title: str, known_case_ids) -> str:
    """Longest-first title-prefix match — the same rule (and the same reason)
    as reviewer.match_case_id_by_title: with positional ids, "...-TC-1" is a
    prefix of "...-TC-10", so an arbitrary iteration order attributes a
    failing screen to the wrong case."""
    candidates = sorted((cid for cid in known_case_ids or () if cid),
                        key=lambda cid: (-len(cid), cid))
    return next((cid for cid in candidates if title.startswith(cid)), None)


def _read_attachment(attachment: dict) -> dict:
    """Reads one attachment's JSON body, whether Playwright inlined it
    (`body`, base64) or spilled it to a file (`path`) — it chooses between
    those by size, so both shapes occur for the same fixture."""
    if attachment.get("path"):
        raw = read_text_file(attachment["path"])
    elif attachment.get("body"):
        import base64
        try:
            raw = base64.b64decode(attachment["body"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return {}
    else:
        return {}

    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract(run_results: dict, known_case_ids=None) -> dict:
    """Returns {test_case_id: {"url", "title", "elements": [...]}} for every
    case whose failing attempt captured a snapshot.

    Walks the report's arbitrarily-nested suite tree the same way
    reviewer._extract_failures and bug_reporter._index_screenshots_by_case
    do. Best-effort throughout: a malformed report yields {} rather than
    raising into a heal that has nothing to do with it.
    """
    if not isinstance(run_results, dict):
        return {}

    found = {}

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs", []) or []:
                case_id = _match_case_id(spec.get("title", ""), known_case_ids)
                if not case_id:
                    continue
                for test in spec.get("tests", []) or []:
                    for result in test.get("results", []) or []:
                        # Only a FAILED attempt's snapshot is the failing
                        # screen. On a retried test that eventually passed,
                        # the passing attempt's page is the WORKING one —
                        # healing toward it would be healing toward nothing.
                        if result.get("status") in ("passed", "skipped"):
                            continue
                        for attachment in result.get("attachments", []) or []:
                            if attachment.get("name") != ATTACHMENT_NAME:
                                continue
                            snapshot = _read_attachment(attachment)
                            if snapshot.get("elements"):
                                found[case_id] = snapshot
            walk(suite.get("suites"))

    try:
        walk(run_results.get("suites"))
    except (AttributeError, TypeError):
        return found
    return found


#: A failure this early almost never means "the selector drifted" — it means
#: the page was not the page. Step 1 or 2 of a long flow failing points at
#: session/auth/navigation state, which is a different diagnosis entirely.
_EARLY_FAILURE_STEPS = 2


def extract_timeline(report: dict, case_ids) -> dict:
    """{test_case_id: {steps_completed, last_step, failed_early, duration_ms}}.

    Playwright's trace is a zip the model cannot read, but the part that
    matters — how far the test got — is plain text in the JSON report and was
    never passed on. `self_healer`'s docstring has always claimed it receives
    a "failure trace/screenshot"; it received neither.

    Without it the healer cannot tell "failed on the first assertion" from
    "passed twelve steps and failed on the thirteenth", and those want
    opposite diagnoses. On a real run the test failed at step 1 of 20 and the
    healer concluded a "refactor introduced a regression in the selector" —
    when the session simply was not authenticated, so no locator on that page
    could ever have matched.
    """
    from utils.case_index import case_id_from_title as _case_id_from_title

    wanted = {c for c in (case_ids or set()) if c}
    found = {}
    if not isinstance(report, dict) or not wanted:
        return found

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs") or []:
                case_id = _case_id_from_title(spec.get("title") or "")
                if case_id not in wanted:
                    continue
                for test in spec.get("tests") or []:
                    for result in test.get("results") or []:
                        if result.get("status") in ("passed", "skipped"):
                            continue
                        steps = [s for s in (result.get("steps") or [])
                                 if str(s.get("title", "")).strip()]
                        found[case_id] = {
                            "steps_completed": len(steps),
                            "last_step": steps[-1].get("title", "") if steps else "",
                            "failed_early": len(steps) <= _EARLY_FAILURE_STEPS,
                            "duration_ms": int(result.get("duration") or 0),
                            "steps": [{"title": s.get("title", ""),
                                       "duration_ms": int(s.get("duration") or 0)}
                                      for s in steps[-8:]],
                        }
            walk(suite.get("suites"))

    walk(report.get("suites"))
    return found
