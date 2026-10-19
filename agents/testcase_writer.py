"""
Agent 1: Test Case Writer
IN:  JIRA story + Epic link (context), plus whichever references describe the
     thing under test — a Figma link (UI reference) and/or an OpenAPI/Swagger
     spec (`--api-spec`, API reference). The references decide what kind of
     cases come out: browser steps and on-screen results for UI, HTTP requests
     and status codes for API. See skills/testcase_writer.skill.md, "STEP 0".
     A spec with no story keys is also a story SOURCE — see `run`.
OUT: Excel sheet, one row per test case, matching your manual test case format:
     Name, Description, Product, Component/s, Sub-Component, Prerequisite,
     Test Step, Test Data, Expected Result, Execution Type, Testing Type
     (+ 2 pipeline-only columns appended at the end — see COLUMNS below)

NOTE on MCP: JIRA/Figma fetch calls below are stubbed as `fetch_jira_story`
and `fetch_figma_context`. Replace the stub bodies with real MCP tool calls
once JIRA MCP / Figma MCP are wired into your environment. The prompt/parse/
Excel-writing logic is fully functional as-is.
"""

import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# Lets this file run standalone (`python agents/testcase_writer.py ...`) as
# well as imported as part of the `agents` package — `__package__` is falsy
# only in the former case, where `utils` wouldn't otherwise be importable.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import write_json

# Moved to agents/testcase_api_spec.py; re-exported so existing imports keep working.
from agents.testcase_api_spec import (  # noqa: F401
    _MAX_OPS_PER_API_STORY,
    _operation_criterion,
    _render_schema,
    _request_schema_name,
    api_spec_to_stories,
    fetch_api_spec,
    render_api_contract,
)

# Moved to agents/testcase_design_docs.py; re-exported so existing imports keep working.
from agents.testcase_design_docs import (  # noqa: F401
    _extract_docx_images,
    _fetch_figma_api,
    _read_local_design_doc,
    fetch_figma_context,
)

# Moved to agents/testcase_import.py; re-exported so existing imports keep working.
from agents.testcase_import import (  # noqa: F401
    _CASE_KEY_COLUMN_RE,
    _EXPECTED_COLUMN_RE,
    _IMPORT_MODE_INSTRUCTIONS,
    _JIRA_KEY_RE,
    _MAX_BASELINE_TITLES,
    _MAX_ROWS_PER_IMPORT_CALL,
    _STEP_COLUMN_RE,
    _STEP_DATA_COLUMN_RE,
    _check_step_fidelity,
    _count_steps,
    _derive_story_epic_from_filename,
    _detect_step_row_layout,
    _expected_steps_block,
    _group_step_rows,
    fetch_test_cases_from_jira,
    import_existing_test_cases,
    import_existing_test_cases_folder,
    render_reference_baseline,
)

# Moved to agents/testcase_jira.py; re-exported so existing imports keep working.
from agents.testcase_jira import (  # noqa: F401
    _AC_HEADING_RE,
    _AC_SIBLING_SECTIONS,
    _INGESTIBLE_ATTACHMENT_EXTS,
    _extract_acceptance_criteria,
    _extract_text_from_adf,
    fetch_jira_hierarchy,
    fetch_jira_story,
    fetch_jira_story_from_file,
)

# Moved to agents/testcase_limits.py; re-exported so existing imports keep working.
from agents.testcase_limits import (  # noqa: F401
    _MAX_PARALLEL_STORIES,
)

# Moved to agents/testcase_sheet.py; re-exported so existing imports keep working.
from agents.testcase_sheet import (  # noqa: F401
    _EXCEL_CELL_LIMIT,
    _OLE2_SIGNATURE,
    COLUMNS,
    MANUAL_COLUMNS,
    PIPELINE_COLUMNS,
    REVIEW_COLUMN,
    _load_workbook_tolerating_xls,
    _read_existing_test_case_file,
    _to_cell_text,
    _write_excel,
)
from agents.testcase_prompt import (
    compact_json,
    relevant_design_data,
    story_for_prompt,
    story_with_hierarchy,
)
from utils import app_design_doc as _app_design_doc
from utils.llm_client import append_context_summary, call_agent, parse_json_response
from utils.run_layout import artifact_path

# Standing instructions live in skills/testcase_writer.skill.md (cached on
# every call — see utils/llm_client.py). Edit the skill file, not this code,
# to change agent behavior.


# ===========================================================================
# API CONTRACT (OpenAPI/Swagger) — the API-side counterpart of Figma
#
# Figma tells this agent what the UI looks like; an OpenAPI spec tells it what
# the service accepts and returns. Both are REFERENCES attached to a story, and
# a spec can additionally be used as the STORY SOURCE itself (`--api-spec` with
# no `--story`), because a spec already carries everything the prompt needs:
# operations, request schemas, and a documented meaning for every status code.
# The skill file (skills/testcase_writer.skill.md, "STEP 0") decides per case
# whether to write UI or API cases from whichever references are present.
# ===========================================================================


