"""Translation between the existing agents and the contract layer.

Every function here does the same three things: call the existing agent
unchanged, translate its output into a contract shape, and validate it. The
agents themselves are not modified. That is the point — they carry a great
deal of behavior earned against a real application, and the way to add
guarantees on top of working code is to constrain its edges, not to rewrite
its middle.

The translation is mostly renaming, and the renames are recorded here rather
than being applied inside the agents so that when an agent is eventually
rewritten to emit contract shapes natively, the corresponding function below
collapses to a validate call and nothing else in the pipeline moves.

Keeping the agents unmodified also meant the old orchestrator kept running
throughout the migration, which is what made retiring it a decision rather
than a leap — the two could be compared side by side until this layer was
proven.

The sheet column names are the interesting case. `testcase_writer.COLUMNS`
defines a human-facing spreadsheet — "Name", "Test Step", "Prerequisite" —
which is the team's real manual test case format and is not ours to change.
The contract uses `title`, `steps`, `preconditions`. Both are right for their
audience; `sheet_row_to_case` is the seam.
"""

from __future__ import annotations

import re
from pathlib import Path

from agents import automation_planner as _planner
from agents import bug_reporter as _bug_reporter
from agents import coding_agent as _coder
from agents import executor as _executor
from agents import jira_uploader as _jira
from agents import reviewer as _reviewer
from agents import testcase_writer as _writer
from agents.artifact_io import normalize_target_file, read_json, read_text_file
from qa_pipeline.contracts import (
    AUTOMATION_PLAN,
    CLASSIFICATION,
    GENERATED_FILE,
    RUN_RESULT,
    TEST_CASE,
    VERDICTS,
)

# ---------------------------------------------------------------------------
# Test cases  (sheet <-> contract)
# ---------------------------------------------------------------------------

#: Maps the sheet's human-facing column to the contract field. The sheet is
#: the team's own format and stays as it is; this is the seam.
_SHEET_TO_CASE = {
    "Test Case ID": "test_case_id",
    "Name": "title",
    "Prerequisite": "preconditions",
    "Expected Result": "expected_result",
    "Execution Type": "execution_type",
    "Status": "status",
}

#: "Testing Type" is free text in the sheet ("Functional", "API", "Smoke").
#: Only the API signal changes how the pipeline behaves, so only it is mapped;
#: everything else becomes a tag and defaults the case to an e2e test.
_API_HINT = re.compile(r"\b(api|service|endpoint|rest|soap)\b", re.I)
_UI_HINT = re.compile(r"\b(ui|visual|layout|cosmetic|css)\b", re.I)


def _split_steps(raw) -> list:
    """The sheet holds steps as one cell. Numbered, bulleted or newline-
    separated — all three occur in real sheets, including imported ones."""
    text = str(raw or "").strip()
    if not text:
        return []
    parts = [p.strip() for p in re.split(r"\r?\n+", text) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in re.split(r"(?:(?<=\.)|;)\s*(?=\d+[.)]\s)", text) if p.strip()]
    # "1." "1)" and the skill's own "1-Step" numbering, plus bullets. "1-Step"
    # kept its literal "1-" because only "." and ")" were recognised.
    return [re.sub(r"^\s*(?:\d+\s*[.)\-]|[-*•])\s*", "", p) for p in parts] or [text]


def sheet_row_to_case(row: dict) -> dict:
    """One spreadsheet row -> one `test_case` artifact."""
    case = {field: str(row.get(column) or "").strip()
            for column, field in _SHEET_TO_CASE.items()}

    case["steps"] = _split_steps(row.get("Test Step"))
    if not case["steps"]:
        # The contract requires at least one step. A row with none is a real
        # defect in the sheet, and surfacing it here — where it names the
        # case — beats it becoming an empty test three stages later.
        case["steps"] = ["(no steps recorded in the sheet)"]

    execution = case.get("execution_type", "").strip().lower()
    case["execution_type"] = "Automated" if execution == "automated" else "Manual"

    testing_type = str(row.get("Testing Type") or "")
    if _API_HINT.search(testing_type):
        case["test_kind"] = "api"
    elif _UI_HINT.search(testing_type):
        case["test_kind"] = "ui"
    else:
        case["test_kind"] = "e2e"

    case["tags"] = [t.strip() for t in re.split(r"[,;/]", testing_type) if t.strip()]
    case["acceptance_criterion"] = str(row.get("Description") or "").strip()
    case["title"] = case["title"] or "(untitled test case)"
    case["expected_result"] = case["expected_result"] or "(not specified)"
    case["source"] = "legacy_testcase" if not case["test_case_id"] else "inferred"
    case["confidence"] = 0.9
    case["declined"] = False
    case["decline_reason"] = ""

    if not case["test_case_id"]:
        case["test_case_id"] = "UNKNOWN"
    return TEST_CASE.coerce(case)


