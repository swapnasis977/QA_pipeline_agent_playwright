"""
Agent 5: Automation Reviewer
IN:  Coding standards doc, list of failing tests + logs, list of failure reasons
OUT: Compliance confirmation, deviation list, known-vs-new failure classification

Pure reasoning agent — no external tools needed (per lean design).
"""

import contextlib
import difflib
import json
import re
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import read_json, read_text_file, write_json
from utils import failure_dom as _failure_dom
from utils.llm_client import append_context_summary, call_agent, parse_json_response
from utils.run_layout import artifact_path

#: Where the exact content of every file sent to the LLM on the LAST review
#: is stashed, so a re-run against the same feature (e.g. after a small
#: self-heal patch, or re-running just to re-check flaky failures) doesn't
#: re-send every OTHER unchanged file's full body again — see _files_for_prompt().
_PRIOR_FILES_FILENAME = "reviewer_prior_files.json"

#: Header text pom_builder writes into every deterministic file it generates
#: (same value as agents.self_healer.GENERATED_MARKER; not imported to keep this
#: module free of the healer's dependencies).
_GENERATED_MARKER = "GENERATED from the"
_GENERATED_HEAD_CHARS = 600

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _compact_json(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)

# Standing instructions live in skills/reviewer.skill.md (cached).
# known_flaky_patterns is passed as extra_system since it's a standing,
# rarely-changing list specific to your codebase — keeping it out of the
# user prompt means it doesn't get re-typed into the growing conversation.


def _load_prior_files(feature_dir: Path) -> dict:
    # A corrupt cache degrades to "every file looks new" (full content
    # re-sent, correct but more expensive) instead of crashing the review.
    return read_json(artifact_path(feature_dir, _PRIOR_FILES_FILENAME), default={})


def _save_prior_files(feature_dir: Path, generated_files: list):
    write_json(artifact_path(feature_dir, _PRIOR_FILES_FILENAME),
               {f.get("path", "?"): f.get("content", "") for f in generated_files})


def _files_for_prompt(generated_files: list, prior_files: dict) -> list:
    """A file identical to what was sent on the LAST review of this feature
    doesn't need its full body re-sent — that exact content was already
    checked for hardcoded waits/weak assertions/etc, and review_report.json
    from that run still has the verdict. A CHANGED file still gets a diff
    (not silence) rather than being dropped, since the changed lines are
    exactly what a fresh review needs to see; a genuinely NEW file still
    gets its full content, same as before this change existed."""
    out = []
    for f in generated_files:
        path, content = f.get("path", "?"), f.get("content", "")
        # Page objects and the route helper are emitted by deterministic code (see
        # pom_builder), not written by the model: nothing there for a compliance
        # review to catch, and they are the bulk of the bytes on first sight.
        if _GENERATED_MARKER in str(content)[:_GENERATED_HEAD_CHARS]:
            continue
        prior = prior_files.get(path)
        if prior is None:
            out.append({"path": path, "status": "new", "content": content})
        elif prior == content:
            out.append({"path": path, "status": "unchanged_since_last_review",
                        "note": "Identical to the content already reviewed for compliance "
                                "in a previous run — see review_report.json for that verdict. "
                                "Full content omitted here to save tokens."})
        else:
            diff = "\n".join(difflib.unified_diff(
                prior.splitlines(), content.splitlines(),
                fromfile=f"{path} (last reviewed)", tofile=f"{path} (current)",
                lineterm="",
            ))
            out.append({"path": path, "status": "changed_since_last_review", "diff": diff})
    return out


