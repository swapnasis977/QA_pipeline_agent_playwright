"""
Agent 7: Bug Reporter
IN:  Reviewer's failure classification (review_report.json), the Executor's
     Playwright JSON report (run_results), the repo's test-results/ screenshots
IN:  Which cases the Self-Healer could NOT fix (still_failing_ids) — a
     "fixable_test_issue" the healer already resolved is not a bug
OUT: BUG_LIST.md — one entry per genuine defect, with steps-to-reproduce and
     a copied screenshot, plus a bug_screenshots/ folder next to it

Deliberately NOT an LLM agent. Everything here — which cases qualify, what
the repro steps are, where the screenshot lives — is already fully
determined by the Reviewer's classification + the test's own title/screenshot
path, so writing it is a templating problem, not a reasoning one. This also
means bug_reporter.run() works with NO API key/provider configured at all,
unlike every other agent in this pipeline (see utils/llm_client.py)
— it's pure file I/O.

Modeled directly on the manually-written BUG_LIST.md produced in a live
session against the Face Comparison Tool (PROJ-68308/68312/68316/68479):
one bug per genuine_bug/unhealed-fixable_test_issue case, with a real
screenshot and steps derived from the test name, not invented prose.
"""

import hashlib
import re
import shutil
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import read_json, write_json, write_text_file
from utils.run_layout import artifact_path

#: Persists a stable bug number per (test_case_id, reason) across runs, so a
#: defect that's still open on run 3 keeps being called "BUG-01" instead of
#: getting renumbered from scratch every time BUG_LIST.md regenerates — that
#: renumbering is what made it impossible to tell "still the same bug" from
#: "a new one" across runs.
_REGISTRY_FILENAME = "bug_registry.json"

#: Lower number = more severe = sorted first. Missing/unrecognized severity
#: (an older review_report.json predating the "severity" field, or a model
#: that omitted it) sorts after every recognized tier rather than crashing.
_SEVERITY_ORDER = {"blocker": 0, "major": 1, "minor": 2, "n/a": 3}

#: Written by the self-heal stage when the model declines — {test_case_id: reason}
#: for cases where the Self-Healer explicitly declined to patch (see
#: skills/self_healer.skill.md's unresolved_reason convention) rather than
#: simply running out of heal attempts. Optional: absent entirely on runs
#: with no declines, or predating this file.
_SELF_HEAL_DECLINED_FILENAME = "self_heal_declined.json"


def _signature(bug: dict) -> str:
    """What the failure IS, stable across runs: the quoted error, not the
    Reviewer's prose. The reason is free text that differs on every run, so
    keying on it made "still open" almost never fire and issued a new BUG
    number each time. Digits (ids, timings, counts) are normalized away."""
    # Playwright's own first error line wins: it comes from the tool, so it is the
    # same text every run. The Reviewer's citation/reason is model prose that
    # differs run to run, which gave the same bug a new number each time.
    text = str(bug.get("_error_line") or bug.get("citation") or bug.get("reason") or "")
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)          # terminal colour codes
    text = re.sub(r"\d+", "#", text.lower())
    return re.sub(r"\s+", " ", text).strip()[:200]


#: Bump when `_signature` changes meaning, so old registry entries (keyed by the
#: previous scheme) are retired instead of being reported as "fixed".
_REGISTRY_VERSION = "2"


def _error_headlines(run_results: dict, case_ids) -> dict:
    """{case id: first line of Playwright's error for that case's last result}."""
    wanted = [c for c in case_ids if c]
    found = {}

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs") or []:
                case_id = _match_case_id_by_title(str(spec.get("title") or ""), wanted)
                if not case_id:
                    continue
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    error = (results[-1].get("error") or {}) if results else {}
                    message = str(error.get("message") or "") if isinstance(error, dict) else ""
                    lines = [ln.strip() for ln in message.splitlines() if ln.strip()]
                    if lines:
                        found[case_id] = lines[0][:200]
            walk(suite.get("suites"))

    walk((run_results or {}).get("suites"))
    return found


def _bug_key(bug: dict) -> str:
    return (f"{bug.get('test_case_id', '?')}::"
            f"{hashlib.sha256(_signature(bug).encode()).hexdigest()[:16]}")