def read_test_cases(sheet_path) -> list:
    """Reads the sheet through the existing reader, then contract-shapes it.

    `coding_agent.read_test_case_sheet` is reused rather than reimplemented
    because it already drops formatting-only blank rows and raises loudly on a
    hand-edited sheet whose row length no longer matches the header — a
    misalignment that would otherwise shift every column silently.
    """
    return [sheet_row_to_case(row) for row in _coder.read_test_case_sheet(Path(sheet_path))]


def write_test_cases(sheet_path, story_key, epic_key, feature_dir, *,
                     figma_url: str = "", design_doc: str = "", api_spec: str = "",
                     knowledge_provider=None, reference_cases=None,
                     knowledge_available: bool = None, feature_link: str = ""):
    """Runs the existing Test Case Writer, then reads its sheet back.

    `knowledge_provider` is a `(query) -> str` callable the writer calls once
    per story, after its JIRA fetch, to pull relevant ledger knowledge into
    the generation prompt. A callable rather than a rendered block because
    only the writer knows the story's vocabulary, and only that vocabulary
    makes the query work — see `testcase_writer._knowledge_block`. Keeping the
    ledger behind a callable also preserves the layering rule: `agents/`
    imports nothing from `qa_pipeline/`.

    `api_spec` is an OpenAPI/Swagger spec for the service under test — the API
    counterpart of `figma_url`. Given here it is an extra REFERENCE beside the
    story, so criteria stated as endpoints and status codes get API test cases
    instead of invented UI steps. (The writer can also derive stories from a spec
    on its own, but that is a standalone-CLI mode: the pipeline runs one story per
    invocation, and deriving several would break that.)

    `feature_link` is a pasted JIRA Feature/Epic/Story/Test link or key (see
    JIRA_FEATURE_LINK / --feature-link) — the writer walks it into ancestor
    issues and linked Zephyr Test issues, each with description/comments/
    attachments, and folds that into the generation prompt. See
    `agents.testcase_writer.fetch_jira_hierarchy`.
    """
    _writer.run(story_key, epic_key, figma_url, Path(feature_dir),
                app_design_doc_path=design_doc or None,
                knowledge_provider=knowledge_provider,
                api_spec_path=api_spec,
                reference_cases=reference_cases or None,
                knowledge_available=knowledge_available,
                feature_link=feature_link)
    return read_test_cases(sheet_path)


def import_test_cases(sheet_path, story_key, epic_key, feature_dir, *,
                      source_file: str = "", jira_source: str = ""):
    """Imports test cases that already exist instead of generating new ones.

    Two sources, one code path in the underlying agent:
      - `source_file`  an .xlsx/.xlsm/.csv the team already maintains
      - `jira_source`  a Zephyr Scale cycle id, or comma-separated case keys

    Either way the sheet is normalized into this pipeline's format and then
    goes through the SAME human review gate as a generated one. That is the
    point: an imported case has had no more scrutiny than a generated one —
    it was written for a previous release, by someone who is not in the room,
    against a UI that may since have changed.
    """
    _writer.import_existing_test_cases(
        Path(source_file) if source_file else None,
        story_key, epic_key, Path(feature_dir),
        jira_source=jira_source)
    return read_test_cases(sheet_path)


def import_test_cases_folder(folder, default_epic, context_root, *, jira_source: str = ""):
    """Batch-imports a folder of sheets, one file per story.

    Deliberately does NOT continue into planning or code generation. Those
    stages need per-story configuration this batch mode has no way to collect
    per file, and silently guessing it for forty stories at once is how a
    batch run produces forty subtly wrong suites. Import here, review each
    sheet, then run the pipeline per story.
    """
    return _writer.import_existing_test_cases_folder(
        Path(folder), default_epic, Path(context_root), jira_source=jira_source)