#: The story key used when generation is driven by documents/Figma/existing
#: cases rather than by a tracker item. Prefixed so a sheet that came from
#: documents is never mistaken for one traceable to a real story — the Test
#: Case IDs carry it, and "DOC-1-TC-001" answers "where did this come from?"
#: on sight.
_DOCUMENT_STORY_KEY = "DOC-1"


def _synthesize_story(epic_key: str, sources: dict) -> dict:
    """Builds a story-shaped dict when there is no tracker story to fetch.

    The writer's whole pipeline downstream of resolution expects a story:
    a title to query the ledger with, a description, and acceptance criteria
    for the coverage gate. Documents alone are a legitimate input — plenty of
    teams have a design doc and no JIRA item for it — so rather than refusing,
    this gives that path the same shape every other path already produces.

    It states in the description WHICH sources are present, because the model
    is being asked to derive scenarios from material that arrives in other
    system blocks (design doc, ledger, Figma, coverage baseline); without this
    it sees an empty story and reasonably concludes there is nothing to do.

    `acceptance_criteria` is deliberately left EMPTY rather than invented. The
    coverage gate reads it, and a fabricated criterion would be checked
    against, reported on, and trusted as if a human had written it.
    """
    present = [name for name, ok in sources.items() if ok]
    return {
        "story_key": _DOCUMENT_STORY_KEY,
        "epic_key": epic_key or "DOC",
        "title": "Test coverage derived from supplied documentation",
        "description": (
            "There is no tracker story for this run. Derive the test cases "
            "from the material supplied in the other context blocks of this "
            "prompt: " + ", ".join(present) + ". Cover the behaviour those "
            "sources actually describe — screens, fields, rules, states and "
            "error paths — and do not invent product areas they do not "
            "mention."
        ),
        "acceptance_criteria": [],
    }