def match_case_id_by_title(title: str, known_case_ids) -> str:
    """The case id a test title belongs to, by the coding_agent convention
    that every title STARTS with its Test Case ID verbatim.

    Candidates are tried LONGEST FIRST, and the iteration order is made
    deterministic by sorting — `next(cid for cid in <set> if ...)` picked an
    arbitrary winner whenever one id is a prefix of another ("TC-1" vs
    "TC-10", or "PROJ-68319-TC-1" vs "...-TC-10", which this pipeline's
    positional ids produce as soon as a story has ten or more cases). Set
    iteration order also varies per process, so the same report could map to
    a different case on a re-run. Longest match is the only correct one:
    "TC-10 ..." starts with "TC-1" too, but "TC-10" is the real owner.
    """
    candidates = sorted((cid for cid in known_case_ids or () if cid),
                        key=lambda cid: (-len(cid), cid))
    return next((cid for cid in candidates if title.startswith(cid)), None)


def _extract_failures(run_results: dict, known_case_ids: set) -> list:
    """Walks the Playwright JSON reporter's suite tree (same shape/convention
    bug_reporter._index_screenshots_by_case relies on — every test title
    starts with its Test Case ID verbatim) collecting each currently-failing
    case's error message. Used only for the deterministic known_flaky
    pre-match below — parse failures here are non-fatal (best-effort), since
    worst case is just "no pre-match, fall back to asking the LLM"."""
    failures = []

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs", []) or []:
                title = spec.get("title", "")
                case_id = match_case_id_by_title(title, known_case_ids)
                if not case_id:
                    continue
                for test in spec.get("tests", []) or []:
                    for result in test.get("results", []) or []:
                        if result.get("status") in ("passed", "skipped"):
                            continue
                        error = result.get("error") or {}
                        message = error.get("message") or ""
                        if not message and result.get("errors"):
                            message = (result["errors"][0] or {}).get("message", "")
                        failures.append({"test_case_id": case_id, "file": spec.get("file", ""),
                                          "error": _ANSI_RE.sub("", str(message)),
                                          # Playwright's own verdict: "flaky" = failed,
                                          # then passed on a retry.
                                          "flaky": test.get("status") == "flaky"})
            walk(suite.get("suites"))

    with contextlib.suppress(Exception):  # best-effort — see docstring
        walk(run_results.get("suites"))
    return failures


#: Overall similarity (difflib ratio) above which two failure messages are
#: treated as the same failure even with no long shared verbatim run — this
#: is the "same error, different numbers" case: a timeout that reports 5000ms
#: this run and 3000ms when the pattern was recorded is the SAME known flake.
_NEAR_IDENTICAL_RATIO = 0.85

#: Length of shared VERBATIM text that, on its own, identifies a repeat
#: failure — long enough that Playwright's generic boilerplate ("Error:
#: Timeout 30000ms exceeded while waiting for locator...", ~57 chars) can't
#: reach it, so two unrelated failures can't collide on framework prose alone.
_MIN_SHARED_RUN = 80

#: Floor applied alongside _MIN_SHARED_RUN: a long shared run inside two
#: otherwise very different messages still isn't a match.
_MIN_SUPPORTING_RATIO = 0.6


def _messages_match(reason: str, error: str) -> bool:
    """Whether a recorded known_flaky reason and a current error message
    describe the SAME failure. Both inputs are already lowercased.

    Two independent signals, either sufficient:
      - near-identical overall (>= _NEAR_IDENTICAL_RATIO), or
      - a long shared verbatim run (>= _MIN_SHARED_RUN) backed by a
        reasonable overall similarity.

    The previous implementation tested `reason[:80] in error or
    error[:80] in reason`, which is PREFIX CONTAINMENT, not the shared
    substring its comment described. For any message shorter than 80 chars
    that slice is the entire string, so the test collapsed to "one message
    contains the other verbatim" — a single differing digit (5000ms vs
    3000ms, a different element index, a different port) defeated it. The
    pre-match therefore almost never fired on real Playwright errors, and
    every recurrence of an ALREADY-RECORDED flake silently fell through to a
    paid LLM classification — the exact cost this function exists to avoid,
    and the same failure mode as the earlier `reason == failure_reason`
    byte-equality bug noted in _prematch_known_flaky's docstring.
    """
    matcher = difflib.SequenceMatcher(None, reason, error)
    ratio = matcher.ratio()
    if ratio >= _NEAR_IDENTICAL_RATIO:
        return True
    # find_longest_match over the whole of both strings — the real "shared
    # substring" the original comment intended, rather than a prefix test.
    longest = matcher.find_longest_match(0, len(reason), 0, len(error)).size
    return longest >= _MIN_SHARED_RUN and ratio > _MIN_SUPPORTING_RATIO