def _load_registry(feature_dir: Path) -> dict:
    # A corrupt registry degrades to "every bug looks new" (renumbered from
    # 1) rather than crashing the final reporting stage of a whole run.
    return read_json(artifact_path(feature_dir, _REGISTRY_FILENAME), default={})


def _assign_bug_numbers(bugs: list, feature_dir: Path) -> dict:
    """Returns {bug_key: (bug_no, is_recurring)} — is_recurring means this
    exact (test_case_id, reason) pair already had a number from a previous
    run, so BUG_LIST.md can call it out as still-open rather than new."""
    registry = _load_registry(feature_dir)
    if registry and registry.get("_version") != _REGISTRY_VERSION:
        # Keyed by an older signature scheme: those keys can never match again, and
        # leaving them would report every old bug as "fixed". Retire them but keep the
        # numbering moving forward so a number is never reused for a different bug.
        floor = max((v for k, v in registry.items()
                     if isinstance(v, int) and not str(k).startswith("_")), default=0)
        registry = {"_floor": floor}
    registry["_version"] = _REGISTRY_VERSION
    # A hand-edited registry can hold a non-int value; max() over mixed
    # types raises TypeError, which would abort the report.
    next_no = max((v for k, v in registry.items()
                   if isinstance(v, int) and not isinstance(v, bool)), default=0) + 1

    assignment = {}
    for bug in bugs:
        key = _bug_key(bug)
        if key in registry:
            assignment[key] = (registry[key], True)
        else:
            registry[key] = next_no
            assignment[key] = (next_no, False)
            next_no += 1

    write_json(artifact_path(feature_dir, _REGISTRY_FILENAME), registry)
    return assignment


def _fixed_entries(feature_dir: Path, assignment: dict, executed_case_ids=None) -> list:
    """[(bug number, case id, registry key)] reported in an earlier run and absent from this one.

    `executed_case_ids`, when given, is the set of cases that RAN and PASSED this run: a
    bug is only called fixed if its case is in it. A `--grep` subset, or an executor that
    yielded nothing, must not report every bug it did not even try as fixed. None keeps
    the older behaviour (every absent bug) for callers that cannot say what ran.
    """
    registry = _load_registry(feature_dir)
    if registry and registry.get("_version") != _REGISTRY_VERSION:
        return []     # keyed by an older signature scheme: never comparable, see _assign_bug_numbers
    entries = sorted(
        (no, str(key).split("::")[0], key) for key, no in registry.items()
        if not str(key).startswith("_") and isinstance(no, int) and key not in assignment)
    if executed_case_ids is not None:
        entries = [e for e in entries if e[1] in executed_case_ids]
    return entries


def _fixed_bugs(feature_dir: Path, assignment: dict, executed_case_ids=None) -> list:
    """[(bug number, case id)] reported in an earlier run and absent from this one."""
    return [(no, case) for no, case, _ in _fixed_entries(feature_dir, assignment, executed_case_ids)]


def _retire_fixed(feature_dir: Path, entries: list) -> None:
    """Drops bugs just reported as fixed from the registry so "No longer failing" is said
    once, not on every later run. Their numbers are never reused: the highest one is kept
    as a floor, which _assign_bug_numbers counts when it picks the next number."""
    if not entries:
        return
    registry = _load_registry(feature_dir)
    numbers = [v for k, v in registry.items()
               if isinstance(v, int) and not isinstance(v, bool)]
    registry["_floor"] = max(numbers, default=0)
    for _no, _case, key in entries:
        registry.pop(key, None)
    write_json(artifact_path(feature_dir, _REGISTRY_FILENAME), registry)


def _fixed_section(fixed: list) -> list:
    if not fixed:
        return []
    return ["", "---", "", "## No longer failing", "",
            "Reported in an earlier run, absent from this one: "
            + ", ".join(f"BUG-{no:02d} ({case})" for no, case in fixed)
            + ". Confirm the fix before closing the ticket."]