def run(story_keys, epic_key: str, figma_url: str, feature_dir: Path,
        app_design_doc_path: str = "", knowledge_provider=None,
        api_spec_path: str = "", reference_cases=None,
        knowledge_available: bool = None, feature_link: str = "") -> Path:
    """Generates test cases for one or more JIRA stories under the same epic,
    writing ONE Excel sheet PER story. `story_keys` may be a single story key
    (str, returns a single Path) or a list of them (returns a list of Path,
    one per story, in the same order) — each story gets its own JIRA fetch +
    LLM call, so per-story cost stays separate.

    With MORE THAN ONE story the per-story sheets are intermediates, written
    under `_internal/per_story/`, and ONE merged `<epic>_TestCases.xlsx` is
    written to `1_testcases/` — that is the file to review and use. Case ids
    are story-prefixed, so merging cannot collide them.

    `app_design_doc_path`, when given, is a human-authored reference for the
    TARGET APPLICATION (real screen names, navigation, environment quirks —
    see utils/app_design_doc.py) folded into the generation prompt so cases
    are written in the app's actual vocabulary rather than invented terms a
    later stage then has to bridge with a guess.

    `knowledge_provider`, when given, is a callable `(query: str) -> str` that
    returns relevant application knowledge for a story — in the pipeline, the
    ledger, via `qa_pipeline.brain.retrieve.for_agent`. It is a CALLABLE and
    not a pre-rendered string on purpose: retrieval is only as good as its
    query, the query that works is the story's own vocabulary (title and
    acceptance criteria), and that vocabulary is not known until the JIRA
    fetch below has run. See `_knowledge_block`.

    `feature_link`, when given (a pasted JIRA Feature/Epic/Story/Test link or
    key — JIRA_FEATURE_LINK in .env, or --feature-link), is walked ONCE via
    `fetch_jira_hierarchy` into ancestor issues (Feature/Epic) and linked
    Zephyr Test issues, each with its own description/comments/attachments,
    and merged into every story's `jira_data["hierarchy"]` below. Independent
    of `story_keys` — the story being generated for and the link that
    supplies its surrounding context need not be the same issue.

    `api_spec_path` is an OpenAPI/Swagger spec (local path or URL) — the API
    counterpart of `figma_url`, and it works two ways:
      - WITH story keys, it is a REFERENCE: the contract is shown alongside the
        JIRA story so API-level criteria get API-level cases (see the skill's
        "STEP 0"). The whole spec is rendered, since which endpoints the story
        touches is the story's business, not this function's to guess.
      - WITHOUT story keys, it is the STORY SOURCE: stories are derived from it,
        one per tag (chunked — see `api_spec_to_stories`), each scoped to its own
        slice of the contract. `epic_key` doubles as the story-key prefix.
    """
    app_design_doc = _app_design_doc.load(app_design_doc_path)
    single_story = isinstance(story_keys, str)
    if single_story:
        story_keys = [story_keys]

    # DROP EMPTY KEYS BEFORE ANYTHING TESTS THE LIST FOR TRUTHINESS.
    #
    # "no story" arrives as "" or [""] from callers that always pass the
    # parameter (the orchestrator builds one argument list for every mode).
    # Normalizing a bare "" produces [""] — a list of length one, which is
    # TRUTHY — so the document-derived branch below was skipped and the
    # resolution loop asked JIRA for the empty issue key, i.e.
    # GET /rest/api/2/issue/ . The list must therefore be cleaned here,
    # before `if not story_keys` is asked anything.
    story_keys = [key for key in story_keys if str(key or "").strip()]

    spec_data = fetch_api_spec(api_spec_path) if api_spec_path else {}
    derived_stories = []
    if spec_data and not story_keys:
        derived_stories = api_spec_to_stories(
            spec_data, story_prefix=(epic_key or "API"), epic_key=epic_key or "API")
        print(f"      Derived {len(derived_stories)} story/stories from "
              f"{spec_data['title']} ({len(spec_data['operations'])} operations, "
              f"max {_MAX_OPS_PER_API_STORY} per story).")
        single_story = False

    figma_data = fetch_figma_context(figma_url, feature_dir) if figma_url else {}

    # ANY ONE INPUT IS ENOUGH TO GENERATE.
    #
    # This used to raise unless a story key or an API spec was given, which
    # made the other inputs strictly decorative: a team with a design doc, a
    # Figma file, a folder of documents or an existing regression sheet — and
    # no tracker item — could not generate at all, even though those are
    # exactly the materials test cases get written from. The story is now the
    # PREFERRED source, not the required one.
    #
    # Only genuinely empty input is refused, and the message lists what would
    # have been accepted rather than naming the two parameters this function
    # happens to take.
    baseline_block = render_reference_baseline(reference_cases or [])
    if not story_keys and not derived_stories:
        sources = {
            "a design document": bool(app_design_doc.strip()),
            "a Figma design": bool(figma_data.get("sections") or figma_data.get("screen_model")),
            "an existing test suite (as a coverage baseline)": bool(baseline_block),
            # WIRED IS NOT THE SAME AS POPULATED.
            #
            # This asked `knowledge_provider is not None`, and the pipeline
            # ALWAYS passes one — so the ledger counted as a source even when
            # empty, this guard could never fire from the orchestrator, and a
            # run with no story, no document and a 0-card ledger announced it
            # was "deriving test cases from ingested project knowledge" and
            # generated from nothing at all. Invented cases are worse than a
            # refusal: they look like work.
            #
            # `knowledge_available` is the caller's answer to "does the ledger
            # actually hold anything?" — the orchestrator knows (it owns the
            # ledger), and the writer cannot find out for itself here because
            # retrieval needs a story's vocabulary that does not exist yet.
            # Defaults to the old behaviour for direct callers that pass a
            # provider and nothing else.
            "ingested project knowledge": (
                knowledge_available if knowledge_available is not None
                else knowledge_provider is not None),
        }
        if not any(sources.values()):
            raise ValueError(
                "Nothing to generate from. Supply at least one of: a JIRA story "
                "key or .xml export, an OpenAPI spec, a design document, a Figma "
                "URL, an existing test-case sheet, or a folder of ingested "
                "documents.")
        story = _synthesize_story(epic_key, sources)
        epic_key = epic_key or story["epic_key"]
        derived_stories = [(story["story_key"], story, [])]
        single_story = False
        print(f"      No story supplied — deriving test cases from "
              f"{', '.join(n for n, ok in sources.items() if ok)}.")
    num_sections = len(figma_data.get("sections", []))

    # Pass 1 — cheap, sequential, no LLM call: resolve each story's real key
    # + JIRA data, and fill in epic_key from the FIRST XML export that
    # supplies one if it wasn't passed explicitly. Kept sequential (not
    # folded into the parallel pass 2 below) because epic_key is mutated
    # here and every later story's output filename depends on its final
    # value — running this part concurrently would make the result depend on
    # thread scheduling order instead of story order.
    resolved = []
    # Spec-derived stories skip the JIRA lookup entirely — they ARE the story
    # data — and each carries only its own slice of the contract.
    for story_key, jira_data, operations in derived_stories:
        # `spec_data` is empty for a story synthesized from documents rather
        # than derived from a spec — `render_api_contract` indexes
        # spec_data['title'] unconditionally, so it must not be called at all
        # in that case, not called and allowed to return "".
        contract = render_api_contract(spec_data, operations) if spec_data else ""
        resolved.append((story_key, jira_data, contract))

    # The whole contract, when a spec accompanies real JIRA stories rather than
    # replacing them. Rendered once, not per story.
    story_api_contract = render_api_contract(spec_data) if spec_data and story_keys else ""

    for story_key in story_keys:
        # A story_key that names an existing local .xml file is read directly
        # instead of hitting the live JIRA API — lets this run against
        # browser-exported XML (Tools > Export > XML) when the pipeline has
        # no JIRA network access, without needing JIRA_API_TOKEN at all.
        story_path = Path(story_key)
        if story_path.suffix.lower() == ".xml" and story_path.is_file():
            jira_data = fetch_jira_story_from_file(story_path)
            # The sheet filename and downstream Test Case ID prefix use the
            # REAL story key parsed out of the XML (e.g. "PROJ-68308"), not the
            # file path — otherwise output files end up named after the
            # local path instead of the issue key.
            story_key = jira_data.get("story_key") or story_path.stem
            epic_key = epic_key or jira_data.get("epic_key") or "EPIC"
        else:
            jira_data = fetch_jira_story(story_key, epic_key)
        resolved.append((story_key, jira_data, story_api_contract))

    # A STORY THAT COULD NOT BE READ IS NOT A STORY. With no JIRA credentials
    # the fetch returns "[PLACEHOLDER] Fetch story title via JIRA MCP", and a
    # failed lookup returns an empty body; either was fed to the model as if it
    # were requirements, and the stage succeeded with invented cases. Fine when
    # something ELSE can supply the requirements; a hard stop when nothing can.
    unreadable = [key for key, data, _c in resolved
                  if str(data.get("title", "")).startswith("[PLACEHOLDER]")
                  or data.get("jira_lookup_failed")]
    other_sources = any((
        figma_data, app_design_doc, baseline_block, knowledge_available, feature_link,
        any(contract for _k, _d, contract in resolved),
    ))
    if unreadable and not other_sources:
        raise ValueError(
            f"Could not read story {', '.join(unreadable)} (JIRA credentials missing or the "
            f"lookup failed) and no other requirements source was given, so any test cases "
            f"would be invented. Fix JIRA_BASE_URL/JIRA_API_TOKEN, pass a local XML export "
            f"via --story, or supply --design-doc / --figma / --api-spec.")
    if unreadable:
        print(f"⚠️  Story {', '.join(unreadable)} could not be read; generating from the other "
              f"supplied sources only.")

    # One hierarchy walk for the whole run (not per story) — the pasted
    # Feature/Epic/Story/Test link describes the surrounding feature once,
    # and every story generated in this invocation shares that same context.
    hierarchy = fetch_jira_hierarchy(feature_link, feature_dir) if feature_link else {}
    # Attachment screenshots are sent to the model as real images (see
    # `screenshots` below), not as text — kept out of the JSON block so the
    # prompt doesn't show the model a bare local file path as if it were
    # readable content.
    hierarchy_screenshots = hierarchy.pop("screenshots", []) if hierarchy else []
    if hierarchy:
        resolved = [
            (story_key, story_with_hierarchy(jira_data, hierarchy, story_key), contract)
            for story_key, jira_data, contract in resolved
        ]

    # Pass 2 — each story's LLM call + Excel write is fully independent (own
    # prompt, own output file, own context-summary line), so these run
    # concurrently. I/O-bound (waiting on the LLM API), not CPU-bound.
    #
    # Per-story try/except (mirrors import_existing_test_cases' as_completed
    # pattern below), not pool.map: pool.map's list(...) call propagates the
    # FIRST exception any single story raises (bad/truncated JSON, zero
    # cases, etc.) and aborts the whole batch — even though other stories'
    # .xlsx files were already successfully written to disk by their own
    # threads before that exception surfaced. A batch of N stories should
    # degrade to N-1 successes + 1 reported failure, not lose everything.
    # Results are collected by SUBMISSION INDEX, not by completion order:
    # this function documents (and single-story callers rely on) returning one
    # path per story in the order given, and `as_completed` returns them in
    # whatever order the provider answered — so a two-story run could hand
    # back story 2's sheet as "the first one".
    story_results = []
    failed_stories = []
    # ONE visible output per run. With several stories each story's own sheet is
    # an intermediate (kept for traceability, hidden under _internal/); the
    # reader opens the single merged sheet written below. A lone story's sheet
    # already is the final output, so it stays where it is.
    per_story_dir = artifact_path(feature_dir, "per_story") if len(resolved) > 1 else None
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_STORIES, len(resolved) or 1)) as pool:
        future_to_story = {
            pool.submit(_generate_one_story, story_key, jira_data, epic_key, figma_data,
                        num_sections, feature_dir, app_design_doc,
                        knowledge_provider, api_contract, baseline_block,
                        hierarchy_screenshots, per_story_dir): (index, story_key)
            for index, (story_key, jira_data, api_contract) in enumerate(resolved)
        }
        completed = {}
        for future in as_completed(future_to_story):
            index, story_key = future_to_story[future]
            try:
                completed[index] = future.result()
            except Exception as exc:
                print(f"⚠️  Story {story_key} failed to generate test cases: {exc}")
                failed_stories.append({"story_key": story_key, "error": str(exc)})
        story_results = [completed[i] for i in sorted(completed)]
    if failed_stories and not story_results:
        raise RuntimeError(
            f"All {len(failed_stories)} story/stories failed to generate test cases: {failed_stories}")
    out_paths = [out_path for out_path, _ in story_results]

    # A failed story must not vanish: out_paths is now SHORTER than story_keys,
    # so a caller zipping the two silently misaligns, and the stage would report
    # success. The return shape is unchanged (callers depend on it); the failure
    # is recorded where an operator and a later stage can find it.
    failed_path = artifact_path(feature_dir, "testcase_writer_failed_stories.json")
    if failed_stories:
        write_json(failed_path, failed_stories)
        failed_keys = ", ".join(f["story_key"] for f in failed_stories)
        print(f"⚠️  {len(failed_stories)} of {len(resolved)} story/stories produced NO test "
              f"cases ({failed_keys}) — returned paths cover only the "
              f"{len(out_paths)} that succeeded. Details: {failed_path}")
        append_context_summary(
            feature_dir, "testcase_writer",
            f"INCOMPLETE: no test cases were generated for {failed_keys} "
            f"(see testcase_writer_failed_stories.json). Re-run those stories before review.")
    elif failed_path.exists():
        write_json(failed_path, [])  # this run checked and found none

    # Cross-story near-duplicate check: each story's LLM call has NO
    # visibility into any other story's output, so nothing stops two
    # stories under the same epic from independently generating near-
    # identical coverage for shared UI (e.g. both writing "verify login
    # with valid credentials"). Flagged for human review at the existing
    # review gate rather than auto-merged/deleted — a human, not a fuzzy
    # string match, should decide whether two similar-looking cases are
    # actually redundant or cover genuinely different acceptance criteria.
    all_cases = [c for _, cases in story_results for c in cases]
    if len(resolved) > 1:
        duplicates = _detect_near_duplicate_cases(all_cases)
        if duplicates:
            write_json(artifact_path(feature_dir, "testcase_writer_duplicates.json"), duplicates)
            print(f"      ⚠️  {len(duplicates)} likely duplicate/near-duplicate case pair(s) "
                  f"across stories — see {artifact_path(feature_dir, 'testcase_writer_duplicates.json')}")
            append_context_summary(
                feature_dir, "testcase_writer",
                f"{len(duplicates)} likely duplicate/near-duplicate case pair(s) detected across "
                f"stories — see testcase_writer_duplicates.json. Review before marking Reviewed."
            )

    if len(resolved) > 1:
        merged = artifact_path(feature_dir, f"{epic_key}_TestCases.xlsx")
        _write_excel(all_cases, merged, epic_key)
        print(f"      Final sheet: {merged} ({len(all_cases)} cases from "
              f"{len(story_results)} stories; per-story sheets are in "
              f"{artifact_path(feature_dir, 'per_story')})")

    return out_paths[0] if single_story else out_paths