def _is_known_flaky_match(known_flaky_entry: dict, error_lower: str) -> bool:
    """Whether one known_flaky.json entry describes `error_lower`."""
    reason_lower = _ANSI_RE.sub("", str(known_flaky_entry.get("reason") or "")).lower()
    if not reason_lower or not error_lower:
        return False
    return _messages_match(reason_lower, error_lower)


def _prematch_known_flaky(failures: list, known_flaky_patterns: list) -> tuple:
    """Deterministic pre-match: a failure whose test_case_id already has a
    known_flaky.json entry, AND whose CURRENT error message meaningfully
    overlaps that entry's recorded reason, is classified as known_flaky
    without asking the LLM to re-derive a call it already made once. Only
    an exact-repeat case is pre-matched — anything the substring check
    doesn't confidently recognize still goes to the LLM, same as before this
    existed, so this can only ever REDUCE what's asked, never misclassify
    something the model would have caught."""
    by_case_id = {}
    for kf in known_flaky_patterns:
        cid = kf.get("test_case_id")
        if cid:
            by_case_id.setdefault(cid, []).append(kf)

    prematched, remaining = [], []
    for failure in failures:
        kf_entries = by_case_id.get(failure["test_case_id"], [])
        error_lower = (failure.get("error") or "").lower()

        # Module-level _is_known_flaky_match rather than a closure over
        # error_lower: a function defined inside the loop that captures a
        # loop variable is the classic late-binding hazard (ruff B023) —
        # correct only as long as it is called within the same iteration,
        # which is exactly the kind of invariant a later refactor breaks
        # silently. Passing the value explicitly removes the hazard.
        # known_flaky needs a case that PASSED at least once (the skill's rule, and
        # what `_merge_known_flaky` enforces when recording). A case failing every
        # attempt is a failure even if its message matches a recorded flake, or a
        # real regression gets pre-labelled and dropped from the bug list.
        match = next((kf for kf in kf_entries
                      if failure.get("flaky") and _is_known_flaky_match(kf, error_lower)), None)
        if match and any(p["test_case_id"] == failure["test_case_id"] for p in prematched):
            continue  # several failed attempts of one flaky case: one verdict is enough
        if match:
            prematched.append({
                "test_case_id": failure["test_case_id"], "file": failure.get("file", ""),
                "classification": "known_flaky", "severity": "n/a",
                "reason": match.get("reason", ""),
                "suggested_fix": match.get("suggested_fix", ""),
            })
        else:
            remaining.append(failure)
    return prematched, remaining


#: Per-message cap on an error/stack string forwarded to the model. A
#: Playwright failure message carries the assertion diff plus a full stack;
#: the first ~2000 chars hold the assertion and the top frames, which is
#: what a classification decision actually turns on.
_MAX_ERROR_CHARS = 2000