def peek_story_from_xml(path):
    """Reads the story and epic keys out of a local JIRA XML export.

    Resolved BEFORE the run starts so the feature directory and sheet are
    named with the real JIRA key ("PROJ-68308") rather than the literal file
    path. The Test Case Writer re-parses the same file itself — cheap, it is
    plain XML with no model call — so the two stay in sync by construction
    rather than by threading a parsed dict through every layer.

    Returns `{}` for anything that is not a readable XML export, so a caller
    can treat "this --story is a plain key" as the ordinary case.
    """
    candidate = Path(path)
    if candidate.suffix.lower() != ".xml" or not candidate.is_file():
        return {}
    try:
        peeked = _writer.fetch_jira_story_from_file(candidate) or {}
    except Exception as exc:
        print(f"⚠️  Could not parse {candidate.name} as a JIRA XML export "
              f"({type(exc).__name__}: {exc}) — treating --story as a plain key.")
        return {}
    return {"story_key": peeked.get("story_key") or candidate.stem,
            "epic_key": peeked.get("epic_key") or ""}


def filter_automatable(cases: list, include_manual: bool = False) -> list:
    """Contract-shaped equivalent of `coding_agent.filter_automatable_cases`.

    Kept as its own function rather than calling through, because the existing
    one reads the sheet's `"Execution Type"` column while these are already
    translated. The RULE is identical and deliberately so: generating code for
    a case the sheet itself calls Manual silently overrides a human judgment
    call.
    """
    if include_manual:
        return list(cases)
    return [c for c in cases if c.get("execution_type") == "Automated"]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------

def _case_to_planner_row(case: dict) -> dict:
    """Back to the shape the existing Planner expects (sheet columns)."""
    return {
        "Test Case ID": case.get("test_case_id", ""),
        "Name": case.get("title", ""),
        "Description": case.get("acceptance_criterion", ""),
        "Prerequisite": case.get("preconditions", ""),
        "Test Step": "\n".join(case.get("steps") or []),
        "Expected Result": case.get("expected_result", ""),
        "Execution Type": case.get("execution_type", "Automated"),
        "Testing Type": ", ".join(case.get("tags") or []),
        "Status": case.get("status", ""),
    }


def plan(cases: list, pom_summary: str, feature_dir, element_catalogue: str = "") -> list:
    """Runs the existing Automation Planner and contract-shapes its output.

    `element_catalogue` carries the ledger's real element vocabulary — see
    qa_pipeline.brain.catalogue for why the Planner cannot name elements
    correctly without it."""
    planner_path = _planner.run([_case_to_planner_row(c) for c in cases],
                                pom_summary, Path(feature_dir),
                                element_catalogue=element_catalogue)
    raw = read_json(planner_path, default=[]) or []
    return [normalize_plan(entry) for entry in raw if isinstance(entry, dict)]


def replan(cases: list, pom_summary: str, feature_dir, repair_prompt: str,
           element_catalogue: str = "") -> list:
    """Re-plans exactly `cases`, with a verifier's diagnosis threaded into the
    Planner's system prompt. Used by the orchestrator's repair loop.

    This is the seam that was missing. `stage_plan` received a precise repair
    instruction from `gate_plan` — "element 'Save' on 'Registration' could not be
    confirmed; name it the way the UI actually labels it" — and had nowhere to
    put it, so it re-issued the identical planning call for the identical case
    list and got the identical failure, up to `max_repairs` times. The Planner
    now exposes `plan_cases(..., extra_system=...)` for precisely this, so the
    retry carries the diagnosis and re-plans only what failed.

    Unlike `plan()`, this writes NO files: `planner_doc.json` holds the whole
    feature's plans, and letting a one-case repair rewrite it would delete
    every other case's plan. The caller assembles the accepted plans and
    persists them once.
    """
    if not cases:
        return []
    # The catalogue leads, the diagnosis follows. A repair that re-states the
    # vocabulary is far more likely to land than one that only says what was
    # wrong: the first attempt failed precisely because the Planner had no
    # list to choose from, and dropping it on the retry would reproduce that.
    system = "\n\n".join(part for part in (element_catalogue, repair_prompt) if part)
    raw = _planner.plan_cases(
        [_case_to_planner_row(c) for c in cases], pom_summary, Path(feature_dir),
        extra_system=system,
        header="Re-plan these test cases, fixing the problems listed above:",
        label="repair")
    return [normalize_plan(entry) for entry in raw if isinstance(entry, dict)]