def run(review_path: Path, run_results: dict, feature_dir: Path,
        still_failing_ids: set = None, case_index: list = None,
        repo_output_dir: Path = None, executed_case_ids: set = None) -> Path:
    """`repo_output_dir` is accepted only for backward compatibility with
    existing keyword callers and is unused: every path this agent reads comes
    from the Playwright report's own absolute attachment paths, and
    everything it writes goes under feature_dir. It is deliberately last and
    optional so it can be dropped once callers stop passing it.

    `executed_case_ids` is the set of case ids that RAN this run and did not fail. When
    given, only a registered bug whose case is in it is reported as "No longer failing";
    when None (a caller that cannot say), every registered bug absent from this run is."""
    review = read_json(review_path, default={})
    classifications = review.get("failure_classification", [])
    still_failing_ids = still_failing_ids or set()

    # A "fixable_test_issue" that the Self-Healer already confirmed fixed
    # (i.e. NOT in still_failing_ids) is exactly what it sounds like — a test
    # script problem that's been resolved. Only "genuine_bug" cases, and
    # "fixable_test_issue" cases the healer gave up on, are real findings.
    def verdict_of(entry):
        # The heal stage branches on the normalized `verdict`; this read the raw
        # `classification`, so a case healed as "fixable" could be dropped from
        # the list here. Anything that is not one of the two benign verdicts is
        # a finding.
        value = str(entry.get("verdict") or entry.get("classification") or "").strip()
        return value if value in ("known_flaky", "fixable_test_issue") else "genuine_bug"

    bugs = [
        c for c in classifications
        if verdict_of(c) == "genuine_bug"
        or (verdict_of(c) == "fixable_test_issue"
            and c.get("test_case_id") in still_failing_ids)
    ]
    known_flaky = [c for c in classifications if verdict_of(c) == "known_flaky"]

    # Blockers first, then major, then minor/unspecified — a human triaging
    # this file top-to-bottom sees the worst defects immediately instead of
    # in whatever order the Reviewer happened to list them.
    bugs.sort(key=lambda b: _SEVERITY_ORDER.get((b.get("severity") or "n/a").lower(), 3))

    declined_path = artifact_path(feature_dir, _SELF_HEAL_DECLINED_FILENAME)
    declined_reasons = read_json(declined_path, default={})

    screenshots_dir = artifact_path(feature_dir, "bug_screenshots")
    out_path = artifact_path(feature_dir, "BUG_LIST.md")

    if not bugs:
        fixed_entries = _fixed_entries(feature_dir, {}, executed_case_ids)
        write_text_file(
            out_path,
            "# Bug List\n\nNo genuine bugs or unresolved test issues found in this run.\n"
            + (f"\n{len(known_flaky)} known-flaky failure(s) were seen and ignored "
               f"(already tracked in known_flaky.json).\n" if known_flaky else "")
            + "\n".join(_fixed_section([(no, case) for no, case, _ in fixed_entries])) + "\n"
        )
        _retire_fixed(feature_dir, fixed_entries)
        return out_path

    screenshots_dir.mkdir(parents=True, exist_ok=True)
    file_by_case_id = {
        entry["test_case_id"]: _norm_path(entry["target_file"])
        for entry in (case_index or [])
        if entry.get("test_case_id") and entry.get("target_file")
    }
    attachments_by_case = _index_screenshots_by_case(
        run_results, [b.get("test_case_id") for b in bugs], file_by_case_id)
    headlines = _error_headlines(run_results, [b.get("test_case_id") for b in bugs])
    for bug in bugs:
        bug["_error_line"] = headlines.get(bug.get("test_case_id"), "")
    bug_numbers = _assign_bug_numbers(bugs, feature_dir)
    fixed_entries = _fixed_entries(feature_dir, bug_numbers, executed_case_ids)
    fixed_since_last_run = [(no, case) for no, case, _ in fixed_entries]

    lines = [
        "# Bug List",
        "",
        f"{len(bugs)} finding(s) from this run "
        f"({sum(1 for b in bugs if b.get('classification') == 'genuine_bug')} genuine_bug, "
        f"{sum(1 for b in bugs if b.get('classification') == 'fixable_test_issue')} unresolved after self-healing).",
        "",
        "---",
        "",
    ]

    for bug in bugs:
        case_id = bug.get("test_case_id", "?")
        reason = bug.get("reason", "(no reason given)")
        suggested_fix = bug.get("suggested_fix", "")
        file_path = bug.get("file", "")
        classification = bug.get("classification", "?")
        severity = bug.get("severity") or "n/a"
        bug_no, is_recurring = bug_numbers[_bug_key(bug)]

        lines.append(f"## BUG-{bug_no:02d} — {case_id}"
                      + (" (still open — first reported in a previous run)" if is_recurring else " (new)"))
        lines.append("")
        lines.append(f"**Classification:** {classification}")
        if classification == "genuine_bug":
            lines.append(f"**Severity:** {severity}")
        lines.append(f"**File:** `{file_path}`" if file_path else "**File:** (not identified)")
        lines.append("")
        lines.append(f"**What went wrong:** {reason}")
        if suggested_fix:
            lines.append("")
            lines.append(f"**Reviewer's suggested fix:** {suggested_fix}")

        if case_id in declined_reasons:
            lines.append("")
            lines.append(f"**Self-healer declined to auto-fix:** {declined_reasons[case_id]}")

        lines.append("")
        lines.append("**Steps to reproduce:** run the test named below and observe the failure —")
        lines.append(f"- Test Case ID: `{case_id}`")
        # The case index's own target file is repo-relative; the model's `file` is often
        # relative to testDir and would not resolve. `(?![\w-])` anchors the id so
        # "...-TC-1" does not also select "...-TC-10".
        repro_file = file_by_case_id.get(case_id) or file_path
        if repro_file:
            lines.append(f"- `npx playwright test {repro_file} -g \"{case_id}(?![\\w-])\"`")

        screenshot_src = attachments_by_case.get(case_id)
        copied_name = None
        if screenshot_src and Path(screenshot_src).is_file():
            dest_name = f"BUG-{bug_no:02d}-{_slugify(case_id)}.png"
            try:
                shutil.copy(screenshot_src, screenshots_dir / dest_name)
                copied_name = dest_name
            except OSError as exc:
                # This is the LAST stage of the run, and every finding above
                # is already formatted. An unreadable/locked screenshot (a
                # cleaned test-results dir, an AV lock, a full disk) must
                # degrade this one entry to "no screenshot" rather than raise
                # and throw away the whole BUG_LIST.md.
                print(f"⚠️  Could not copy {case_id}'s screenshot ({exc}) — "
                      f"BUG-{bug_no:02d} will be reported without one.")

        if copied_name:
            lines.append("")
            lines.append(f"**Screenshot:** `bug_screenshots/{copied_name}`")
            lines.append(f"![{case_id}](bug_screenshots/{copied_name})")
        else:
            lines.append("")
            lines.append("**Screenshot:** none captured (test may have failed before a screenshot point, "
                          "or Playwright's screenshot-on-failure setting is off).")

        lines.append("")
        lines.append("---")
        lines.append("")

    if known_flaky:
        lines.append("## Known-flaky failures (not listed as bugs)")
        lines.append("")
        lines.append(f"{len(known_flaky)} failure(s) matched an existing known_flaky.json pattern "
                      "and were excluded above:")
        for kf in known_flaky:
            lines.append(f"- `{kf.get('test_case_id', '?')}` — {kf.get('reason', '')}")
        lines.append("")

    # utf-8 explicitly: these lines carry em dashes and the Reviewer's own
    # prose, which the Windows locale codepage cannot always encode.
    lines.extend(_fixed_section(fixed_since_last_run))
    write_text_file(out_path, "\n".join(lines))
    _retire_fixed(feature_dir, fixed_entries)
    return out_path