def _compact_run_results(run_results: dict, exclude_case_ids=None, known_case_ids=None) -> dict:
    """Reduces the Playwright JSON report to what a review decision needs.

    The whole report was previously serialized into the prompt. That report
    embeds, for EVERY test including passing ones, the full step tree, per-
    step timings, stdout/stderr, and attachment metadata — routinely
    megabytes for a suite of any size. Three costs: the failures the agent
    is asked to classify are buried in mostly-irrelevant structure; the
    input is paid for on every re-review; and a large enough report pushes
    the request past the model's context window, at which point the review
    fails outright rather than degrading.

    Kept: top-level stats/errors, and one entry for every test with AT LEAST
    ONE non-passing attempt — including one that passed on retry, since that
    is what a "known_flaky" classification is about — with its file, title,
    final status, attempt counts, and truncated error text. Tests that
    passed on the first attempt are reduced to a count: a compliance check
    reads the file CONTENT (sent separately, in full), never the passing-run
    structure, and failure classification by definition concerns failures.

    `exclude_case_ids` (with `known_case_ids` to map titles to ids) drops cases
    already classified deterministically (the known_flaky pre-match): the model is
    told not to classify them, so their errors would be paid for and ignored.
    They are counted in `prematched_count` so nothing silently disappears.

    Returns the input unchanged if it isn't a recognizable report (e.g. one
    of executor.py's infra-error shapes: {"error": ..., "failure_kind":
    "infra"}), since those are small and the error text is the whole point.
    """
    if not isinstance(run_results, dict) or "suites" not in run_results:
        return run_results

    exclude_case_ids = set(exclude_case_ids or ())
    failures, passed_count, skipped_count, prematched_count = [], 0, 0, 0

    def walk(suites):
        nonlocal passed_count, skipped_count, prematched_count
        for suite in suites or []:
            for spec in suite.get("specs", []) or []:
                spec_case_id = (match_case_id_by_title(spec.get("title", ""), known_case_ids)
                                if exclude_case_ids else None)
                for test in spec.get("tests", []) or []:
                    results = test.get("results", []) or []
                    final_status = results[-1].get("status") if results else None
                    # Playwright records one result per ATTEMPT. A test is
                    # reported here if ANY attempt did not pass — not just if
                    # its LAST one didn't. A test that failed and then passed
                    # on retry is precisely the "known_flaky" case this agent
                    # is asked to classify, so keying off the final attempt
                    # alone would hide every flake from the classification it
                    # exists to make. `passed_on_retry` carries the
                    # distinction the model needs to tell a flake from a
                    # hard failure.
                    attempts = [r for r in results
                                if r.get("status") not in ("passed", "skipped")]
                    if not attempts:
                        if final_status == "skipped":
                            skipped_count += 1
                        else:
                            passed_count += 1
                        continue
                    # First failing attempt's message: on a retried test the
                    # later attempts are often a truncated/derived error, and
                    # the original failure is the diagnostic one.
                    if spec_case_id in exclude_case_ids:
                        prematched_count += 1
                        continue
                    first = attempts[0]
                    error = first.get("error") or {}
                    message = error.get("message") or ""
                    if not message and first.get("errors"):
                        message = (first["errors"][0] or {}).get("message", "")
                    message = _ANSI_RE.sub("", str(message))
                    failures.append({
                        "file": spec.get("file", ""),
                        "title": spec.get("title", ""),
                        "status": final_status,
                        "attempts": len(results),
                        "failed_attempts": len(attempts),
                        "passed_on_retry": final_status == "passed",
                        "error": message[:_MAX_ERROR_CHARS],
                    })
            walk(suite.get("suites"))

    with contextlib.suppress(AttributeError, TypeError):
        walk(run_results.get("suites"))

    compacted = {
        "stats": run_results.get("stats"),
        "run_errors": run_results.get("errors") or [],
        "passed_count": passed_count,
        "skipped_count": skipped_count,
        "failures": failures,
        "_note": ("Compacted from the full Playwright JSON report: passing tests are "
                  "counted, not listed, and each failure's error text is truncated to "
                  f"{_MAX_ERROR_CHARS} chars. Every non-passing test is present"
                  + (", except those already classified known_flaky." if prematched_count
                     else ".")),
    }
    if prematched_count:
        compacted["prematched_known_flaky_count"] = prematched_count
    if run_results.get("failure_kind"):
        compacted["failure_kind"] = run_results["failure_kind"]
    if run_results.get("error"):
        compacted["error"] = run_results["error"]
    return compacted


def _unchanged_paths(files_for_prompt: list) -> set:
    return {f["path"] for f in files_for_prompt if f.get("status") == "unchanged_since_last_review"}