#: Fields `automation_planner._inspect_plans` attaches to a plan AFTER the
#: model has spoken — the browser's own observations, not model output:
#: what was on the screen, whether login and navigation actually succeeded,
#: and the confidence correction the grade forced. `replan` cannot reproduce
#: any of it, because it calls `plan_cases` (the raw LLM call) and never
#: drives a browser.
_INSPECTION_EVIDENCE_FIELDS = (
    "dom_inspection_result",
    "dom_inspection_quality",
    "dom_inspection_context",
    "dom_inspection_reused_from_cache",
    "dom_inspection_confidence",
    "spot_checked",
    "reused_pom_unverified",
)

#: What decides whether the first pass's snapshot is still evidence for the
#: repaired plan: same click path, same screens, same file. Change any of
#: those and the snapshot is of somewhere else.
_CLICK_PATH_FIELDS = ("navigation_keywords", "target_file")


def _screens(plan: dict) -> set:
    return {str(e.get("screen") or "") for e in (plan.get("elements") or [])
            if isinstance(e, dict)}


def merge_replanned(original: dict, repaired: dict) -> dict:
    """Folds a gate-repaired plan onto the first-pass plan it replaces,
    keeping the browser evidence the repair could not have produced.

    `stage_plan` used to substitute the repaired plan WHOLESALE. Since
    `replan` never inspects, that silently deleted `dom_inspection_result`
    and its grade — so the one case the gate had just corrected reached the
    Coding Agent with no snapshot of its screen. If the repaired plan also
    happened to say `needs_dom_inspection: false`, nothing re-inspected it
    either: no DOM in the generation prompt, no snapshot for
    `_verify_file_locators` to check the generated locators against, and
    therefore no locator repair pass. The most-scrutinized case in the run
    ended up the least grounded one.

    Evidence is carried forward ONLY while it still describes the same
    screen — same navigation_keywords, same element screens, same
    target_file. A repair that redirects the click path has, by definition,
    invalidated the snapshot: keeping it would be worse than keeping
    nothing, because a stale snapshot LOOKS like verification. In that case
    the fields are dropped and the plan is marked for re-inspection
    downstream, which `coding_agent._cases_needing_reinspection` then picks
    up (confidence 0.0 is below its _DOM_CONFIDENCE_THRESHOLD).
    """
    if not isinstance(repaired, dict) or not repaired:
        return original
    if not isinstance(original, dict):
        return repaired

    merged = dict(repaired)
    same_click_path = (
        all(original.get(f) == repaired.get(f) for f in _CLICK_PATH_FIELDS)
        and _screens(original) == _screens(repaired)
    )

    if same_click_path:
        for field in _INSPECTION_EVIDENCE_FIELDS:
            # Only ever ADDS what the repair lacks. A repair that somehow
            # carries its own value for one of these keeps it.
            if field in original and field not in repaired:
                merged[field] = original[field]
        return merged

    for field in _INSPECTION_EVIDENCE_FIELDS:
        merged.pop(field, None)
    # Not "maybe re-inspect": there is now no snapshot for this case at all,
    # and it is a case the gate has already rejected once.
    merged["needs_dom_inspection"] = True
    merged["dom_inspection_confidence"] = 0.0
    return merged


#: Page objects generated from a human recording. Not a guess about naming:
#: `utils.recording_parser.generate` writes every one of them to
#: `<repo>/src/pages/recorded/` and names the class
#: `f"Recorded{pascal_case(test_case_id)}Page"`, so the prefix and the
#: directory are two halves of one guarantee it makes.
_RECORDED_POM_PREFIX = "Recorded"
_RECORDED_POM_DIR = "src/pages/recorded"
_DEFAULT_POM_DIR = "src/pages"


def _pom_dir(class_name: str) -> str:
    """Where a page object named `class_name` lives, when the plan didn't say.

    A flat `src/pages/<Class>.ts` was assumed for every class, which is wrong
    for exactly the ones this project leans on hardest — the recorded page
    objects, its highest-confidence source. The Coding Agent imports whatever
    this list says, so a wrong directory is an unresolvable import, a failed
    syntax gate and a rolled-back chunk.
    """
    if class_name.startswith(_RECORDED_POM_PREFIX):
        return _RECORDED_POM_DIR
    return _DEFAULT_POM_DIR