def _slugify(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", text).strip("-") or "case"


def _norm_path(p) -> str:
    """Same normalization the orchestrator applies to a path (backslashes to
    forward slashes, leading './' stripped) — small, deliberate duplication
    rather than a cross-layer import from an agent module, matching this
    codebase's existing tolerance for a handful of independently-owned copies
    of trivial helpers."""
    return re.sub(r"^(\./)+", "", str(p or "").replace("\\", "/"))


def _match_case_id_by_title(title: str, wanted) -> str:
    """Longest-first, deterministic version of the title-prefix match (same
    logic as reviewer.match_case_id_by_title — duplicated here for the same
    reason as _norm_path above: this module must stay importable with no
    AI_PROVIDER/LLM dependency at all, and reviewer.py pulls in llm_client).

    Longest-first matters: with positional ids, "PROJ-1-TC-1" is a prefix of
    "PROJ-1-TC-10", so a plain `next(...)` over a SET attached BUG-01's
    screenshot to whichever id the set happened to yield first — a different
    one from run to run.
    """
    candidates = sorted((cid for cid in wanted or () if cid),
                        key=lambda cid: (-len(cid), cid))
    return next((cid for cid in candidates if title.startswith(cid)), None)


def _index_screenshots_by_case(run_results: dict, case_ids: list, file_by_case_id: dict = None) -> dict:
    """Walks the Playwright JSON reporter's suite tree (arbitrarily nested —
    file > describe blocks > specs > tests > results > attachments) looking
    for a failed result's screenshot attachment, matched to a case id.

    PRIMARY match: the coding_agent skill's own convention — every test
    title starts with its Test Case ID verbatim (skills/coding_agent.skill.md).

    FALLBACK match (file_by_case_id, from case_index.json): if a spec's
    title doesn't start with ANY wanted case_id (convention drift — see
    coding_agent._missing_title_prefixes, which now detects and logs
    exactly this at generation time), but its file matches the ONE wanted
    case_id's known target_file, that's still a confident match — refuses
    to guess when more than one wanted case_id shares that file, since
    picking the wrong one's screenshot is worse than showing none.

    Returns the LAST (most recent) screenshot path found per case id.
    """
    wanted = set(c for c in case_ids if c)
    file_by_case_id = file_by_case_id or {}
    case_ids_by_file = {}
    for cid, file_path in file_by_case_id.items():
        if cid in wanted:
            case_ids_by_file.setdefault(file_path, []).append(cid)

    found = {}

    def walk_suites(suites):
        for suite in suites or []:
            for spec in suite.get("specs", []) or []:
                title = spec.get("title", "")
                case_id = _match_case_id_by_title(title, wanted)
                if not case_id:
                    candidates = case_ids_by_file.get(_norm_path(spec.get("file")))
                    if candidates and len(candidates) == 1:
                        case_id = candidates[0]
                if case_id:
                    for test in spec.get("tests", []) or []:
                        for result in test.get("results", []) or []:
                            # Only a FAILED attempt's screenshot evidences a
                            # bug. Playwright records one result per attempt,
                            # and `screenshot: 'on'` (or a test that captures
                            # deliberately) attaches one to passing attempts
                            # too — so on a case that failed then PASSED on
                            # retry, the unfiltered scan took the passing
                            # attempt's screenshot (it comes last) and
                            # attached a green screen to the bug report.
                            if result.get("status") in ("passed", "skipped"):
                                continue
                            for att in result.get("attachments", []) or []:
                                if att.get("name") == "screenshot" and att.get("path"):
                                    found[case_id] = att["path"]
            walk_suites(suite.get("suites"))

    walk_suites(run_results.get("suites"))
    return found


def _cli():
    """Standalone entry point — generate BUG_LIST.md straight from a
    review report + run results, without going through the rest of the
    pipeline. Needs no LLM/AI_PROVIDER key at all (see module docstring)."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-path", required=True, help="Path to review_report.json.")
    parser.add_argument("--run-results", required=True, help="Path to run_results.json.")
    parser.add_argument("--repo-dir", default="",
                         help="Accepted for backward compatibility and unused — screenshot "
                              "paths come from the Playwright report itself, and all output "
                              "goes under --feature-dir (see run()).")
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--still-failing", nargs="*", default=[],
                         help="Test Case IDs the Self-Healer could not fix (or all, if skipping self-heal).")
    parser.add_argument("--case-index-file", default="",
                         help="Path to case_index.json — enables the file-based screenshot-"
                              "matching fallback when a test title doesn't start with its Test "
                              "Case ID (see _index_screenshots_by_case).")
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)
    run_results = read_json(args.run_results, default={})
    case_index = read_json(args.case_index_file, default=[])

    out_path = run(
        review_path=Path(args.review_path), run_results=run_results,
        feature_dir=feature_dir, still_failing_ids=set(args.still_failing),
        case_index=case_index,
    )
    print(f"\n✅ Bug list written: {out_path}")


if __name__ == "__main__":
    _cli()