#: Two cases' normalized "name" text at or above this similarity ratio
#: (difflib.SequenceMatcher, 0-1 scale) are flagged as a likely duplicate.
#: 0.85 catches near-identical phrasing ("Verify X" vs "Verify the X") while
#: staying well above the ratio two genuinely different but topically
#: related cases (e.g. "Verify login" vs "Verify login error message")
#: would score.
_DUPLICATE_SIMILARITY_THRESHOLD = 0.85


def _detect_near_duplicate_cases(cases: list) -> list:
    """Deterministic, non-LLM pairwise check — no extra API cost — across
    ALL cases generated for this feature (every story). O(n^2) but n is a
    feature's total case count (tens to low hundreds), not a concern at
    that scale."""
    import difflib

    def normalize(text):
        return re.sub(r"\s+", " ", (text or "").strip().lower())

    duplicates = []
    for i in range(len(cases)):
        name_i = normalize(cases[i].get("name"))
        if not name_i:
            continue
        for j in range(i + 1, len(cases)):
            if cases[i].get("_story_key") == cases[j].get("_story_key"):
                continue  # within-story duplicates are the model's own call to avoid, not flagged here
            name_j = normalize(cases[j].get("name"))
            if not name_j:
                continue
            ratio = difflib.SequenceMatcher(None, name_i, name_j).ratio()
            if ratio >= _DUPLICATE_SIMILARITY_THRESHOLD:
                duplicates.append({
                    "case_a": cases[i].get("id"), "case_b": cases[j].get("id"),
                    "name_a": cases[i].get("name"), "name_b": cases[j].get("name"),
                    "similarity": round(ratio, 2),
                })
    return duplicates