def normalize_plan(entry: dict) -> dict:
    """Coerces one planner entry into the `automation_plan` contract.

    `target_file` is normalized to forward slashes with any leading './'
    stripped, because the Executor passes it to Playwright's CLI where it is
    treated as a REGEX matched against test file paths — a backslashed or
    absolute path matches nothing and yields "No tests found" with zero tests
    collected, reported as a clean run.
    """
    plan_entry = dict(entry)
    plan_entry["target_file"] = normalize_target_file(plan_entry.get("target_file"))

    # `layer` is the Planner skill's own long-standing field name ("UI" /
    # "API" / "E2E"); `test_kind` is the contract's. Reading both means a plan
    # from either vocabulary lands in the right tests/<kind>/ directory —
    # without it every UI-only case was silently graded "e2e" and then failed
    # the scaffold gate for sitting in tests/ui/.
    kind = str(plan_entry.get("test_kind") or plan_entry.get("kind")
               or plan_entry.get("layer") or "e2e").lower()
    plan_entry["test_kind"] = kind if kind in ("ui", "api", "e2e") else "e2e"

    elements = []
    for raw in plan_entry.get("elements") or []:
        if isinstance(raw, str):
            elements.append({"logical_name": raw, "screen": "", "card_id": "", "locator": ""})
        elif isinstance(raw, dict):
            elements.append({
                "logical_name": raw.get("logical_name") or raw.get("name") or "",
                "screen": raw.get("screen") or "",
                "card_id": raw.get("card_id") or "",
                "locator": raw.get("locator") or "",
            })
    plan_entry["elements"] = elements

    for field in ("page_objects", "components", "fixtures", "navigation_keywords"):
        value = plan_entry.get(field)
        if isinstance(value, str):
            plan_entry[field] = [value] if value else []
        elif not isinstance(value, list):
            plan_entry[field] = []
        else:
            plan_entry[field] = [str(v) for v in value if v]

    # `reused_pom` is the Planner skill's field for existing page objects, and
    # it is emitted as "ClassName.method" rather than as a path. Fold the
    # class half into page_objects as a scaffold path so the reuse signal
    # reaches the scaffold gate and the Coding Agent's import list instead of
    # being dropped on the floor between two vocabularies.
    reused = plan_entry.get("reused_pom")
    if isinstance(reused, str):
        reused = [reused]
    # `src/pages/<Class>.ts` is a FALLBACK for a class the plan did not
    # locate, not an assertion about where page objects live. They do not all
    # sit directly in src/pages/: everything generated from a human recording
    # lives in src/pages/recorded/.
    #
    # Observed on a real run of VENDOR-TC-001 — the plan came back with
    # six page objects: the three real recorded ones under
    # src/pages/recorded/, plus the same three again under src/pages/, which
    # exist nowhere. That list is handed to the Coding Agent as its import
    # list, so half of it resolves to nothing (TS2307), the generated file
    # fails its syntax gate and the whole chunk is rolled back. A fully
    # grounded plan (31/31 elements resolved) still produces no spec.
    #
    # So: only add the fallback when this class is not ALREADY located
    # somewhere in page_objects. Matched on the file name so a path in any
    # directory counts, and so two distinct classes never collapse into one.
    located = {Path(p).name for p in plan_entry["page_objects"]}
    for entry in reused or []:
        class_name = str(entry).split(".", 1)[0].strip()
        if not class_name or "/" in class_name:
            continue
        if f"{class_name}.ts" in located:
            continue
        plan_entry["page_objects"].append(f"{_pom_dir(class_name)}/{class_name}.ts")
        located.add(f"{class_name}.ts")

    plan_entry.setdefault("confidence", 0.7)
    plan_entry.setdefault("source", "inferred")
    plan_entry.setdefault("declined", False)
    plan_entry.setdefault("decline_reason", "")
    return AUTOMATION_PLAN.coerce(plan_entry)


# ---------------------------------------------------------------------------
# Coding Agent
# ---------------------------------------------------------------------------