def _flag_suspicious_unchanged_findings(review: dict, unchanged_paths: set, prior_review: dict) -> list:
    """A file marked 'unchanged_since_last_review' was NOT sent to the model
    this call (see _files_for_prompt — only a short marker was, no content).
    If this run's compliance entry for that file lists an issue that wasn't
    in the PRIOR run's compliance entry for the same file, the model
    invented a finding about content it never actually saw this call — the
    skill instructs against this (skills/reviewer.skill.md: "Do NOT invent
    compliance findings for this file"), but a skill instruction is not
    enforcement. This is the deterministic enforcement: flag it for human
    review rather than trust it silently. Not a crash/rejection — the
    Reviewer's overall output is still used as-is; this is a side-channel
    warning, same pattern as coding_agent's title-prefix warnings."""
    prior_issues_by_file = {
        c.get("file"): set(c.get("issues") or [])
        for c in (prior_review.get("compliance") or [])
    }
    suspicious = []
    for entry in review.get("compliance") or []:
        file_path = entry.get("file")
        if file_path not in unchanged_paths:
            continue
        new_issues = set(entry.get("issues") or []) - prior_issues_by_file.get(file_path, set())
        if new_issues:
            suspicious.append({"file": file_path, "unexplained_new_issues": sorted(new_issues)})
    return suspicious


#: Elements of the failure DOM shown per case. The snapshot the fixture
#: captures runs to dozens of entries; the ones that decide a diagnosis are
#: the interactive ones, and the whole point is to stay small enough to send
#: on every review.
_MAX_DOM_ELEMENTS = 40


def _failure_evidence(run_results: dict, known_case_ids, exclude_case_ids=None) -> dict:
    """What the page ACTUALLY looked like when each case failed.

    THE REVIEWER WAS DIAGNOSING BLIND. `_compact_run_results` keeps the error
    text and drops everything else, so this agent — the one asked to decide
    "product defect or test defect" — saw a timeout message and nothing about
    the page. The Self-Healer, one stage later, is handed exactly this
    evidence (`stages.stage_heal` calls the same extractor).

    The cost of that asymmetry, observed end to end: a spec asserted
    `getByText('01/01/1990')` on the Review screen and timed out. The value
    was on screen the whole time, inside an `<input>` — `getByText` matches
    text nodes, not input values. With no DOM the Reviewer could only guess,
    and guessed "the Review screen almost certainly renders the date in a
    different format", which is both wrong and confident. The Self-Healer
    then acted on that reason and widened timeouts.

    An element list is enough to settle that class of question — is the value
    on the page at all, is it in an input, is the control disabled — without
    sending the raw HTML.
    """
    try:
        by_case = _failure_dom.extract(run_results, known_case_ids) or {}
    except Exception:
        # Evidence is an enhancement to the prompt, never a precondition for
        # reviewing. A malformed report must not take the stage down.
        return {}

    evidence = {}
    for case_id, snapshot in by_case.items():
        if case_id in (exclude_case_ids or ()):
            continue  # already classified known_flaky deterministically
        elements = (snapshot.get("elements") or [])[:_MAX_DOM_ELEMENTS]
        evidence[case_id] = {
            "url": snapshot.get("url", ""),
            "title": snapshot.get("title", ""),
            "element_count": len(snapshot.get("elements") or []),
            "elements": elements,
        }
    return evidence


def _failing_case_ids(compacted: dict, known_case_ids):
    """The case ids of every failure in a compacted report, or None when that cannot
    be said for certain (not a compacted report, or a failure whose title maps to no
    known case) - the caller then sends the full, unfiltered context."""
    if not isinstance(compacted, dict) or not isinstance(compacted.get("failures"), list) \
            or compacted.get("failure_kind") or compacted.get("error"):
        return None
    ids = set()
    for failure in compacted["failures"]:
        case_id = match_case_id_by_title(str((failure or {}).get("title") or ""), known_case_ids)
        if not case_id:
            return None
        ids.add(case_id)
    return ids