#: How much of a story's description feeds the ledger query. The description
#: is the richest source of the app's real vocabulary, but a long one is
#: mostly boilerplate (definition of done, links, sign-off tables) whose
#: tokens dilute the overlap score without describing the feature.
_KNOWLEDGE_QUERY_DESC_CHARS = 2000


def _knowledge_block(story_key: str, jira_data: dict, knowledge_provider) -> str:
    """Application knowledge relevant to THIS story, fetched through a callable.

    THE QUERY IS THE WHOLE POINT. The orchestrator used to render this block
    itself and query with `f"{story_key} {epic_key}"` — two identifiers, e.g.
    "PROJ-68308 EPIC". `brain.retrieve._tokens` case-folds, splits on
    non-alphanumerics and drops anything under four characters, so that query
    reduced to roughly {"68308", "epic"}, matched no card's text, and scored
    every card at zero. The block came back all but empty, and was then never
    passed to this agent anyway.

    Querying with the story's title, description and acceptance criteria gives
    the scorer the feature's actual vocabulary to overlap against, which is
    what `retrieve.AGENT_WEIGHTS["testcase_writer"]` was written to serve:
    glossary and rule cards weighted above elements, because a test case needs
    to know what testers call a thing and which rules constrain it, not which
    CSS selector reaches it.

    Failure is non-fatal. Knowledge here is enrichment — a story generated
    without it is worse-grounded but still valid — so a retrieval error is
    reported and swallowed rather than losing the story's whole LLM call.
    """
    if knowledge_provider is None:
        return ""

    criteria = jira_data.get("acceptance_criteria") or []
    query = " ".join(part for part in (
        jira_data.get("title", ""),
        (jira_data.get("description", "") or "")[:_KNOWLEDGE_QUERY_DESC_CHARS],
        " ".join(str(c) for c in criteria),
    ) if part)

    try:
        return (knowledge_provider(query) or "").strip()
    except Exception as exc:
        print(f"      ⚠️  [{story_key}] application-knowledge retrieval failed "
              f"({exc}) — generating from JIRA and the design doc alone.")
        return ""