def generate_code(*, sheet_path, cycle_id, planner_doc_path, conventions_path,
                  feature_dir, repo_dir, pom_summary, include_manual, design_doc,
                  repair_prompt: str = "", prior_files: dict = None,
                  only_case_ids=None) -> dict:
    """Runs the existing Coding Agent and contract-shapes its files.

    `repair_prompt` carries a gate's diagnosis into a retry. This is the same
    seam `replan` opened on the Planner side, and it was the missing half on
    this one: the orchestrator's repair loop had a precise instruction
    ("`Save` matches 3 elements; scope it") and nowhere to put it, so each
    repair attempt re-issued the identical generation call.
    """
    result = _coder.run(
        sheet_path=Path(sheet_path), cycle_id=cycle_id,
        planner_doc_path=Path(planner_doc_path),
        conventions_path=Path(conventions_path) if conventions_path else None,
        feature_dir=Path(feature_dir), repo_output_dir=Path(repo_dir),
        existing_pom_summary=pom_summary, include_manual=include_manual,
        app_design_doc_path=design_doc or None,
        repair_prompt=repair_prompt or "",
        prior_files=prior_files,
        only_case_ids=only_case_ids,
    )
    return {
        "files": [normalize_generated_file(f) for f in result.get("files", [])],
        "unresolved": result.get("unresolved", []),
    }


#: A Playwright test/it title, permissive about quote style — mirrors
#: `coding_agent._TEST_TITLE_RE`, which checks the same convention from the
#: other side.
_TEST_TITLE = re.compile(r"""\b(?:test|it)\s*\(\s*['"`]([^'"`]*)""")


def _case_ids_in_titles(content: str) -> list:
    """Case ids read off the test titles in a generated file, in order and
    de-duplicated. Empty when the file has no titled tests (a page object),
    which is the correct answer for one."""
    seen, out = set(), []
    for title in _TEST_TITLE.findall(str(content or "")):
        match = _ID_IN_TITLE.match(title.strip())
        if match and match.group(1) not in seen:
            seen.add(match.group(1))
            out.append(match.group(1))
    return out


def normalize_generated_file(raw: dict) -> dict:
    """Coerces one generated file into the `generated_file` contract.

    `test_case_ids` is the field that matters. The Coding Agent's own skill
    already asks each file to declare which cases it implements — a signal the
    old pipeline paid for and then only used as the FIRST of four fallback
    tiers in `_resolve_case_file`. Making it a required, validated field is
    what lets the other three tiers be deleted.
    """
    generated = dict(raw)
    path = str(generated.get("path") or "")
    generated["path"] = re.sub(r"^(\./)+", "", path.replace("\\", "/")).lstrip("/")

    ids = generated.get("test_case_ids") or generated.get("test_case_id") or []
    if isinstance(ids, str):
        ids = [ids]
    ids = [str(i).strip() for i in ids if str(i).strip()]

    # Backfill from the file's own test titles when the model omitted the
    # field. The Coding Agent's skill requires every test title to BEGIN with
    # its case id, and that convention is separately checked — so when the
    # titles carry ids and the declaration does not, the titles are evidence,
    # not a guess. Recovering the join here is worth it: without it the whole
    # file is rejected by the scaffold gate and regenerated, at full price,
    # over a field the file itself already answers.
    if not ids:
        ids = _case_ids_in_titles(generated.get("content", ""))
    generated["test_case_ids"] = ids

    kind = str(generated.get("kind") or "").lower()
    if kind not in ("spec", "page", "component", "fixture", "data", "util"):
        lowered = generated["path"].lower()
        if "/pages/" in lowered:
            kind = "page"
        elif "/components/" in lowered:
            kind = "component"
        elif "fixture" in lowered:
            kind = "fixture"
        elif "/data/" in lowered:
            kind = "data"
        elif lowered.endswith((".spec.ts", ".spec.js")):
            kind = "spec"
        else:
            kind = "util"
    generated["kind"] = kind

    generated.setdefault("content", "")
    generated.setdefault("confidence", 0.7)
    generated.setdefault("source", "inferred")
    generated.setdefault("declined", False)
    generated.setdefault("decline_reason", "")
    return GENERATED_FILE.coerce(generated)


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------

#: `executor._INFRA` — the test environment itself is broken, as opposed to
#: tests having run and failed. Mirrored rather than imported to keep this
#: module's "translate, don't depend on internals" posture, and asserted
#: against the real value by the executor tests.
_INFRA_FAILURE = "infra"