def _carry_forward_compliance(review: dict, prior_review: dict, unchanged_paths: set) -> None:
    """Compliance findings for a file not re-sent this run (unchanged since the last
    review) are kept: write_json replaces review_report.json wholesale, so without
    this a finding the previous review recorded vanished on the next run."""
    current = review.get("compliance")
    if current is None:
        current = []
    if not isinstance(current, list):
        return
    have = {c.get("file") for c in current if isinstance(c, dict)}
    carried = [c for c in (prior_review.get("compliance") or [])
               if isinstance(c, dict) and c.get("file") in unchanged_paths
               and c.get("file") not in have]
    if carried:
        review["compliance"] = current + carried


def run(run_results: dict, generated_files: list, coding_standards: str,
        known_flaky_patterns: list, feature_dir: Path,
        case_index: list = None) -> Path:
    # Files are sent IN FULL on first sight of each file, not truncated to
    # 2000 chars. The skill asks this agent to check for hardcoded waits and
    # weak assertions — both live in test bodies, which a ~50-line excerpt
    # usually cuts off, so a truncated review was certifying code it had
    # never seen. On a RE-review of the same feature, a file unchanged since
    # the last review is collapsed to a short marker instead (already fully
    # checked once), and a changed file gets a diff instead of its whole
    # body — see _files_for_prompt(). .get() rather than [] — a model that
    # omits a key shouldn't KeyError inside an f-string.
    prior_files = _load_prior_files(feature_dir)
    files_for_prompt = _files_for_prompt(generated_files, prior_files)

    # Deterministic pre-match against known_flaky.json — a failure that's an
    # exact repeat of a pattern already recorded doesn't need the LLM to
    # re-diagnose it; only genuinely new/ambiguous failures are left for the
    # model to classify. No-op (prematched == []) when case_index is empty
    # or nothing matches, so this can only shrink the model's job, never
    # change behavior for a feature with no repeat flakes.
    known_case_ids = {c.get("test_case_id") for c in (case_index or []) if c.get("test_case_id")}
    failures = _extract_failures(run_results, known_case_ids)
    prematched, _ = _prematch_known_flaky(failures, known_flaky_patterns)
    prematched_ids = {p["test_case_id"] for p in prematched}

    # The page as it stood at the moment of failure, per case. See
    # _failure_evidence: without this the agent classifies from an error
    # string alone and fills the gap with plausible invention. Pre-matched
    # cases are left out: the model is told not to classify them.
    evidence = _failure_evidence(run_results, known_case_ids, prematched_ids)

    compacted = _compact_run_results(run_results, prematched_ids, known_case_ids)

    # Only failing cases matter for classification, so only their rows of the case
    # index and their known_flaky entries are sent - unless some failure cannot be
    # tied to a case id, when the model needs the whole index to map it.
    failing_ids = _failing_case_ids(compacted, known_case_ids)
    case_rows = list(case_index or [])
    flaky_for_prompt = list(known_flaky_patterns or [])
    if failing_ids is not None:
        case_rows = [c for c in case_rows if c.get("test_case_id") in failing_ids]
        flaky_for_prompt = [k for k in flaky_for_prompt
                            if not k.get("test_case_id") or k.get("test_case_id") in failing_ids]

    user_prompt = f"""Run results (compacted — see the "_note" field):
{_compact_json(compacted)}

DOM captured AT THE MOMENT OF FAILURE, per test case. This is evidence, not
narrative: if a value the test looked for is present here inside an input, or
a control it clicked is disabled, say so and cite it. If a case is absent from
this block there was no snapshot for it — say the evidence is missing rather
than inventing a cause:
{_compact_json(evidence)}

Generated files (full content for new files, a diff for changed files, a short
marker for files unchanged since the last review of this feature):
{_compact_json(files_for_prompt)}

Known test cases (the failing ones only) — use these exact test_case_id values in your output, do not invent new ones:
{_compact_json(case_rows)}

Produce the review now."""

    extra_system = (
        f"Coding standards:\n{coding_standards or '(none — use general Playwright best practices)'}\n\n"
        f"Known flaky patterns (entries for the failing cases):\n{_compact_json(flaky_for_prompt)}"
    )
    if prematched_ids:
        extra_system += (
            f"\n\nThese test_case_id(s) already have a CONFIRMED known_flaky classification "
            f"from a deterministic match against known_flaky_patterns this run — do NOT include "
            f"them in your 'failure_classification' output, they are merged in automatically: "
            f"{sorted(prematched_ids)}"
        )

    raw = call_agent(
        agent_name="reviewer",
        user_prompt=user_prompt,
        feature_dir=feature_dir,
        max_tokens=8000,
        extra_system=extra_system,
    )

    review = parse_json_response(raw, feature_dir, "reviewer")
    if not isinstance(review, dict):
        raise ValueError(
            f"reviewer: expected a JSON object with 'compliance'/"
            f"'failure_classification', got {type(review).__name__}."
        )

    # Merge the deterministic pre-matches in, deduped by test_case_id in
    # case the model included one of them anyway despite the instruction above.
    existing_ids = {c.get("test_case_id") for c in review.get("failure_classification", [])}
    review["failure_classification"] = review.get("failure_classification", []) + [
        p for p in prematched if p["test_case_id"] not in existing_ids
    ]

    # Self-consistency check: did the model invent a NEW compliance finding
    # for a file it was only shown a "already reviewed, content omitted"
    # marker for this call? See _flag_suspicious_unchanged_findings.
    out_path = artifact_path(feature_dir, "review_report.json")
    prior_review = read_json(out_path, default={})
    suspicious = _flag_suspicious_unchanged_findings(review, _unchanged_paths(files_for_prompt), prior_review)
    _carry_forward_compliance(review, prior_review, _unchanged_paths(files_for_prompt))
    if suspicious:
        write_json(artifact_path(feature_dir, "reviewer_suspicious_findings.json"), suspicious)
        print(f"⚠️  {len(suspicious)} file(s) got a NEW compliance finding despite being marked "
              f"unchanged since the last review — see {artifact_path(feature_dir, 'reviewer_suspicious_findings.json')} "
              f"(the model may be inventing findings for content it wasn't actually shown this call).")

    write_json(out_path, review)
    _save_prior_files(feature_dir, generated_files)

    classifications = [c.get("classification") for c in review.get("failure_classification", [])]
    append_context_summary(
        feature_dir, "reviewer",
        f"Reviewed {len(generated_files)} file(s). "
        f"Failure breakdown: {dict((c, classifications.count(c)) for c in set(classifications))}."
    )

    return out_path