def _generate_one_story(story_key: str, jira_data: dict, epic_key: str, figma_data: dict,
                         num_sections: int, feature_dir: Path, app_design_doc: str = "",
                         knowledge_provider=None, api_contract: str = "",
                         baseline_block: str = "", hierarchy_screenshots=(),
                         sheet_dir: Path = None) -> tuple[Path, list]:
    # Where THIS story's sheet and story_context are written. Defaults to the
    # run folder (a single story's sheet IS the final output); a multi-story
    # run passes a hidden per-story folder so only the merged sheet is visible.
    sheet_dir = sheet_dir or feature_dir
    relevance_note = (
        f"\nNOTE: the Figma Context above contains {num_sections} distinct "
        "screens/sections from ONE shared design file, not all necessarily "
        "related to this story. Read the JIRA Story's title/description/"
        "acceptance criteria first, then use ONLY the section(s) that are "
        "actually relevant to THIS story when writing test cases — ignore "
        "sections describing unrelated screens/features entirely, don't "
        "write test cases for them just because they're present in the file."
        if num_sections > 1 else ""
    )

    # Real screenshots the design doc had embedded — actually shown to the
    # model now (see utils/llm_client._prepare_images), not just saved to
    # disk unseen. Filter out the "(unsaved — no feature_dir passed)"
    # placeholder strings _extract_docx_images emits when it ran WITHOUT
    # a feature_dir — this call always passes one, so that placeholder
    # shouldn't appear here, but a stale/pre-existing figma_data payload
    # from an older run of this pipeline still could.
    screenshots = [s for s in figma_data.get("screenshots", []) if Path(s).is_file()]
    screenshots += [s for s in hierarchy_screenshots if Path(s).is_file()]
    vision_note = (
        f"\n{len(screenshots)} screenshot(s) from the design file and/or JIRA "
        "attachments are attached as images above this text — use them as real "
        "visual reference for UI structure/labels/layout, not just the text "
        "sections below." if screenshots else ""
    )

    # The two reference blocks are labelled exactly as the skill's STEP 0 names
    # them ("Figma Context", "API Contract") — that section decides UI vs API
    # cases by which of them is present, so the labels are load-bearing.
    # Omitted entirely rather than sent empty: an empty "API Contract:" heading
    # reads as a contract with no operations in it.
    # Only the design sections that relate to THIS story (all of them when none
    # can be matched) — see testcase_prompt.relevant_design_data.
    story_figma = relevant_design_data(figma_data, jira_data) if figma_data else figma_data
    if story_figma is not figma_data:
        relevance_note = (
            f"\nNOTE: the Figma Context above was pre-filtered to the "
            f"{len(story_figma.get('sections', []))} section(s) that relate to this "
            f"story; the shared design file has {num_sections} in all. Use them as the "
            f"visual/structural reference for this story's test cases.")
    figma_block = (f"\nFigma Context: {compact_json(story_figma)}\n{relevance_note}{vision_note}\n"
                   if figma_data else "")
    api_block = (f"\nAPI Contract (OpenAPI specification for the service under test — "
                 f"write API cases against these operations, per STEP 0):\n{api_contract}\n"
                 if api_contract else "")

    user_prompt = f"""JIRA Story: {compact_json(story_for_prompt(jira_data))}
{figma_block}{api_block}
Generate the full test case set now."""

    raw = call_agent(
        agent_name="testcase_writer",
        user_prompt=user_prompt,
        feature_dir=feature_dir,
        max_tokens=16000,
        images=screenshots,
        # Two standing-knowledge blocks, design doc first and ledger second.
        # The ledger's own header (see `brain.retrieve.render`) states that a
        # fact it sourced 'confirmed', 'recorded' or 'crawled' was OBSERVED on
        # the running app and overrules design-doc prose, so the block that
        # wins a disagreement is also the one the model reads last.
        extra_system="\n\n".join(part for part in (
            _app_design_doc.as_extra_system(app_design_doc),
            _knowledge_block(story_key, jira_data, knowledge_provider),
            # LAST, so it is the nearest instruction to the model's own turn.
            # This block's rule is subtractive ("do not write these"), and a
            # subtractive rule read before two blocks of positive material is
            # the one most easily talked out of.
            baseline_block,
        ) if part.strip()),
    )

    cases = parse_json_response(raw, feature_dir, "testcase_writer")

    # Shape-check before touching the sheet. A model that returns
    # {"test_cases": [...]} instead of a bare array would otherwise make
    # the loop below iterate over dict KEYS and raise a confusing
    # TypeError on `"test_cases"["_story_key"] = ...`; and an empty list
    # would silently produce a header-only sheet that sails through the
    # human review gate with nothing actually reviewed.
    if isinstance(cases, dict):
        for key in ("test_cases", "cases", "testCases", "items", "data"):
            if isinstance(cases.get(key), list):
                cases = cases[key]
                break
        else:
            raise ValueError(
                f"[{story_key}] testcase_writer returned a JSON object with no "
                f"test-case array; keys were {list(cases)}."
            )
    if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
        raise ValueError(
            f"[{story_key}] testcase_writer: expected a JSON array of case "
            f"objects, got {type(cases).__name__}."
        )
    if not cases:
        raise ValueError(
            f"[{story_key}] testcase_writer returned zero test cases — "
            f"refusing to write an empty sheet."
        )

    # Scope each case's id to its story (PROJ-68308-TC-001, not bare
    # TC-001). Every story's model call independently starts numbering
    # from TC-001, so across a multi-story feature the SAME id gets
    # reused by unrelated cases — confirmed in practice: 4 stories x ~20
    # cases each collapsed to only 23 unique ids out of 80. Everything
    # downstream (Planner, Coding Agent, Reviewer, Self-Healer) joins on
    # this id, so a collision there silently overwrites one story's
    # mapping with another's.
    # Ids must be unique WITHIN the story too, not just across stories. A
    # model asked for twenty cases occasionally repeats "TC-004" or restarts
    # numbering partway through, and every downstream stage joins on this
    # string — a collision silently maps two cases to one plan, one file and
    # one bug. Renumbered deterministically from the first duplicate onward,
    # preserving the model's own ids where they are already unique so a
    # re-run of an unchanged sheet is stable.
    used = set()
    for position, c in enumerate(cases, start=1):
        c["_story_key"] = story_key
        raw_id = str(c.get("id") or "").strip() or f"TC-{position:03d}"
        if raw_id in used:
            candidate = f"TC-{position:03d}"
            while candidate in used:
                position += 1
                candidate = f"TC-{position:03d}"
            print(f"      ⚠️  Duplicate test case id {raw_id!r} from the model — "
                  f"renumbered to {candidate!r}.")
            raw_id = candidate
        used.add(raw_id)
        c["id"] = f"{story_key}-{raw_id}"

    out_path = artifact_path(sheet_dir, f"{epic_key}_{story_key}_TestCases.xlsx")
    _write_excel(cases, out_path, epic_key)

    # The story's acceptance criteria, persisted next to the sheet.
    #
    # This is what lets the coverage gate actually run. `coverage_check`
    # compares the generated cases against the story's criteria and reports
    # any criterion nothing covers — the one check a human reviewer cannot
    # perform by reading forty prose cases — but it needs the criteria, and
    # they were previously parsed here, used in the prompt, and then dropped.
    # Written per story (not one shared file) because stories generate
    # concurrently and would otherwise race on the same path.
    write_json(artifact_path(sheet_dir, f"story_context_{story_key}.json"), {
        "story_key": story_key,
        "epic_key": epic_key,
        "title": jira_data.get("title", ""),
        "acceptance_criteria": jira_data.get("acceptance_criteria") or [],
    })

    # Write a SHORT distilled note for downstream agents instead of making
    # them re-read/re-parse the full JIRA+Figma payload again.
    types = {}
    for c in cases:
        types[c.get("testing_type", "?")] = types.get(c.get("testing_type", "?"), 0) + 1
    append_context_summary(
        feature_dir, "testcase_writer",
        f"Generated {len(cases)} test cases for {story_key} (epic {epic_key}). "
        f"Testing type split: {types}. Sheet: {out_path.name}."
    )

    return out_path, cases