def execute(repo_dir, feature_dir, *, test_files=None, workers=None, shard=None,
            out_filename: str = None, grep_ids=None, reuse_auth: bool = False,
            retries: int = None, fresh_reports: bool = False) -> dict:
    """Runs the existing Executor and contract-shapes its report.

    `executor.run` RETURNS its failures rather than raising — deliberately,
    since "no live test environment" is a normal outcome the caller checks
    for, not an exception. That distinction is preserved in the contract as
    the `ran` field, so a caller can tell "ran and failed" from "never ran"
    without inspecting a returncode for None.
    """
    kwargs = {"env_config": {}, "feature_dir": Path(feature_dir)}
    if test_files:
        kwargs["test_files"] = list(test_files)
    if workers is not None:
        kwargs["workers"] = workers
    if shard is not None:
        kwargs["shard"] = shard
    if out_filename:
        kwargs["out_filename"] = out_filename
    if grep_ids:
        kwargs["grep_ids"] = list(grep_ids)
    if reuse_auth:
        kwargs["reuse_auth"] = True
    if retries is not None:
        kwargs["retries"] = retries
    if fresh_reports:
        kwargs["fresh_reports"] = True

    raw = _executor.run(Path(repo_dir), **kwargs)
    return normalize_run_result(raw)


def normalize_run_result(raw: dict) -> dict:
    """`ran` means "Playwright produced real pass/fail results", which is what
    every caller actually branches on — not "a process exited".

    It used to be `returncode is not None`, and that silently mis-classified
    the most important failure in the set. A Playwright config error, a
    missing browser binary or a bad `testMatch` fails BEFORE running any test
    and exits NON-ZERO, so it has a returncode: `ran` came back True with zero
    cases, `stage_execute` recorded "0 passed", the Reviewer was handed an
    empty result, and the pipeline exited 0. A green run against a broken
    environment. `failure_kind` had the answer at every layer and nothing
    consulted it.

    A genuine test failure also exits non-zero and MUST still count as ran —
    that is the distinction `failure_kind` draws and returncode cannot.
    """
    returncode = raw.get("returncode")
    failure_kind = raw.get("failure_kind") or ""
    result = {
        "returncode": returncode if isinstance(returncode, int) else -1,
        "ran": returncode is not None and failure_kind != _INFRA_FAILURE,
        "failure_kind": failure_kind,
        "error": str(raw.get("error") or ""),
        "stats": raw.get("stats") or {},
        "cases": _flatten_cases(raw),
        "allure_report": raw.get("allure_report") or "",
        "test_files": list(raw.get("test_files") or []),
    }
    return RUN_RESULT.coerce(result)


def _flatten_cases(raw: dict) -> list:
    """Playwright's JSON report nests suite -> spec -> test -> result. Flattened
    here so every consumer reads one list instead of re-walking the tree."""
    cases = []

    def walk(suites, file_hint=""):
        for suite in suites or []:
            path = suite.get("file") or file_hint
            for spec in suite.get("specs") or []:
                title = spec.get("title") or ""
                for test in spec.get("tests") or []:
                    # ONE ROW PER TEST, from its FINAL attempt. Under CI the
                    # scaffold retries once, so a test that failed then passed
                    # produced two rows, one "failed": `passed` was overcounted
                    # and a heal that passed on retry was judged to have failed
                    # (and its good locator blocklisted). The earlier failed
                    # attempts are kept as `attempts`/`flaky`, not as cases.
                    results = test.get("results") or []
                    if not results:
                        continue
                    outcome = results[-1]
                    status = outcome.get("status") or "failed"
                    if test.get("status") == "flaky":
                        status = "passed"
                    cases.append({
                        "test_case_id": _case_id_from_title(title),
                        "file": str(spec.get("file") or path or "").replace("\\", "/"),
                        "status": status,
                        "attempts": len(results),
                        "flaky": test.get("status") == "flaky" or (
                            status == "passed" and len(results) > 1),
                        "duration_ms": int(outcome.get("duration") or 0),
                        "error_message": (outcome.get("error") or {}).get("message", "")
                        if isinstance(outcome.get("error"), dict) else "",
                        "screenshots": [a.get("path", "") for a in outcome.get("attachments") or []
                                        if a.get("contentType", "").startswith("image/")],
                        "trace": next((a.get("path", "") for a in outcome.get("attachments") or []
                                       if a.get("name") == "trace"), ""),
                        "failure_dom": "",
                        # A test annotated {type: 'issue'} is a KNOWN mismatch between the case and
                        # the application, not an unexplained failure. `execute` lists these apart.
                        "issue": next((str(a.get("description") or "issue")
                                       for a in test.get("annotations") or []
                                       if isinstance(a, dict) and a.get("type") == "issue"), ""),
                        # Uncaught script errors the APPLICATION threw during the test (the
                        # `recordAppErrors` fixture); informational, never a failure.
                        "app_errors": [str(a.get("description") or "") for a in test.get("annotations") or []
                                       if isinstance(a, dict) and a.get("type") == "app-error"],
                    })
            walk(suite.get("suites"), path)

    walk(raw.get("suites"))
    return cases