def _cli():
    """Standalone entry point — review a run's results + generated files
    without going through the rest of the pipeline. Generated files are
    read straight off disk via --repo-dir + --file-glob, since coding_agent
    doesn't persist its own file list separately from what it wrote."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-results", required=True, help="Path to run_results.json.")
    parser.add_argument("--repo-dir", required=True, help="Repo containing the generated files.")
    parser.add_argument("--file-glob", default="**/*.spec.ts",
                         help="Glob (relative to --repo-dir) selecting generated files to review.")
    parser.add_argument("--conventions-file", default="")
    parser.add_argument("--known-flaky-file", default="")
    parser.add_argument("--case-index-file", default="")
    parser.add_argument("--feature-dir", required=True)
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)
    repo_dir = Path(args.repo_dir)

    run_results = read_json(args.run_results, default={})
    # sorted() so a review's file order is reproducible across machines
    # (glob order is filesystem-dependent), and is_file() so a directory
    # matching the glob can't reach read_text().
    generated_files = [
        {"path": str(p.relative_to(repo_dir)).replace("\\", "/"),
         "content": read_text_file(p)}
        for p in sorted(repo_dir.glob(args.file_glob)) if p.is_file()
    ]
    coding_standards = read_text_file(args.conventions_file)
    known_flaky = read_json(args.known_flaky_file, default=[])
    case_index = read_json(args.case_index_file, default=[])

    out_path = run(run_results, generated_files, coding_standards, known_flaky,
                    feature_dir, case_index=case_index)
    print(f"\n✅ Review report written: {out_path}")


if __name__ == "__main__":
    _cli()