def _cli():
    """Standalone entry point — run this agent on its own without going
    through the orchestrator, e.g. to regenerate one story's sheet in
    isolation. Every input is a real CLI flag rather than a hardcoded
    variable, so a one-off run needs no file edits."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--story", nargs="+", default=[],
                         help="One or more JIRA story keys, or local .xml export paths. "
                              "With --import-file, exactly one value is used purely as "
                              "the Test Case ID prefix/output filename, not fetched from "
                              "JIRA. Not used (and not required) with --import-folder — "
                              "each file's story key is derived from its filename.")
    parser.add_argument("--epic", default="", help="JIRA epic key.")
    parser.add_argument("--figma", default="", help="Figma URL or local .docx/.xlsx/.pdf path.")
    parser.add_argument("--api-spec", default="",
                         help="OpenAPI/Swagger JSON spec (local path or URL) for the service "
                              "under test. WITH --story it is an extra reference alongside "
                              "the story, so API-level criteria get API-level cases. WITHOUT "
                              "--story it becomes the source: stories are derived from the "
                              "spec, one per tag (split further when a tag has more than "
                              f"{_MAX_OPS_PER_API_STORY} operations), and --epic supplies the "
                              "story-key prefix.")
    import_group = parser.add_mutually_exclusive_group()
    import_group.add_argument("--import-file", default="",
                         help="Path to an existing test case file (.xlsx/.xlsm/.csv — a legacy .xls must be re-saved as .xlsx first) to "
                              "normalize into this pipeline's format, instead of "
                              "generating new cases from a JIRA story.")
    import_group.add_argument("--import-from-jira", default="",
                         help="Zephyr Scale test cycle id, or comma-separated test case "
                              "keys (e.g. 'PROJ-T101,PROJ-T102'), to pull existing manual test "
                              "cases from JIRA/Zephyr Scale and normalize them into this "
                              "pipeline's format, instead of generating new cases from a "
                              "JIRA story. Requires ZEPHYR_API_TOKEN — see .env.example.")
    import_group.add_argument("--import-folder", default="",
                         help="Path to a folder of existing test case files (.xlsx/.xlsm/"
                              ".csv; a legacy .xls must be re-saved as .xlsx first), "
                              "ONE FILE PER STORY, to import and normalize in one "
                              "run. Filenames should be '<EPIC_KEY>_<STORY_KEY>.xlsx' "
                              "(e.g. 'PROJ-1000_PROJ-1234.xlsx'), or just '<STORY_KEY>.xlsx' "
                              "if --epic is given as the default for the whole batch. "
                              "Each file gets its own run folder under runs/_adhoc/ "
                              "(<epic>_<story>) — --story/--feature-dir are ignored in "
                              "this mode.")
    parser.add_argument("--feature-dir", default="",
                         help="Defaults to runs/_adhoc/<epic>_<story1>_<story2>... "
                              "Ignored with --import-folder (each file gets its own).")
    args = parser.parse_args()

    if args.import_folder:
        results = import_existing_test_cases_folder(
            Path(args.import_folder), args.epic, Path("runs") / "_adhoc", jira_source="")
        print("   Token usage logged under each story's own "
              "runs/_adhoc/<epic>_<story>/_internal/token_log.json")
        # import_existing_test_cases_folder already prints per-file progress
        # and an ok/failed summary, so nothing is re-printed here — but its
        # results were previously discarded entirely, which meant a folder
        # import where EVERY file failed still exited 0. A non-zero exit is
        # what lets this be driven from a script or CI step.
        failed = [r for r in results if r.get("status") != "ok"]
        if failed:
            raise SystemExit(f"{len(failed)}/{len(results)} file(s) failed to import "
                             f"(see the per-file errors above).")
        return

    if not args.story and not args.api_spec:
        parser.error("--story is required unless using --import-folder or --api-spec.")
    if args.api_spec and not args.story and not args.epic:
        parser.error("--epic is required with --api-spec and no --story — it is the "
                      "prefix for the story keys derived from the spec.")

    feature_dir = Path(args.feature_dir) if args.feature_dir else \
        Path("runs") / "_adhoc" / (f"{args.epic}_{'_'.join(args.story)}" if args.story else args.epic)
    feature_dir.mkdir(parents=True, exist_ok=True)

    if args.import_file or args.import_from_jira:
        if len(args.story) != 1:
            parser.error("--story must be exactly one value when using --import-file/"
                          "--import-from-jira (used as the Test Case ID prefix, not a "
                          "JIRA lookup).")
        out_path = import_existing_test_cases(
            Path(args.import_file) if args.import_file else None,
            args.story[0], args.epic, feature_dir,
            jira_source=args.import_from_jira,
        )
        print(f"\n✅ Imported and normalized test cases written to {out_path}")
        print(f"   Token usage logged to: {artifact_path(feature_dir, 'token_log.json')}")
        return

    if not args.story:
        story_keys = []           # spec-as-source: run() derives the stories
    elif len(args.story) > 1:
        story_keys = args.story
    else:
        story_keys = args.story[0]
    single_story = isinstance(story_keys, str)

    out_paths = run(story_keys, args.epic, args.figma, feature_dir,
                    api_spec_path=args.api_spec)
    if single_story:
        out_paths = [out_paths]

    print(f"\n✅ {len(out_paths)} test case sheet(s) written:")
    for p in out_paths:
        print(f"   - {p}")
    print(f"   Token usage logged to: {artifact_path(feature_dir, 'token_log.json')}")


if __name__ == "__main__":
    _cli()