from utils.case_index import ID_IN_TITLE as _ID_IN_TITLE  # noqa: E402
from utils.case_index import case_id_from_title as _case_id_from_title  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Reviewer
# ---------------------------------------------------------------------------

def review(run_result: dict, generated_files: list, conventions_path,
           known_flaky: list, feature_dir, case_index=None) -> list:
    """Runs the existing Reviewer and contract-shapes its classifications."""
    standards = read_text_file(conventions_path) if conventions_path else ""
    review_path = _reviewer.run(
        run_result, generated_files, standards, known_flaky or [],
        Path(feature_dir), case_index=case_index)
    raw = read_json(review_path, default={}) or {}
    return ([normalize_classification(entry)
             for entry in raw.get("failure_classification", [])
             if isinstance(entry, dict)], review_path)


def _confidence_or_default(value, default: float = 0.7) -> float:
    """A model can answer "high" or "0.8 "; a bare float() raised AFTER the
    review call was paid for and lost every verdict in it."""
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def normalize_classification(entry: dict) -> dict:
    """`classification` -> `verdict`, plus the citation requirement.

    The existing Reviewer emits its verdict under the key `classification`
    with the vocabulary `known_flaky` / `genuine_bug` / `fixable_test_issue`.
    The contract keeps those exact values (renaming them would mean the
    contract and the live pipeline disagreed about the one field the
    Self-Healer branches on) and only moves them to a clearer field name.
    """
    verdict = str(entry.get("verdict") or entry.get("classification") or "").strip()
    unclassified = False
    if verdict not in VERDICTS:
        # An unknown or missing verdict used to become "fixable_test_issue",
        # i.e. a product failure the model mislabelled went to the Self-Healer
        # and was "fixed" away. Map the obvious spellings; anything else is
        # surfaced as a bug for a person to look at, never silently healed.
        lowered = verdict.lower()
        if "flak" in lowered:
            verdict = "known_flaky"
        elif any(word in lowered for word in ("fixable", "test_issue", "locator", "selector")):
            verdict = "fixable_test_issue"
        else:
            verdict, unclassified = "genuine_bug", True

    citation = str(entry.get("citation") or entry.get("evidence") or "").strip()
    if not citation:
        # An uncited verdict is a guess wearing a diagnosis's clothes. Rather
        # than reject the whole review, fall back to the reason text and let
        # the gate report the weakness — the Reviewer runs on real failure
        # output, so its reason is usually quoting the error already.
        citation = str(entry.get("reason") or "")[:400]

    return CLASSIFICATION.coerce({
        "test_case_id": str(entry.get("test_case_id") or ""),
        "file": str(entry.get("file") or "").replace("\\", "/"),
        "verdict": verdict,
        "severity": str(entry.get("severity") or "n/a"),
        "reason": (("[reviewer gave no usable verdict - needs a person to triage] "
                    if unclassified else "") + str(entry.get("reason") or "")),
        "citation": citation,
        "suggested_fix": str(entry.get("suggested_fix") or ""),
        "confidence": _confidence_or_default(entry.get("confidence")),
        "source": "inferred",
        "declined": bool(entry.get("declined") or False),
        "decline_reason": str(entry.get("decline_reason") or ""),
    })


# ---------------------------------------------------------------------------
# Bug reporter / JIRA (no LLM, pass-through)
# ---------------------------------------------------------------------------

def report_bugs(*, review_path, run_result, repo_dir, feature_dir,
                still_failing_ids, case_index=None, executed_case_ids=None):
    kwargs = {}
    if executed_case_ids is not None:  # None keeps the bug reporter's old behaviour
        kwargs["executed_case_ids"] = set(executed_case_ids)
    return _bug_reporter.run(
        review_path=review_path, run_results=run_result,
        repo_output_dir=Path(repo_dir), feature_dir=Path(feature_dir),
        still_failing_ids=set(still_failing_ids or ()), case_index=case_index,
        **kwargs)


def upload_to_jira(sheet_path, feature_dir, project, issue_type):
    return _jira.run(Path(sheet_path), Path(feature_dir), project, issue_type)
