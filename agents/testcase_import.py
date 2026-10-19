"""Importing test cases that already exist (a sheet, a CSV or JIRA Test issues) instead of generating new ones, and using them as a coverage baseline.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""

from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agents.artifact_io import write_json
from agents.testcase_limits import _MAX_PARALLEL_STORIES
from agents.testcase_sheet import _read_existing_test_case_file, _write_excel
from utils.error_classify import classify_error_kind
from utils.llm_client import append_context_summary, call_agent, parse_json_response
from utils.run_layout import artifact_path

#: Maximum existing test-case rows normalized per LLM call — same
#: truncation-avoidance reasoning as coding_agent._MAX_CASES_PER_CALL: a
#: full JSON test-case object per row (numbered steps, expected results)
#: adds up fast, and a large legacy suite can be hundreds of rows.
_MAX_ROWS_PER_IMPORT_CALL = 15


def fetch_test_cases_from_jira(cycle_or_jql: str) -> list:
    """Pulls existing manual test cases already authored in JIRA (Zephyr
    Scale) into the same raw-row shape `_read_existing_test_case_file`
    produces — a flat dict of column-name -> value — so they can flow into
    the SAME normalization path `import_existing_test_cases` already uses
    for a file-based import, unchanged.

    `cycle_or_jql` is resolved to a list of Zephyr test case keys in two
    steps:
      1. Treated as a Zephyr test cycle id and passed to
         `coding_agent.fetch_test_cycle_keys` (the same call the Coding
         Agent already makes to filter by cycle).
      2. If that comes back empty (not a real cycle, no token, or a cycle
         with nothing in it), falls back to treating `cycle_or_jql` as a
         comma-separated list of explicit test case keys (e.g.
         "PROJ-T101,PROJ-T102") — a simple fallback with no JQL search
         involved, good enough for pulling a known handful of cases without
         needing a real test cycle set up first.

    Requires ZEPHYR_API_TOKEN (see .env.example) — same token
    `coding_agent.fetch_test_cycle_keys` uses. Returns [] with a clear
    warning (never raises) if the token is missing, since there's no
    sensible placeholder for "existing test case content" the way
    `fetch_jira_story` has one for a story. A single case's fetch failure
    is skipped with a warning rather than aborting the whole import.
    """
    import os

    import requests

    from agents.coding_cases import fetch_test_cycle_keys

    token = os.environ.get("ZEPHYR_API_TOKEN", "")
    if not token:
        print("⚠️  ZEPHYR_API_TOKEN missing — cannot import test cases from JIRA/Zephyr Scale. "
              "Set it in .env (see .env.example) for --import-from-jira to work.")
        return []

    case_keys = fetch_test_cycle_keys(cycle_or_jql)
    if not case_keys:
        # Not a real/reachable test cycle, or the cycle has nothing in it —
        # fall back to treating the input as explicit comma-separated keys.
        case_keys = [k.strip() for k in cycle_or_jql.split(",") if k.strip()]
    if not case_keys:
        print(f"⚠️  Could not resolve any test case keys from '{cycle_or_jql}' — "
              "nothing to import.")
        return []

    headers = {"Authorization": f"Bearer {token}"}
    raw_rows = []
    for key in case_keys:
        try:
            case_resp = requests.get(
                f"https://api.zephyrscale.smartbear.com/v2/testcases/{key}",
                headers=headers, timeout=15,
            )
            case_resp.raise_for_status()
            case_data = case_resp.json()

            steps_resp = requests.get(
                f"https://api.zephyrscale.smartbear.com/v2/testcases/{key}/teststeps",
                headers=headers, timeout=15,
            )
            steps_resp.raise_for_status()
            steps_data = steps_resp.json().get("values", [])
        except requests.RequestException as exc:
            print(f"⚠️  Could not fetch Zephyr Scale test case '{key}' ({exc}) — skipping it.")
            continue

        step_texts, result_texts = [], []
        for i, step in enumerate(steps_data, start=1):
            inline = step.get("inline", {}) if isinstance(step, dict) else {}
            description = inline.get("description", "")
            test_data = inline.get("testData", "")
            expected = inline.get("expectedResult", "")
            step_text = description
            if test_data:
                step_text = f"{step_text} (Test Data: {test_data})" if step_text else f"Test Data: {test_data}"
            if step_text:
                step_texts.append(f"{i}-{step_text}")
            if expected:
                result_texts.append(f"{i}-{expected}")

        raw_rows.append({
            "Test Case Key": key,
            "Name": case_data.get("name", ""),
            "Description": case_data.get("objective", ""),
            "Precondition": case_data.get("precondition", ""),
            "Steps": "\n\n".join(step_texts),
            "Expected Results": "\n\n".join(result_texts),
        })

    if not raw_rows:
        print(f"⚠️  None of the {len(case_keys)} resolved test case key(s) could be fetched "
              "from Zephyr Scale — nothing to import.")
    return raw_rows


#: Column-name patterns identifying the TEST CASE a step-row belongs to, in
#: priority order. These are the names real exports use (JIRA/Zephyr/Xray all
#: export one row per STEP, repeating the case's key down the rows).
_CASE_KEY_COLUMN_RE = re.compile(
    r"^(issue\s*key|test\s*case\s*(id|key)?|case\s*(id|key)|key|ticket|story|"
    r"requirement|test\s*id)$", re.I)


#: Columns whose values are PER-STEP and must be merged into a numbered list.
_STEP_COLUMN_RE = re.compile(r"^(step|steps|test\s*step|action|description)s?$", re.I)


_EXPECTED_COLUMN_RE = re.compile(r"^(expected\s*(result|results|outcome)?|result)$", re.I)


_STEP_DATA_COLUMN_RE = re.compile(r"^(test\s*data|data|input)$", re.I)


def _detect_step_row_layout(rows: list) -> tuple:
    """Detects the "one row per STEP" export shape and returns
    (case_key_column, step_columns) — or (None, ()) for the one-row-per-case
    layout the importer already assumed.

    WHY THIS EXISTS. Every test management tool this pipeline imports from
    (JIRA, Zephyr, Xray) exports test STEPS as rows, repeating the owning
    case's key down each row — a 5-case suite arrives as 40 rows. The
    importer sent each row to the model as a whole test case, and
    _IMPORT_MODE_INSTRUCTIONS explicitly forbids merging rows ("never merge
    two source rows into one"), so a real export normalized into 40
    single-step "test cases": each with one action, one assertion, no flow,
    and a Test Case ID that matches nothing a human would recognize. Then
    the review gate asked a human to read and approve all 40.

    Detection requires BOTH signals, so a genuine one-row-per-case sheet
    that merely repeats a value in some column is never grouped:
      1. a case-key column whose values actually REPEAT, and
      2. a per-step column (Step/Action/Expected Result) present.
    """
    if not rows:
        return None, ()

    headers = list(rows[0].keys())
    step_columns = tuple(
        h for h in headers
        if h and (_STEP_COLUMN_RE.match(str(h).strip())
                  or _EXPECTED_COLUMN_RE.match(str(h).strip())
                  or _STEP_DATA_COLUMN_RE.match(str(h).strip())))
    if not step_columns:
        return None, ()

    for header in headers:
        if not header or not _CASE_KEY_COLUMN_RE.match(str(header).strip()):
            continue
        values = [str(r.get(header) or "").strip() for r in rows]
        present = [v for v in values if v]
        if not present:
            continue

        # Two independent signals of a step-per-row layout:
        #   REPEATED key  — the common shape, key restated on every step row.
        #   BLANK key after a populated one — the other shape these exporters
        #     produce, where a continuation step leaves the key empty. Without
        #     this second signal a case whose only continuation rows are blank
        #     was never detected (one populated key means no repeat to find),
        #     so a 2-step case stayed split into two "test cases".
        repeated = len(set(present)) < len(present)
        blank_continuation = any(
            not value for value in values[values.index(present[0]) + 1:])
        if repeated or blank_continuation:
            return header, step_columns

    return None, ()


def _group_step_rows(rows: list) -> list:
    """Collapses a step-per-row export into one row per test case, merging
    the per-step columns into this team's numbered style ("1-...", "2-...").

    Deterministic and LLM-free: the grouping is a mechanical fact of the
    source layout, not a judgment call, so paying a model to infer it would
    add both cost and a chance of getting it wrong. Order is preserved —
    both the order cases first appear and the order of steps within a case.

    Non-step columns (Project, Component, Priority) are carried over from
    the first row of each group; where later rows disagree, the distinct
    values are joined so nothing is silently dropped.
    """
    case_key_column, step_columns = _detect_step_row_layout(rows)
    if not case_key_column:
        return rows

    groups, order = {}, []
    for row in rows:
        key = str(row.get(case_key_column) or "").strip()
        if not key:
            # A step-row with no key belongs to the case above it — that is
            # how these exports represent a continuation row.
            key = order[-1] if order else "(unkeyed)"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    merged_rows = []
    for key in order:
        member_rows = groups[key]
        merged = {case_key_column: key}

        for column in member_rows[0]:
            if column == case_key_column:
                continue
            values = [str(r.get(column) or "").strip() for r in member_rows]
            populated = [v for v in values if v]
            if not populated:
                merged[column] = ""
            elif column in step_columns:
                # Numbered to match the house style, and numbered by STEP
                # POSITION so Step 3 and Expected Result 3 line up even when
                # one of them is blank for some steps.
                numbered = [f"{i}-{v}" for i, v in enumerate(values, start=1) if v]
                merged[column] = "\n\n".join(numbered)
            else:
                distinct = list(dict.fromkeys(populated))
                merged[column] = distinct[0] if len(distinct) == 1 else " / ".join(distinct)
        merged["_source_step_count"] = len(member_rows)
        merged_rows.append(merged)

    print(f"      Detected a step-per-row export: {len(rows)} row(s) grouped by "
          f"'{case_key_column}' into {len(merged_rows)} test case(s) "
          f"({', '.join(f'{k}:{len(groups[k])} steps' for k in order)}).")
    return merged_rows


#: How many existing cases to show as a coverage baseline. A baseline is a
#: LIST OF TITLES, not full cases: the model only has to decide "is this
#: scenario already covered?", and the steps/data of an old case do not help
#: with that while costing ~40x the tokens. 400 titles is far past any real
#: story's regression set and still small enough to sit inside the cached
#: prefix rather than displacing the story itself.
_MAX_BASELINE_TITLES = 400


def render_reference_baseline(cases: list) -> str:
    """Renders existing test cases as a DO-NOT-DUPLICATE coverage baseline.

    The counterpart of `_IMPORT_MODE_INSTRUCTIONS`: import REPLACES
    generation, this one INFORMS it. Both are passed via `extra_system` for
    the same reason — the skill file's cached instructions stay untouched, and
    this layers on top of the schema and style rules it already defines.

    Deliberately titles-only. The failure this guards against is a model given
    forty full existing cases and a story, which then rewrites the existing
    forty instead of writing what is missing: the more of an old case it can
    see, the more it treats reproducing it as the task. A bare title list
    supports the one judgement being asked for and supports no other.
    """
    titles = []
    for case in cases or []:
        title = str(case.get("Name") or case.get("name") or "").strip()
        if title:
            case_id = str(case.get("Test Case ID") or case.get("test_case_id") or "").strip()
            titles.append(f"- {title}" + (f"  [{case_id}]" if case_id else ""))
    if not titles:
        return ""

    shown = titles[:_MAX_BASELINE_TITLES]
    # An elided tail is stated, never silent. A model told "this is the
    # existing coverage" while shown two thirds of it will confidently
    # report a gap that is not a gap.
    omitted = len(titles) - len(shown)
    tail = (f"\n\n(Plus {omitted} further existing case(s) not listed here. "
            f"Treat the covered ground as BROADER than this list, not narrower — "
            f"if unsure whether something is already covered, say so in the case "
            f"description rather than assuming it is not.)" if omitted else "")

    return (
        "EXISTING TEST COVERAGE — DO NOT DUPLICATE.\n\n"
        "These test cases ALREADY EXIST for this application. They are given "
        "as a coverage baseline, NOT as content to reproduce, reformat or "
        "improve. Do not rewrite them, do not re-number them, and do not "
        "include any of them in your output.\n\n"
        "Your job is to write ONLY the test cases that are MISSING for the "
        "story you have been given. If a scenario below already covers "
        "something the story's acceptance criteria ask for, skip it — a "
        "smaller set of genuinely new cases is the correct answer, and "
        "returning an empty list is correct if the story is fully covered "
        "already. Near-misses matter: a case that covers the same scenario "
        "for a different category/role/state IS a duplicate unless the story "
        "specifically calls for that variant.\n\n"
        + "\n".join(shown) + tail
    )


#: Passed via `extra_system` (not baked into skills/testcase_writer.skill.md)
#: so the skill file's cached generation instructions stay untouched for the
#: normal JIRA-generation path — this only applies to the import path,
#: layered on top of the same schema/style rules and real examples the skill
#: file already defines.
_IMPORT_MODE_INSTRUCTIONS = """TASK MODE: IMPORT EXISTING TEST CASES (not generation).

The rows below are EXISTING test cases a human QA team already wrote, in
whatever format/columns their old tracker or spreadsheet used. Your job is
to NORMALIZE each row into the exact JSON schema this skill defines — map,
reformat, and tidy wording to match this team's style — WITHOUT inventing
new test scenarios, changing what's actually being tested, or fabricating
content that isn't present in the source row.

Rules specific to this mode:
- Preserve the tested behavior exactly. Do not add steps, assertions, or
  scenarios the source row doesn't already describe.
- If a source row already has a clear value for a field (e.g. an existing
  execution-type, status, or result column), carry it over rather than
  re-inferring a default — only infer a field's value when the source
  genuinely has no equivalent column.
- If "test_step"/"expected_result" in the source are a single unstructured
  paragraph rather than numbered steps, reformat into this team's numbered
  style ("1-...", "2-...") WITHOUT changing the substance.
- If a required field has no reasonable source equivalent and cannot be
  inferred, leave it as an empty string — do not guess a plausible-looking
  value just to fill the field.
- Every source row must produce exactly one output object, in the same
  order given — never merge two source rows into one, never split one
  source row into several.
- STEP-LEVEL FIDELITY. The rule above is about ROWS; this one is about the
  STEPS INSIDE a row, and it is separate. Every numbered step in a source
  row must survive as its own numbered step in that row's output. Never
  combine two steps into one, never summarise a step away, never drop a
  step for being minor, obvious or repetitive. "test_step" and
  "expected_result" must end up with the SAME number of numbered items as
  the source row had steps, and item N of one must correspond to item N of
  the other.

Why this rule is spelled out separately: a step-per-row export is collapsed
into one row per test case BEFORE you see it (see _group_step_rows), so by
the time these rows reach you a "row" is a whole test case and the no-merge
rule above reads as "don't merge test cases" — which leaves merging STEPS
apparently allowed. It is not. An import that returned the right number of
cases while quietly folding 80 steps into 71 is the exact failure this
paragraph exists to prevent."""


#: Splits a numbered "1-... 2-..." field back into its individual steps, the
#: same way `_group_step_rows` joined them. Used to check what came back
#: against what went in — a count, not a diff, because the model is expected
#: to reword a step and only forbidden to LOSE one.
def _count_steps(text) -> int:
    return len([part for part in re.split(r"\n\s*\n", str(text or "")) if part.strip()])


def _expected_steps_block(chunk_rows: list) -> str:
    """States each case's required step count in the user prompt.

    The static rule in `_IMPORT_MODE_INSTRUCTIONS` is necessary but was not
    sufficient on its own: naming the exact number per case is what made a
    re-run reproduce all 80 steps where the prose rule alone had returned 71.
    Giving the model the number to hit turns "don't lose steps" from a
    principle it must apply into an arithmetic check it can self-verify.

    Returns "" when the source was not a step-per-row export — there is no
    per-case step count to assert, and inventing one would be worse than
    saying nothing.
    """
    counts = [(row.get("Test Case ID") or row.get("id") or f"row {i}",
               row.get("_source_step_count"))
              for i, row in enumerate(chunk_rows or [], start=1)]
    counts = [(cid, n) for cid, n in counts if isinstance(n, int) and n > 0]
    if not counts:
        return ""
    lines = "\n".join(f"  {cid}: {n} steps" for cid, n in counts)
    return (
        "\nREQUIRED STEP COUNTS — 'test_step' and 'expected_result' must EACH "
        "contain exactly this many numbered items:\n" + lines +
        "\nThese counts are not negotiable. If a step reads as redundant, keep "
        "it anyway and reword it; do not fold it into its neighbour.\n"
    )


def _check_step_fidelity(source_rows: list, cases: list) -> list:
    """Reports any case that came back with fewer steps than it went in with.

    The existing chunk check compares CASE counts, which is a real check that
    happens to be blind to the more likely failure: an import that returns 9
    cases for 9 rows while quietly folding 80 steps into 71. Case count
    matched, nothing was reported, and the loss reached the sheet looking
    exactly like a clean import.

    Only a SHORTFALL is reported. A case that comes back with more numbered
    items than the source had is usually a paragraph correctly split into the
    numbered style this team uses, which is the reformatting the import is
    asked to do.
    """
    problems = []
    for source, case in zip(source_rows or [], cases or [], strict=False):
        want = source.get("_source_step_count")
        if not isinstance(want, int) or want <= 0:
            continue
        got_steps = _count_steps(case.get("test_step"))
        got_expected = _count_steps(case.get("expected_result"))
        if got_steps < want or got_expected < want:
            problems.append({
                "test_case_id": source.get("Test Case ID") or case.get("id") or "?",
                "source_steps": want,
                "returned_steps": got_steps,
                "returned_expected_results": got_expected,
            })
    return problems


def import_existing_test_cases(input_path: Path = None, story_key: str = "", epic_key: str = "",
                                feature_dir: Path = None, jira_source: str = "") -> Path:
    """Converts an existing, already-written test case file (Excel/CSV, any
    column layout) — OR existing test cases already stored in JIRA/Zephyr
    Scale — into this pipeline's canonical sheet format — read once,
    normalize via the same schema/house-style the Test Case Writer uses when
    generating from a JIRA story, then goes through the SAME human review
    gate as any other sheet before the Automation Planner/Coding Agent run.

    Use this instead of run() when test cases already exist (e.g. exported
    from a test management tool, hand-maintained team spreadsheet, or
    already authored directly in Zephyr Scale) rather than being generated
    fresh from a JIRA story. `story_key` is used purely as the Test Case ID
    prefix and output filename here — it need not be a real JIRA key when
    importing.

    Exactly one of `input_path` (a local .xlsx/.xlsm/.csv file) or
    `jira_source` (a Zephyr Scale test cycle id, or comma-separated test
    case keys — see `fetch_test_cases_from_jira`) must be given.
    """
    if jira_source:
        raw_rows = fetch_test_cases_from_jira(jira_source)
    elif input_path:
        raw_rows = _read_existing_test_case_file(input_path)
        # Collapse a step-per-row export (JIRA/Zephyr/Xray all export this
        # way) into one row per test case BEFORE normalizing — see
        # _group_step_rows. Without it a 5-case export arrived as 40 rows and
        # normalized into 40 single-step "test cases".
        raw_rows = _group_step_rows(raw_rows)
    else:
        raise ValueError("import_existing_test_cases requires either 'input_path' or "
                          "'jira_source' to be set.")

    source_label = jira_source if jira_source else str(input_path)
    if not raw_rows:
        raise ValueError(f"'{source_label}' has no data rows to import.")

    chunks = [raw_rows[i:i + _MAX_ROWS_PER_IMPORT_CALL]
              for i in range(0, len(raw_rows), _MAX_ROWS_PER_IMPORT_CALL)]
    if len(chunks) > 1:
        print(f"      Splitting {len(raw_rows)} existing row(s) into {len(chunks)} "
              f"normalization call(s) to avoid truncating large imports.")

    def _normalize_chunk(chunk_rows):
        user_prompt = f"""Existing test case rows to normalize (raw column names from the source file):
{json.dumps(chunk_rows, indent=2, default=str)}
{_expected_steps_block(chunk_rows)}
Generate the normalized test case set now."""
        raw = call_agent(
            agent_name="testcase_writer",
            user_prompt=user_prompt,
            feature_dir=feature_dir,
            max_tokens=16000,
            extra_system=_IMPORT_MODE_INSTRUCTIONS,
        )
        cases = parse_json_response(raw, feature_dir, "testcase_writer")
        if isinstance(cases, dict):
            for key in ("test_cases", "cases", "testCases", "items", "data"):
                if isinstance(cases.get(key), list):
                    cases = cases[key]
                    break
        if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
            raise ValueError(
                f"import chunk: expected a JSON array of case objects, "
                f"got {type(cases).__name__}."
            )
        return cases

    # Chunks are independent LLM calls (no cross-chunk reasoning) — run
    # concurrently, same pattern as _generate_one_story's multi-story path.
    all_cases, unresolved, step_losses = [], [], []
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_STORIES, len(chunks) or 1)) as pool:
        future_to_chunk = {pool.submit(_normalize_chunk, chunk): (i, chunk)
                            for i, chunk in enumerate(chunks, start=1)}
        results_by_chunk = {}
        for future in future_to_chunk:
            chunk_num, chunk_rows = future_to_chunk[future]
            try:
                result = future.result()
                if len(result) != len(chunk_rows):
                    print(f"      ⚠️  Import chunk {chunk_num}/{len(chunks)}: sent "
                          f"{len(chunk_rows)} row(s), got {len(result)} back — "
                          f"review this chunk's output carefully before marking Reviewed.")
                # Case count can match while steps go missing inside the
                # cases — see _check_step_fidelity. Reported per case, and
                # recorded in the unresolved file so the loss survives past
                # a console nobody scrolled back through.
                short = _check_step_fidelity(chunk_rows, result)
                for entry in short:
                    print(f"      ⚠️  {entry['test_case_id']}: source had "
                          f"{entry['source_steps']} step(s), import returned "
                          f"{entry['returned_steps']} step(s) / "
                          f"{entry['returned_expected_results']} expected result(s) "
                          f"— steps were merged or dropped.")
                if short:
                    print(f"      ⚠️  {len(short)} case(s) lost steps in this import. "
                          f"Compare them against the source before marking them "
                          f"Reviewed; re-importing just those rows usually recovers them.")
                    step_losses.extend(short)
                results_by_chunk[chunk_num] = result
            except Exception as exc:
                print(f"      ⚠️  Import chunk {chunk_num}/{len(chunks)} failed: {exc}")
                unresolved.extend({"source_row": row, "reason": str(exc),
                                    "error_type": type(exc).__name__,
                                    "error_kind": classify_error_kind(exc)} for row in chunk_rows)
                results_by_chunk[chunk_num] = []
    for i in range(1, len(chunks) + 1):
        all_cases.extend(results_by_chunk.get(i, []))

    if not all_cases:
        raise ValueError(f"Import of '{source_label}' produced zero normalized test cases.")

    # Renumbered sequentially across ALL chunks (not "story_key + model's own
    # id") because every chunk here shares the SAME story_key — unlike
    # _generate_one_story's multi-STORY case, the model resetting to TC-001
    # per chunk would collide within this one output sheet.
    # CARRY THE REVIEW MARK ACROSS. A sheet that a human already approved is
    # re-normalized into a NEW file here, and the review column used to be
    # written blank unconditionally — so importing an already-Reviewed sheet
    # halted the run and asked for the same approval a second time. Row order
    # is preserved by contract ("every source row must produce exactly one
    # output object, in the same order given"), so index alignment is sound.
    # Anything other than an exact "Reviewed" is NOT carried: a half-filled or
    # misspelled cell must still stop at the gate.
    for c, raw_row in zip(all_cases, raw_rows, strict=False):
        for key, value in (raw_row or {}).items():
            if str(key).strip().lower() in ("status", "review status", "reviewed")                     and str(value).strip() == "Reviewed":
                c["review_status"] = "Reviewed"
                break

    for idx, c in enumerate(all_cases, start=1):
        c["_story_key"] = story_key
        c["id"] = f"{story_key}-TC-{idx:03d}"

    out_path = artifact_path(feature_dir, f"{epic_key}_{story_key}_TestCases.xlsx")
    _write_excel(all_cases, out_path, epic_key)

    if unresolved:
        write_json(artifact_path(feature_dir, "testcase_writer_import_unresolved.json"), unresolved)
        print(f"      ⚠️  {len(unresolved)} source row(s) could not be normalized — "
              f"see {artifact_path(feature_dir, 'testcase_writer_import_unresolved.json')}")

    if step_losses:
        # The ids in step_losses are the SOURCE ids; the sheet carries the
        # renumbered ones. Recording both is the difference between a report a
        # reviewer can act on and one that names a case they cannot find.
        new_id_by_source = {}
        if len(raw_rows) == len(all_cases):
            for source, case in zip(raw_rows, all_cases, strict=True):
                key = source.get("Test Case ID") or source.get("id")
                if key:
                    new_id_by_source[key] = case.get("id", "")
        for entry in step_losses:
            entry["sheet_test_case_id"] = new_id_by_source.get(entry["test_case_id"], "")
        write_json(artifact_path(feature_dir, "testcase_writer_import_step_losses.json"), step_losses)
        print(f"      ⚠️  {len(step_losses)} case(s) came back with fewer steps than the "
              f"source — see {artifact_path(feature_dir, 'testcase_writer_import_step_losses.json')}")

    append_context_summary(
        feature_dir, "testcase_writer",
        f"Imported and normalized {len(all_cases)} existing test case(s) from "
        f"'{source_label}' for {story_key} (epic {epic_key})."
        + (f" {len(unresolved)} row(s) unresolved." if unresolved else "")
    )
    return out_path


#: Matches a JIRA-style project key prefix, e.g. "PROJ-1000" — used to decide
#: whether the part of a filename before the first underscore is an epic key
#: (as opposed to the whole stem just being a plain story key with no epic
#: embedded at all).
_JIRA_KEY_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


def _derive_story_epic_from_filename(path: Path, default_epic: str = "") -> tuple:
    """Derives (epic_key, story_key) from an import-folder filename.

    Two supported conventions:
      - "<EPIC_KEY>_<STORY_KEY>.xlsx" (e.g. "PROJ-1000_PROJ-1234.xlsx") — the
        epic is embedded in the filename itself, split on the FIRST
        underscore. Only treated this way if the part before the underscore
        actually looks like a JIRA key (matches _JIRA_KEY_RE); otherwise the
        underscore is assumed to be part of an ordinary story key/filename
        (e.g. "PROJ-1234_login.xlsx" is NOT split — the whole stem is the
        story key) rather than guessed apart incorrectly.
      - "<STORY_KEY>.xlsx" (e.g. "PROJ-1234.xlsx") — no epic embedded; the
        caller's `default_epic` (a single --epic value for the whole batch)
        is used instead.

    Raises ValueError — rather than silently producing an epic_key of ""
    that would only surface much later as a malformed output filename or a
    confusing downstream JIRA lookup — if no epic can be determined from
    either source, so the caller can report a clean per-file error without
    aborting the rest of the batch.
    """
    stem = path.stem
    epic_key, story_key = "", stem
    if "_" in stem:
        head, rest = stem.split("_", 1)
        if _JIRA_KEY_RE.match(head) and rest:
            epic_key, story_key = head, rest

    if not epic_key:
        epic_key = default_epic

    if not epic_key:
        raise ValueError(
            f"Could not determine an epic key for '{path.name}' — filename doesn't "
            f"embed one (expected '<EPIC_KEY>_<STORY_KEY>.xlsx', e.g. "
            f"'PROJ-1000_PROJ-1234.xlsx') and no --epic default was supplied for the batch."
        )
    return epic_key, story_key


def import_existing_test_cases_folder(folder_path: Path, default_epic: str,
                                       feature_dir_root: Path, jira_source: str = "") -> list:
    """Imports a whole FOLDER of existing test-case files in one run, where
    each file represents ONE story's existing manual test cases.

    Filename convention (see `_derive_story_epic_from_filename`):
      - "<EPIC_KEY>_<STORY_KEY>.xlsx" — epic embedded per file.
      - "<STORY_KEY>.xlsx" — epic taken from `default_epic` (a single --epic
        for the whole batch).

    Each file gets its OWN feature_dir at
    `feature_dir_root / "<epic_key>_<story_key>"` (created if missing — the
    same "<epic>_<story>" naming the orchestrator already uses under
    runs/<app>/), and is normalized via the same `import_existing_test_cases`
    path a single-file import uses.

    One bad file (bad filename, unreadable/unsupported format, a failed LLM
    normalization call) is caught and recorded rather than aborting the rest
    of the batch — a large folder import shouldn't fail entirely because of
    one malformed file.

    Returns a list of per-file result dicts:
      {"file": str, "status": "ok"|"error", "output_path": str|None,
       "story_key": str|None, "epic_key": str|None, "error": str|None}
    """
    files = sorted(
        p for p in folder_path.iterdir()
        # .xls is still COLLECTED on purpose. A genuine legacy .xls fails
        # per-file with an actionable "re-save as .xlsx" message (see
        # _read_existing_test_case_file) recorded in that file's result
        # entry — strictly more useful than silently skipping it and then
        # reporting "no files to import" for a folder the user can plainly
        # see has files in it. A .xls that is really a renamed .xlsx still
        # imports fine.
        if p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm", ".xls", ".csv")
    ) if folder_path.is_dir() else []

    if not files:
        raise ValueError(f"'{folder_path}' has no .xlsx/.xlsm/.csv files to import.")

    print(f"Import Folder — found {len(files)} file(s) in '{folder_path}'")
    results = []
    for i, file in enumerate(files, start=1):
        print(f"  [{i}/{len(files)}] {file.name}")
        try:
            epic_key, story_key = _derive_story_epic_from_filename(file, default_epic)
            story_feature_dir = feature_dir_root / f"{epic_key}_{story_key}"
            story_feature_dir.mkdir(parents=True, exist_ok=True)
            out_path = import_existing_test_cases(file, story_key, epic_key, story_feature_dir,
                                                   jira_source=jira_source)
            print(f"      -> {out_path}")
            results.append({
                "file": str(file), "status": "ok", "output_path": str(out_path),
                "story_key": story_key, "epic_key": epic_key, "error": None,
            })
        except Exception as exc:
            print(f"      ⚠️  Failed: {exc}")
            results.append({
                "file": str(file), "status": "error", "output_path": None,
                "story_key": None, "epic_key": None, "error": str(exc),
            })

    ok_count = sum(1 for r in results if r["status"] == "ok")
    print(f"\nImported {ok_count}/{len(results)} files successfully")
    failed = [r for r in results if r["status"] != "ok"]
    if failed:
        print("Failed file(s):")
        for r in failed:
            print(f"  - {r['file']}: {r['error']}")
    return results
