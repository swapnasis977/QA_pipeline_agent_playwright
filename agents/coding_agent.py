"""
Agent 3: Coding Agent — UI / API / E2E
IN:  Test case Excel sheet + JIRA test cycle (filter to in-cycle cases),
     Automation Planner doc, existing coding conventions, past learnings.
OUT: Working automation code files (Playwright), one file per feature/case group,
     plus a failures.json log for cases the agent couldn't confidently generate.

NOTE on MCP: `fetch_test_cycle_keys` is a stub for JIRA MCP (Xray/Zephyr/native —
whichever you use, per Open Decision #3). Swap in the real call; everything else
(reading the sheet, prompting, writing code files) is functional.
"""

import hashlib
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents import pom_builder
from agents.artifact_io import (
    normalize_target_file,
    read_text_file,
    resolve_within,
    write_json,
)

# Moved to agents/coding_cases.py; re-exported so existing imports keep working.
from agents.coding_cases import (  # noqa: F401
    _MAX_CASES_PER_CALL,
    _MIN_REAL_CONTENT_LENGTH,
    _PLACEHOLDER_CONTENT_MARKERS,
    _TEST_SUFFIX_RE,
    _TEST_TITLE_RE,
    _UNASSIGNED_BUCKET,
    _group_cases_by_target_file,
    _insert_part_suffix,
    _is_test_spec,
    _load_planner_plans,
    _max_cases_per_call,
    _missing_title_prefixes,
    _part_label,
    fetch_test_cycle_keys,
    filter_automatable_cases,
    read_test_case_sheet,
)

# Moved to agents/coding_dom.py; re-exported so existing imports keep working.
from agents.coding_dom import (  # noqa: F401
    _DOM_CONFIDENCE_THRESHOLD,
    _MIN_USEFUL_ELEMENTS,
    _POM_NAME_RE,
    _UNUSABLE_DOM_QUALITY,
    _cases_needing_reinspection,
    _dom_snapshot_is_unusable,
    _plan_navigation_keywords,
    _pom_navigation_hints,
    _reinspect_cases,
)

from agents import coding_prompt

# Moved to agents/coding_repair.py; re-exported so existing imports keep working.
from agents.coding_repair import (  # noqa: F401
    _MAX_PRIOR_CHARS,
    _MAX_PROVEN_LOCATOR_HINTS,
    _PER_RUN_DIAGNOSTICS,
    _clear_stale_diagnostics,
    _inline_locators_in_spec,
    _prior_output_block,
    _proven_locator_hints,
    _repair_unverified_locators,
    _snapshot_elements_for_cases,
    _verify_file_locators,
)

# Moved to agents/ts_check.py; re-exported so existing imports keep working.
from agents.ts_check import (  # noqa: F401
    _POM_PATH_MARKERS,
    _SPAWN_RETRY_DELAY_SEC,
    _SYNTAX_FAIL,
    _SYNTAX_PASS,
    _SYNTAX_SKIPPED,
    _TS_MISSING_PROPERTY,
    _TS_PROPERTY_DECL,
    _TSCONFIG_OPTION_TO_FLAG,
    _TYPECHECK_PROJECT_SEQ,
    _check_syntax,
    _check_syntax_status,
    _run_with_retry,
    _tsc_flags_from_tsconfig,
    _typecheck_project,
    suggest_property_owner,
)
from utils import app_design_doc as app_design_doc_lib
from utils import llm_client, locator_verifier, route_map
from utils.error_classify import classify_error_kind
from utils.llm_client import append_context_summary, call_agent, parse_json_response
from utils.run_layout import artifact_path

# Standing instructions live in skills/coding_agent.skill.md (cached).
# Coding conventions are passed via `extra_system` (small, run-specific,
# not cached) rather than baked into the skill file, since they vary by repo.


#: Chunks are independent LLM calls (no cross-chunk reasoning), so they run
#: concurrently — this is I/O-bound (waiting on the LLM API), not CPU-bound,
#: so a small thread pool cuts wall-clock time without adding real cost.
#: Capped conservatively to stay well under most providers' per-minute
#: rate limits on a single API key.
#:
#: Env-tunable (same knob as automation_planner._MAX_PARALLEL_BATCHES) because
#: the real ceiling is the ACCOUNT's per-minute limit, not anything this code
#: knows: a shared corporate key needs a lower number, a dedicated one can
#: take more. QA_MAX_PARALLEL_CHUNKS=1 restores fully sequential generation
#: for debugging a provider issue.
try:
    _MAX_PARALLEL_CHUNKS = max(1, int(os.environ.get("QA_MAX_PARALLEL_CHUNKS", 4)))
except (TypeError, ValueError):
    _MAX_PARALLEL_CHUNKS = 4


#: Owned by `utils.recording_parser.generate`, which builds exactly
#: `repo_dir / "src" / "pages" / "recorded"`. Every file under it is derived
#: deterministically from a human `playwright codegen` recording — the
#: highest-confidence source this pipeline has, because a person demonstrated
#: the flow and the locators are ground truth by construction rather than by
#: inference.
_RECORDING_OWNED_DIR = "src/pages/recorded/"


def is_recording_owned(rel_path: str) -> bool:
    """Whether `rel_path` belongs to the recording parser rather than to the
    Coding Agent.

    Observed on a real run: generating VENDOR-TC-001 emitted
    `src/pages/recorded/RecordedSampleIdentManagePage.ts` among its files and
    replaced it with a version carrying only what that one case needed — 104
    recorded locator properties down to 11, and 36 `tsc` errors in other specs
    that referenced the missing ones. The model was not misbehaving: it was
    asked for a page object and wrote a complete one. What it cannot infer is
    that this file already has an author.

    Checked in code rather than left to the skill for the same reason path
    confinement is: an instruction is not enforcement.
    """
    normalized = str(rel_path or "").replace("\\", "/").lstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.startswith(_RECORDING_OWNED_DIR)


def run(sheet_path: Path, cycle_id: str, planner_doc_path: Path,
        conventions_path: Path, feature_dir: Path, repo_output_dir: Path,
        existing_pom_summary: str = "", include_manual: bool = False,
        skip_locator_repair: bool = False, app_design_doc_path: str = "",
        repair_prompt: str = "", prior_files: dict = None,
        only_case_ids=None) -> dict:
    """`only_case_ids`, when given, restricts generation to those cases. The
    orchestrator uses it to repair only the files a gate rejected instead of
    regenerating every case again.

    `repair_prompt`, when given, is a verifier's diagnosis of what was
    wrong with THIS agent's previous output for the same cases (see
    qa_pipeline/verifiers/repair_loop.py). It is threaded into the system
    prompt so a retry carries the gate's finding instead of re-issuing the
    identical call: without it the orchestrator's repair loop re-generated
    every file from an unchanged prompt and earned the identical failure,
    up to max_repairs times, which is the exact waste `adapters.replan`
    already exists to prevent on the Planner side.

    `skip_locator_repair` turns off the post-generation locator
    repair pass (see the block after the chunk loop). The repair costs
    one extra LLM call per file whose locators the live snapshot didn't
    corroborate, so it is worth disabling for a dry run or when no live
    environment is configured (in which case there is no snapshot and
    the pass is already a no-op).

    `app_design_doc_path`, when given, is a human-authored reference for the
    TARGET APPLICATION — real screen names, navigation structure, environment
    quirks (see utils/app_design_doc.py) — the same file the Test Case Writer
    reads, so both agents ground their output in the same stated facts about
    the app instead of each independently guessing or missing them.
    """
    sheet_cases = read_test_case_sheet(sheet_path)
    all_cases = filter_automatable_cases(sheet_cases, include_manual)
    if sheet_cases and not all_cases:
        print(f"      All {len(sheet_cases)} case(s) on '{sheet_path.name}' are marked "
              f"'Manual' (Execution Type) — nothing to automate. Pass --include-manual "
              f"to `qa-pipeline run` to automate them anyway.")

    cycle_keys = fetch_test_cycle_keys(cycle_id)
    cases = (
        [c for c in all_cases if c.get("Test Case ID") in cycle_keys]
        if cycle_keys else all_cases
    )
    if only_case_ids:
        wanted_ids = set(only_case_ids)
        cases = [c for c in cases if c.get("Test Case ID") in wanted_ids]

    # The sheet's "Test Case ID" holds pipeline ids (TC-001...) while Zephyr
    # returns its own keys (e.g. PROJ-T412). Unless someone has pasted Zephyr
    # keys into the sheet at the review gate, those namespaces don't overlap
    # and the filter quietly yields nothing — which would otherwise look
    # identical to "the model had no work to do", after paying for the call.
    if cycle_keys and not cases:
        raise ValueError(
            f"Test cycle '{cycle_id}' returned {len(cycle_keys)} Zephyr key(s) "
            f"{cycle_keys[:5]}, but none match the sheet's 'Test Case ID' values "
            f"{[c.get('Test Case ID') for c in all_cases][:5]}. These are different "
            f"ID namespaces, so retrying will not help. Either drop --cycle, or put "
            f"the matching Zephyr keys in the sheet's 'Test Case ID' column."
        )

    # read_text_file returns "" for a missing path, a directory, or an
    # unreadable file, so both fall back to their placeholder text. That
    # covers Path("") — which normalizes to Path("."), a DIRECTORY, and so
    # would have raised on a bare read_text() — which is exactly what
    # the orchestrator passes when a profile declares no conventions.md.
    planner_doc_text = (read_text_file(planner_doc_path)
                        or "(none — infer from test case data)")
    conventions = (read_text_file(conventions_path)
                   or "(none — use clean Playwright Test defaults)")
    app_design_doc = app_design_doc_lib.load(app_design_doc_path)

    # Conventions and the POM summary go into `extra_system`, NOT the user
    # prompt — they're repo-wide constants, identical for every case, so they
    # belong in the system block rather than duplicated into each user message.
    # They also sit inside the Anthropic cache prefix (see _call_anthropic),
    # so re-runs within the cache window bill them at cache-read rates.
    # The POM summary matters: the skill tells the model to reuse existing
    # page objects, and the Planner only emits bare names like
    # "LoginPage.submit" with no import path or signature — without the
    # summary the model has to invent them, which is what it's told not to do.
    # Split in two: `extra_system` holds only what is byte-identical on every
    # call for this run (so it sits before the Anthropic cache breakpoint in
    # _call_anthropic and actually gets cache-read on later chunks/repairs),
    # while `dynamic_context` holds what grows or changes within the SAME
    # run (locator_memory.json gains entries as the Self-Healer confirms
    # fixes, existing_pom_summary gains entries as new page objects get
    # written) or varies per retry (repair_prompt). Any of those sitting in
    # the cached prefix breaks the exact-byte-match the cache requires, so
    # every call pays full cache_creation again instead of a cache_read —
    # observed in production as near-100% cache misses on coding_agent.
    # dynamic_context is folded into the per-call USER prompt instead (see
    # _call_and_parse), where its drift costs nothing extra: it's never
    # cached either way.
    extra_system = f"Existing coding conventions for this repo:\n{conventions}"
    if app_design_doc:
        extra_system += "\n\n" + app_design_doc_lib.as_extra_system(app_design_doc)
    dynamic_context = ""

    # --include-manual-cases filtered these rows IN; say so, or the model
    # keeps declining them for the very reason the human just overrode.
    #
    # Observed on a real run: all 5 cases came back unresolved, every reason
    # opening with "This test case is marked Execution Type = 'Manual'" —
    # even though the operator had explicitly asked for those rows to be
    # automated. filter_automatable_cases acted on the flag, but nothing in
    # the prompt did, so the sheet's Manual marking (still visible in the
    # case JSON) read as an instruction not to generate code.
    if include_manual:
        extra_system += (
            "\n\nNOTE ON 'Execution Type: Manual': the operator ran this stage with "
            "--include-manual-cases, deliberately overriding that marking. Do NOT cite "
            "'this case is marked Manual' as a reason to decline — that decision has "
            "already been made and reversed for this run.\n"
            "Automate whatever part of each case a browser genuinely CAN drive, and be "
            "specific about the rest: if a case depends on something outside the browser "
            "(physical capture hardware, a device SDK, a backend-only trigger), generate "
            "the automatable portion — navigation, form entry, and every UI assertion "
            "reachable before that dependency — and name the exact step where automation "
            "must stop and why. A partial test that reliably verifies the first six steps "
            "is worth far more than no test at all. Only decline outright if NOTHING in "
            "the case is browser-drivable.")

    # Locators this pipeline has ALREADY PROVEN correct on this feature, from
    # locator_memory.json — every entry is a (old -> new) pair the
    # Self-Healer applied and the Executor then confirmed passing (a pair
    # that did NOT fix its case is blocklisted in locator_memory_failed.json
    # and excluded here, so this only ever carries confirmed-good fixes).
    #
    # This store existed before but only the SELF-HEALER read it, so the
    # pipeline learned a locator the hard way — a failed run, a review, an
    # LLM heal, a verification re-run — and then never told the GENERATOR.
    # The next regeneration re-guessed the same selector and re-earned the
    # same failure. Feeding it in here is what closes that loop.
    proven = _proven_locator_hints(feature_dir)
    if proven:
        dynamic_context += (
            "\n\nLocators already CONFIRMED WORKING on this feature by a previous run "
            "(each was applied and then verified by a passing test — prefer these "
            "verbatim over anything you would otherwise write for the same element, "
            "and never revert one to a selector listed as broken):\n" + proven)
        print(f"      Reusing {proven.count(chr(10)) + 1} previously-confirmed locator(s) "
              f"from locator_memory.json.")
    if existing_pom_summary:
        # Bounded: this block is per-repo and could otherwise be arbitrarily large.
        dynamic_context += ("\n\nExisting page objects / helpers available for reuse "
                         "(prefer these over new locators):\n"
                         + existing_pom_summary[:8000])

    # A retry's diagnosis goes LAST, so it is the most recent thing in the
    # dynamic context and cannot be diluted by the standing repo context above.
    if repair_prompt:
        dynamic_context += (
            "\n\n=== CORRECTIONS REQUIRED ON YOUR PREVIOUS OUTPUT ===\n"
            + repair_prompt
            + "\n\nRegenerate the files for these cases with those problems fixed. "
              "Keep everything that was already correct — do not restructure a "
              "file, rename a test, or change an assertion that was not named "
              "above.")
        print(f"      Regenerating with {repair_prompt.count(chr(10)) + 1} line(s) "
              f"of verifier feedback from the previous attempt.")

    # Sequential and BEFORE chunking/threading starts (see
    # _reinspect_cases docstring) — dom_inspector's browser
    # session is a shared singleton, so this must not overlap with the
    # ThreadPoolExecutor below.
    plans_by_id = {p["test_case_id"]: p for p in _load_planner_plans(planner_doc_path)
                   if p.get("test_case_id")}
    reinspect_ids = _cases_needing_reinspection(planner_doc_path)
    dom_snapshots = _reinspect_cases(
        cases, reinspect_ids, existing_pom_summary, plans_by_id)

    groups = _group_cases_by_target_file(cases, planner_doc_path)

    # Even a single target_file's cases can still overflow max_tokens — a
    # group of 11 cases already truncated at 16000 tokens, since full
    # Playwright code (imports, POM calls, assertions) per case runs far
    # heavier than the Planner's JSON. Sub-chunk further, capped small (see
    # _max_cases_per_call — the cap loosens when there's a big POM summary
    # to reuse from, since that shrinks how much NEW code each case needs).
    max_cases_per_call = _max_cases_per_call(existing_pom_summary)
    chunks = []  # list of (label, case_sublist, is_split)
    for target_file, group_cases in groups.items():
        if len(group_cases) <= max_cases_per_call:
            chunks.append((target_file, group_cases, False))
        else:
            for i in range(0, len(group_cases), max_cases_per_call):
                chunks.append((target_file, group_cases[i:i + max_cases_per_call], True))

    if len(chunks) > 1:
        print(f"      Splitting {len(cases)} case(s) into {len(chunks)} generation "
              f"call(s) to avoid truncating large generations.")
    else:
        print(f"      Generating code for {len(cases)} case(s)...")

    # Model-supplied paths are untrusted input. `repo_output_dir / "/etc/x"`
    # DISCARDS repo_output_dir entirely (pathlib absolute-path semantics), and
    # "../.." escapes it — either way mkdir(parents=True)+write_text would
    # create files outside the target repo. Resolve and confine instead.
    repo_output_dir.mkdir(parents=True, exist_ok=True)
    repo_root = repo_output_dir.resolve()

    #: `accepted_files` holds ONLY the files that actually survived every gate
    #: and are on disk right now — it is what run() returns as "files".
    #: `all_files` (the model's raw claim) must NOT be returned: a rejected
    #: file (placeholder content, path escape, failed syntax check → unlinked)
    #: was still in that list, so the Executor was handed test paths that no
    #: longer exist ("No tests found"), the Reviewer reviewed content never
    #: written, and the Self-Healer tried to patch a missing file — which
    #: _apply_patch raises on, killing the run.
    all_files, all_unresolved, written, rejected = [], [], [], []
    #: Files whose locators the live snapshot did NOT corroborate. Recorded
    #: during the parallel phase (cheap, no LLM) and repaired sequentially
    #: AFTER it — a repair is an LLM call, and doing it inside state_lock
    #: would serialize every chunk behind it.
    unverified_locators = []
    #: Chunks lost to an auth/quota/rate-limit failure that OUTLIVED the
    #: per-call retry budget. Tracked separately from ordinary unresolved
    #: cases because the remedy is different in kind: a content failure means
    #: re-prompting, while these cases have perfectly good inputs and simply
    #: never reached the provider — they are recoverable by re-running the
    #: stage once the account issue is fixed, and must not be mistaken for
    #: "the model couldn't automate this case".
    infra_failed_chunks = []
    #: Specs that inline their own locators instead of using a page object.
    #: Structural, not cosmetic — see _inline_locators_in_spec.
    inline_locator_warnings = []
    accepted_files = []
    title_prefix_warnings = []
    overwrite_warnings = []
    #: rel_norm path -> content already written THIS RUN, across every chunk
    #: (not just the current target_file's own group). _insert_part_suffix
    #: only renames collisions on a target_file's OWN test spec; a shared
    #: POM/helper file (e.g. tests/pages/ReportsPage.ts) can be emitted by
    #: two DIFFERENT target_file groups running concurrently, and without
    #: this tracker the second write silently discards the first's content
    #: with no signal anywhere.
    written_content = {}
    split_part_counters = {}
    #: Spec paths (normalized, lower-cased) already claimed by a chunk of THIS
    #: run. Two chunks (or the two halves of a truncation retry) returning the
    #: same spec path used to overwrite each other, so a case's test vanished
    #: with only an overwrite warning to show for it.
    used_spec_paths = set()
    state_lock = threading.Lock()
    failures_path = artifact_path(feature_dir, "coding_agent_unresolved.json")

    # PAGE OBJECTS ARE BUILT FROM THE PLAN, NOT WRITTEN BY THE MODEL. See
    # agents/pom_builder.py: chunk-by-chunk page objects disagreed with each
    # other and with the specs that used them.
    fixed_pom_paths = set()
    fixed_pages = []  # generated page objects, described per chunk (see _call_and_parse)
    if os.environ.get("QA_GENERATED_POM", "1") != "0":
        # Handed the page objects ALREADY in the workspace so a rebuild adds to them
        # instead of replacing them (see pom_builder.build_page_objects).
        current_pages = {}
        pages_dir = Path(repo_root) / "src" / "pages"
        if pages_dir.is_dir():
            for existing_page in pages_dir.glob("*.ts"):
                current_pages[f"src/pages/{existing_page.name}"] = read_text_file(existing_page)
        generated_pages = pom_builder.build_page_objects(
            list(plans_by_id.values()), existing=current_pages)
        for page_file in generated_pages:
            rel = page_file["path"]
            target = resolve_within(repo_root, rel)
            if target is None or is_recording_owned(rel):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(page_file["content"], encoding="utf-8")
            fixed_pom_paths.add(rel.lower())
            written_content[rel] = page_file["content"]
            accepted_files.append({k: v for k, v in page_file.items()
                                   if k in ("path", "content", "kind", "test_case_ids",
                                            "locators_used")})
            all_files.append(accepted_files[-1])
        routes_file = pom_builder.build_route_helper(route_map.load())
        if routes_file and not is_recording_owned(routes_file["path"]):
            routes_target = resolve_within(repo_root, routes_file["path"])
            if routes_target is not None:
                routes_target.parent.mkdir(parents=True, exist_ok=True)
                routes_target.write_text(routes_file["content"], encoding="utf-8")
                fixed_pom_paths.add(routes_file["path"].lower())
                written_content[routes_file["path"]] = routes_file["content"]
                accepted_files.append({k: v for k, v in routes_file.items()
                                       if k in ("path", "content", "kind", "test_case_ids",
                                                "locators_used")})
                all_files.append(accepted_files[-1])
                dynamic_context += "\n\n" + pom_builder.describe_routes(routes_file)
        sliders_file = pom_builder.build_slider_helper(list(plans_by_id.values()))
        if sliders_file and not is_recording_owned(sliders_file["path"]):
            sliders_target = resolve_within(repo_root, sliders_file["path"])
            if sliders_target is not None:
                sliders_target.parent.mkdir(parents=True, exist_ok=True)
                sliders_target.write_text(sliders_file["content"], encoding="utf-8")
                fixed_pom_paths.add(sliders_file["path"].lower())
                written_content[sliders_file["path"]] = sliders_file["content"]
                accepted_files.append({k: v for k, v in sliders_file.items()
                                       if k in ("path", "content", "kind", "test_case_ids",
                                                "locators_used")})
                all_files.append(accepted_files[-1])
                dynamic_context += "\n\n" + pom_builder.describe_sliders(sliders_file)
        if fixed_pom_paths:
            fixed_pages = [p for p in generated_pages if p["path"].lower() in fixed_pom_paths]
            print(f"      Built {len(fixed_pom_paths)} page object(s) from the Planner's "
                  f"grounded locators ({sum(len(p['members']) for p in generated_pages)} "
                  f"member(s)); the model writes specs only.")

    def _chunk_plans(chunk_cases) -> list:
        return [plans_by_id[cid] for cid in
                (c.get("Test Case ID") for c in chunk_cases)
                if cid in plans_by_id]

    def _chunk_planner_doc(chunk_cases, fresh_ids=()) -> str:
        """The planner doc SLICED to just this chunk's cases.

        Every chunk previously received the whole planner_doc.json. With
        real DOM inspection that doc is dominated by snapshots — up to 60
        elements per inspected case — so a 20-case feature handed each
        2-case chunk ~18 cases' worth of OTHER screens' elements. Two costs,
        both paid on every chunk: the relevant screen's locators compete for
        attention with dozens of irrelevant ones (the model picks a
        plausible locator from the wrong screen — indistinguishable in the
        output from a hallucinated one), and the input is inflated by data
        this chunk cannot use. Slicing keeps the join deterministic (on
        test_case_id, the same key everything else joins on).

        Falls back to the full text when no plan matches — a chunk whose
        cases the Planner never covered should still see whatever context
        exists rather than an empty doc."""
        chunk_plans = _chunk_plans(chunk_cases)
        if not chunk_plans:
            return planner_doc_text
        return coding_prompt.compact_json(coding_prompt.slim_plans(chunk_plans, fresh_ids))

    def _call_and_parse(chunk_cases, target_file_for_chunk=""):
        # Failed inspections ({"error": ...}) are not snapshots; identical
        # snapshots (cases on one screen) are sent once with every id they cover.
        fresh = coding_prompt.usable_snapshots(chunk_cases, dom_snapshots)
        snapshot_groups = coding_prompt.dedupe_snapshots(fresh)
        dom_block = (
            f"\n\nAdditional live DOM inspection of the real target screen(s). The "
            f"Planner's own inspection for the cases listed in 'test_case_ids' was "
            f"either low-confidence or came back unusable (it never got past the login "
            f"or landing page); this is a FRESH, deeper snapshot driven by the "
            f"Planner's navigation_keywords plus the case's own Test Step/Expected "
            f"Result text, and it applies to EVERY id in its 'test_case_ids'. The "
            f"Planner doc above omits its own snapshot for those cases — use this "
            f"one's elements' verified 'locator' values:\n"
            f"{coding_prompt.compact_json(snapshot_groups)}"
            if snapshot_groups else ""
        )
        pom_block = ""
        if fixed_pages:
            pom_block = "\n\n" + coding_prompt.cap_text(pom_builder.describe(
                coding_prompt.pages_for_plans(fixed_pages, _chunk_plans(chunk_cases))))
        user_prompt = f"""Test cases to implement (filtered to active cycle if applicable):
{coding_prompt.compact_json(coding_prompt.prompt_cases(chunk_cases))}

Automation Planner doc (only the plans for the cases listed above):
{_chunk_planner_doc(chunk_cases, fresh.keys())}{dom_block}{_prior_output_block(prior_files, chunk_cases, target_file_for_chunk, exclude_paths=fixed_pom_paths)}{dynamic_context}{pom_block}

Generate automation code now."""

        raw = call_agent(
            agent_name="coding_agent",
            user_prompt=user_prompt,
            feature_dir=feature_dir,
            max_tokens=16000,
            extra_system=extra_system,
        )
        # Checked BEFORE parsing — a truncated response may still happen to
        # parse as valid (but incomplete) JSON, in which case parse_json_response
        # wouldn't raise at all and this truncation would go unnoticed.
        truncated = bool(llm_client.last_usage.get("truncated"))
        try:
            result = parse_json_response(raw, feature_dir, "coding_agent")
        except ValueError:
            # Cut-off JSON does not parse. When the cause is truncation, report
            # it as such (empty result, truncated=True) so the caller can split
            # the batch instead of failing the whole chunk.
            if truncated:
                return {}, True
            raise
        if not isinstance(result, dict):
            raise ValueError(
                f"expected a JSON object with 'files'/'unresolved', "
                f"got {type(result).__name__}."
            )
        return result, truncated

    def _generate_chunk(chunk_num, target_file, chunk_cases):
        if len(chunks) > 1:
            label = target_file if target_file != _UNASSIGNED_BUCKET else "unassigned cases"
            print(f"      Generating {chunk_num}/{len(chunks)}: {label} "
                  f"({len(chunk_cases)} case(s))")

        result, truncated = _call_and_parse(chunk_cases, target_file)

        if truncated and len(chunk_cases) <= 1:
            # Nothing left to split. When the cut-off output did not even parse
            # there is nothing to accept: fail with the real reason (the caller
            # records the case as unresolved) instead of an empty "success".
            files_kept = result.get("files", []) if isinstance(result, dict) else []
            if not files_kept:
                raise ValueError("response was truncated at max_tokens with a single case; "
                                 "nothing to split — the case needs a larger output budget "
                                 "or a simpler plan")

        # A truncated chunk is retried ONCE with the batch split in half
        # before falling back to unresolved — a smaller batch needs less
        # output per call, so it's often enough to dodge the same cutoff.
        # Each half still isolates the OTHER half's outcome: one half
        # failing/truncating again just degrades that half to unresolved
        # (via the caller's own except-per-chunk handling), not the whole chunk.
        if truncated and len(chunk_cases) > 1:
            print(f"      ⚠️  Chunk {chunk_num}/{len(chunks)} response was truncated — "
                  f"retrying with the batch split in half.")
            mid = len(chunk_cases) // 2
            halves = [chunk_cases[:mid], chunk_cases[mid:]]
            merged_files, merged_unresolved = [], []
            for half in halves:
                try:
                    half_result, half_truncated = _call_and_parse(half, target_file)
                    half_files = half_result.get("files", [])
                    half_unresolved = half_result.get("unresolved", [])
                    merged_files.extend(half_files)
                    merged_unresolved.extend(half_unresolved)
                    ids_reported = any(f.get("test_case_ids") for f in half_files)
                    if half_truncated and (ids_reported or not half_files):
                        # Cut off again (or not parseable at all): any case this
                        # half's files do not cover would otherwise vanish with
                        # no file and no unresolved entry.
                        covered = {cid for f in half_files
                                   for cid in (f.get("test_case_ids") or [])}
                        covered |= {u.get("test_case_id") for u in half_unresolved}
                        for case in half:
                            if case.get("Test Case ID") not in covered:
                                merged_unresolved.append({
                                    "test_case_id": case.get("Test Case ID"),
                                    "reason": "coding_agent response was truncated again "
                                              "after splitting the batch; this case is "
                                              "not in any returned file",
                                    "error_type": "Truncated",
                                    "error_kind": "content",
                                })
                except Exception as exc:
                    for case in half:
                        merged_unresolved.append({
                            "test_case_id": case.get("Test Case ID"),
                            "reason": f"coding_agent chunk failed after truncation retry: {exc}",
                            "error_type": type(exc).__name__,
                            "error_kind": classify_error_kind(exc),
                        })
            return {"files": merged_files, "unresolved": merged_unresolved}

        return result

    # Chunks are independent LLM calls — submitted concurrently, but the
    # shared state below (all_files/written/rejected/split_part_counters,
    # and the incrementally-written failures_path) is still updated ONE
    # CHUNK'S RESULT AT A TIME under state_lock as futures complete, so a
    # single chunk's write-to-disk-and-log-progress behavior is unchanged —
    # only the LLM calls themselves overlap.
    with ThreadPoolExecutor(max_workers=min(_MAX_PARALLEL_CHUNKS, len(chunks) or 1)) as pool:
        future_to_chunk = {
            pool.submit(_generate_chunk, chunk_num, target_file, chunk_cases): (chunk_num, target_file, chunk_cases, was_split)
            for chunk_num, (target_file, chunk_cases, was_split) in enumerate(chunks, start=1)
        }
        for future in as_completed(future_to_chunk):
            chunk_num, target_file, chunk_cases, was_split = future_to_chunk[future]

            # A single chunk's failure (truncation, malformed JSON the repair
            # pass can't fix, etc.) must NOT abort the whole story — that would
            # throw away every other chunk's already-written, already-paid-for
            # files. Log the failure against these cases as unresolved instead,
            # and keep going with the remaining chunks.
            try:
                result = future.result()
            except Exception as exc:
                # By the time an exception lands here it has ALREADY exhausted
                # call_agent's per-request transient retry (utils/transient),
                # so this is no longer "a blip cost these cases their code" —
                # it is a real content failure or a real outage. That
                # distinction has to reach the operator, because the two need
                # opposite responses and this handler reports SUCCESS for the
                # stage either way: swallowing the chunk is what keeps one
                # chunk's failure from discarding nine other chunks' paid-for
                # files, but it also means nothing downstream raises.
                kind = classify_error_kind(exc)
                print(f"      ⚠️  Chunk {chunk_num}/{len(chunks)} ({target_file}) failed "
                      f"after retries [{kind}]: {type(exc).__name__}: {exc}")
                with state_lock:
                    if kind == "infra":
                        infra_failed_chunks.append({
                            "chunk": f"{chunk_num}/{len(chunks)}",
                            "target_file": target_file,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "test_case_ids": [c.get("Test Case ID") for c in chunk_cases],
                        })
                    for case in chunk_cases:
                        all_unresolved.append({
                            "test_case_id": case.get("Test Case ID"),
                            "reason": f"coding_agent chunk failed: {exc}",
                            "error_type": type(exc).__name__,
                            "error_kind": kind,
                        })
                    write_json(failures_path, all_unresolved)
                continue

            files = result.get("files", [])
            with state_lock:
                if was_split:
                    # A target_file that needed splitting means one logical file's
                    # cases arrived across multiple calls. Rather than silently
                    # merging/overwriting two independently-generated file contents
                    # for the same path (fragile — imports, fixtures, or helper
                    # functions could conflict), each part gets its own physical
                    # file with a distinguishing suffix, and this is surfaced so
                    # it's not mistaken for N unrelated files.
                    split_part_counters[target_file] = split_part_counters.get(target_file, 0) + 1
                    part = split_part_counters[target_file]
                    wanted = normalize_target_file(target_file).lower()
                    for f in files:
                        if normalize_target_file(f.get("path")).lower() == wanted:
                            f["path"] = _insert_part_suffix(
                                f["path"], part, _part_label(chunk_cases, part))
                    print(f"      ⚠️  '{target_file}' was too large for one call — "
                          f"part {part} written separately; consider merging manually "
                          f"or reviewing as split files.")

                # No two chunks may write the same spec file. Splitting handles
                # the planned case; this catches the rest (the half-split retry
                # after truncation, the "(unassigned)" bucket, a path spelled
                # `./tests/A.spec.ts` or with different casing — the same file
                # on Windows).
                for f in files:
                    rel = f.get("path")
                    if not rel or not _TEST_SUFFIX_RE.search(str(rel)):
                        continue
                    key = normalize_target_file(rel).lower()
                    if key in used_spec_paths:
                        label = _part_label(chunk_cases, 0)
                        candidate, n = _insert_part_suffix(rel, 0, label), 2
                        while normalize_target_file(candidate).lower() in used_spec_paths:
                            candidate = _insert_part_suffix(rel, 0, f"{label}-{n}")
                            n += 1
                        print(f"      ⚠️  '{rel}' was already written by another chunk — "
                              f"keeping both, this one as '{candidate}'.")
                        f["path"] = candidate
                        key = normalize_target_file(candidate).lower()
                    used_spec_paths.add(key)

                # Written to disk PER CHUNK, immediately — not batched until every
                # chunk finishes. A 74-case feature can take dozens of chunk calls;
                # waiting until the very end means an interruption (crash, Ctrl-C, or
                # just wanting to inspect progress) loses everything generated so far,
                # even though most of it already succeeded and was paid for.
                # PHASE 1 — validate and WRITE every file in the chunk.
                #
                # Nothing is syntax-checked until the whole chunk is on disk,
                # because the files in one chunk depend on each other: a spec
                # imports the page object generated alongside it. Checking
                # each file the instant it was written meant `tsc` saw the
                # spec against the PREVIOUS run's page object and rejected it
                # with "Property 'memberTypeOption' does not exist" — for a
                # property the page object written two lines later does
                # define. The result was worse than a plain failure: the page
                # object landed, the spec was thrown away, and the workspace
                # was left holding one half of a matched pair.
                staged = []
                for f in files:
                    rel, content = f.get("path"), f.get("content")
                    if not rel or content is None:
                        rejected.append({"path": rel, "reason": "missing 'path' or 'content'"})
                        continue
                    # Observed in practice: a model can return syntactically valid
                    # JSON whose "content" is itself a placeholder ("...", "full POM
                    # content") rather than real code — e.g. when it gets confused by
                    # a literal "[PLACEHOLDER]" string elsewhere in its input (the
                    # DOM-inspection stub) into treating its OWN output as
                    # sketch-worthy too. That would otherwise be written to disk as a
                    # "successful" file containing garbage, with no error anywhere.
                    stripped = content.strip()
                    if len(stripped) < _MIN_REAL_CONTENT_LENGTH or stripped in _PLACEHOLDER_CONTENT_MARKERS:
                        rejected.append({"path": rel, "reason": f"content looks like a placeholder/stub, "
                                                                 f"not real generated code (got: {stripped[:80]!r})"})
                        continue
                    rel_norm = str(rel).replace("\\", "/").lstrip("/")
                    if rel_norm.lower() in fixed_pom_paths:
                        # Generated from the plan; a model's version would only
                        # reintroduce the cross-chunk disagreement.
                        continue
                    if (fixed_pom_paths and not _TEST_SUFFIX_RE.search(rel_norm)
                            and rel_norm.lower().startswith("src/pages/")
                            and not is_recording_owned(rel_norm)):
                        # A second, model-written page object for the same
                        # screens disagrees with the generated ones, and specs
                        # written against it fail `tsc`. Say so, in a form the
                        # repair prompt carries back to the model.
                        rejected.append({"path": rel, "reason": (
                            "page objects for this feature are generated; use the listed "
                            "classes and members instead of writing another one")})
                        continue
                    # Same confinement helper the Self-Healer now uses, so
                    # both write paths enforce it identically (see
                    # artifact_io.resolve_within).
                    file_path = resolve_within(repo_root, rel_norm)
                    if file_path is None:
                        rejected.append({"path": rel, "reason": "path escapes repo_output_dir"})
                        continue
                    # Recorded page objects belong to the recording parser.
                    # Rejected rather than merged: this file is regenerated
                    # wholesale from the human recording, so any edit here is
                    # overwritten next ingest anyway — and in the meantime it
                    # destroys locators no model can reconstruct. See
                    # is_recording_owned for the run that lost 93 of them.
                    if is_recording_owned(rel_norm):
                        rejected.append({
                            "path": rel,
                            "reason": (f"'{_RECORDING_OWNED_DIR}' is generated from human "
                                       f"recordings by utils/recording_parser.py and is "
                                       f"read-only to this agent — import from it instead. "
                                       f"Put any new locator in a page object outside it."),
                        })
                        continue
                    # A shared POM/helper path emitted by an EARLIER chunk this
                    # same run (any target_file group, not just this one) with
                    # DIFFERENT content means this write would silently discard
                    # that chunk's version. Flag it — still write (a second
                    # chunk's version is often the more complete one once more
                    # cases' selectors are folded in), but make the collision
                    # visible instead of a silent last-write-wins.
                    prior_content = written_content.get(rel_norm)
                    if prior_content is not None and prior_content != content:
                        overwrite_warnings.append({
                            "path": rel_norm,
                            "note": "written by more than one generation chunk this run "
                                    "with different content — later chunk's version was "
                                    "kept; review for a dropped page-object/helper change",
                        })
                    written_content[rel_norm] = content
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    # What was there BEFORE this write, so a rejection can put
                    # it back. See the restore in phase 2.
                    previous_content = None
                    if file_path.is_file():
                        try:
                            previous_content = file_path.read_text(encoding="utf-8")
                        except Exception:
                            previous_content = None
                    file_path.write_text(content, encoding="utf-8")
                    staged.append((f, rel, rel_norm, content, file_path, previous_content))

                # PHASE 2 — now that the whole chunk is on disk, check it.
                #
                # ATOMIC, ALL-OR-NOTHING. A chunk's spec and the page objects it
                # imports are generated together and only make sense together,
                # so they are accepted or rolled back as ONE unit.
                #
                # Rolling back per-file instead is a real, observed defect: a
                # regenerated spec failed its syntax check and was restored to
                # the previous run's version, while the NEW page object written
                # beside it in the same chunk was kept. The workspace was left
                # holding last run's spec calling this run's page object — it
                # compiled (each file is individually valid) and then died at
                # run time with "register.driveCaptureTillResult is not a function".
                # Every file in the chunk sharing one fate is what makes
                # "the workspace always holds a consistent set" true.
                syntax_failures = []
                for _f, rel, _rel_norm, _content, file_path, _previous_content in staged:
                    # Syntax-checked AFTER writing (needs a real path on disk) —
                    # a file that fails is rolled back rather than left on disk
                    # looking like a "successful" generation with no error
                    # anywhere, same intent as the placeholder-content check above.
                    syntax_error = _check_syntax(file_path, repo_root=repo_root)
                    if syntax_error:
                        syntax_failures.append((rel, syntax_error))

                if syntax_failures:
                    # ROLL BACK TO THE PREVIOUS VERSION, don't delete.
                    # `unlink` here destroyed a spec that had been
                    # generated, executed and healed on an earlier run,
                    # because this run's replacement for it failed to
                    # compile. A rejected regeneration is a reason to keep
                    # what already worked, never a reason to leave the
                    # workspace with no test at all.
                    failed_paths = ", ".join(p for p, _ in syntax_failures)
                    for _f, rel, rel_norm, _content, file_path, previous_content in staged:
                        if previous_content is not None:
                            file_path.write_text(previous_content, encoding="utf-8")
                        else:
                            file_path.unlink(missing_ok=True)
                        written_content.pop(rel_norm, None)
                        own_error = next((e for p, e in syntax_failures if p == rel), None)
                        # tsc says only that a property is absent. When it
                        # actually lives on another page object this same spec
                        # imports, say so — that is the difference between a
                        # repair that lands and two more rounds of guessing.
                        hint = suggest_property_owner(own_error or "", repo_root)
                        if hint:
                            own_error = f"{own_error} {hint}"
                        rejected.append({
                            "path": rel,
                            "reason": (f"syntax check failed: {own_error}" if own_error else
                                       f"rolled back with its chunk — {failed_paths} failed "
                                       f"the syntax check, and a chunk's spec and page "
                                       f"objects are only valid together"),
                            "restored_previous_version": previous_content is not None,
                            "rolled_back_with_chunk": own_error is None,
                        })
                    write_json(artifact_path(feature_dir, "coding_agent_rejected_files.json"), rejected)
                    all_files.extend(files)
                    all_unresolved.extend(result.get("unresolved", []))
                    write_json(failures_path, all_unresolved)
                    continue

                for f, _rel, rel_norm, content, _file_path, _previous_content in staged:
                    # Deterministic convention check, not a rejection reason —
                    # a file can be genuinely correct code and still miss this
                    # convention, so it's logged for visibility rather than
                    # thrown away. See _missing_title_prefixes' docstring for
                    # why this is now a secondary signal, not the primary join.
                    # Do the locators in this file actually EXIST on the
                    # screen it targets? The syntax check above proved only
                    # that the file compiles. See utils/locator_verifier —
                    # a non-match is evidence, not proof, so this warns and
                    # feeds a repair pass rather than rejecting outright.
                    verification = _verify_file_locators(content, chunk_cases, dom_snapshots)
                    if verification and verification.get("unverified"):
                        unverified_locators.append({
                            "path": rel_norm,
                            "summary": locator_verifier.summarize(verification),
                            "unverified": verification["unverified"],
                            "_elements": verification.get("_elements", []),
                        })

                    inline = _inline_locators_in_spec(rel_norm, content)
                    if inline:
                        inline_locator_warnings.append({
                            "path": rel_norm, "inline_locators": inline[:20],
                            "count": len(inline),
                        })

                    expected_ids = f.get("test_case_ids") or [c.get("Test Case ID") for c in chunk_cases]
                    missing = _missing_title_prefixes(rel_norm, content, expected_ids)
                    if missing:
                        title_prefix_warnings.append({"path": rel_norm, "missing_test_case_ids": missing})

                    written.append(rel_norm)
                    # Normalized path recorded back onto the accepted entry so
                    # every downstream consumer joins on the SAME string this
                    # module wrote to disk, not the model's original spelling.
                    accepted_files.append({**f, "path": rel_norm})

                all_files.extend(files)
                all_unresolved.extend(result.get("unresolved", []))

                # Kept current after every chunk (not just at the end) for the same
                # reason: so partial progress is visible/usable if the run is
                # interrupted or still going, not just after full completion.
                write_json(failures_path, all_unresolved)
                if rejected:
                    write_json(artifact_path(feature_dir, "coding_agent_rejected_files.json"), rejected)

    # ---- Locator repair pass -------------------------------------------
    # Sequential and AFTER the parallel phase on purpose: each repair is an
    # LLM call, and running one inside state_lock would serialize every
    # other chunk behind it. This is the step that turns locator
    # verification from a warning nobody reads into a fix — a file whose
    # locators don't exist would otherwise compile, run for 30s, and time
    # out at Executor time.
    if unverified_locators and not skip_locator_repair:
        print(f"      {len(unverified_locators)} file(s) have locators the live DOM "
              f"snapshot doesn't corroborate — attempting one repair pass each.")
        repair_attempted = set()
        for entry in unverified_locators:
            rel_norm = entry["path"]
            target = resolve_within(repo_root, rel_norm)
            if target is None or not target.is_file():
                continue
            original = read_text_file(target)
            # A shared page object is verified once PER CHUNK, so the same file
            # arrives here many times. Re-asking about byte-identical content
            # that was already tried and rejected cannot land differently — it
            # was costing a 40s model call each time (ImageEditingPage.ts: 18).
            attempt_key = (rel_norm, hashlib.sha1(original.encode("utf-8", "replace")).hexdigest())
            if attempt_key in repair_attempted:
                entry["repair"] = "skipped — this exact content was already tried"
                continue
            repair_attempted.add(attempt_key)
            print(f"      Repairing locators in {rel_norm} ({entry['summary']})")
            repaired = _repair_unverified_locators(original, entry, feature_dir, extra_system)
            if repaired == original:
                entry["repair"] = "no change produced"
                continue

            # A repair must not make things worse: it is only kept if it
            # still compiles AND corroborates strictly more locators than
            # the version already on disk.
            target.write_text(repaired, encoding="utf-8")
            syntax_error = _check_syntax(target, repo_root=repo_root)
            after = locator_verifier.verify_file(repaired, entry.get("_elements", []))
            improved = len(after.get("unverified", [])) < len(entry["unverified"])
            if syntax_error:
                # One retry that shows the model its own compile error —
                # without it the same mistake is simply made again.
                retried = _repair_unverified_locators(original, entry, feature_dir,
                                                      extra_system, compile_error=syntax_error)
                if retried and retried != original:
                    target.write_text(retried, encoding="utf-8")
                    syntax_error = _check_syntax(target, repo_root=repo_root)
                    after = locator_verifier.verify_file(retried, entry.get("_elements", []))
                    improved = len(after.get("unverified", [])) < len(entry["unverified"])
                    repaired = retried
            if syntax_error or not improved:
                target.write_text(original, encoding="utf-8")
                entry["repair"] = ("rejected — did not compile" if syntax_error
                                   else "rejected — corroborated no more locators than before")
                print(f"      -> {rel_norm}: repair {entry['repair']}; original kept.")
                continue

            entry["repair"] = (f"applied — unverified locators "
                                f"{len(entry['unverified'])} -> {len(after.get('unverified', []))}")
            entry["unverified_after_repair"] = after.get("unverified", [])
            written_content[rel_norm] = repaired
            for accepted in accepted_files:
                if accepted.get("path") == rel_norm:
                    accepted["content"] = repaired
            print(f"      -> {rel_norm}: {entry['repair']}.")

    if unverified_locators:
        # `_elements` is the full snapshot echoed back for the repair prompt;
        # it would dominate the artifact and is already in planner_doc.json.
        report = [{k: v for k, v in e.items() if k != "_elements"} for e in unverified_locators]
        write_json(artifact_path(feature_dir, "coding_agent_unverified_locators.json"), report)
        still_bad = sum(1 for e in unverified_locators
                        if e.get("unverified_after_repair", e["unverified"]))
        print(f"⚠️  {still_bad} file(s) still contain locator(s) the live DOM snapshot could "
              f"not corroborate — see {artifact_path(feature_dir, 'coding_agent_unverified_locators.json')}. "
              f"A snapshot only covers ~60 elements of ONE screen, so this is evidence, not "
              f"proof; check these before trusting the run.")

    if infra_failed_chunks:
        write_json(artifact_path(feature_dir, "coding_agent_infra_failures.json"), infra_failed_chunks)
        lost = sum(len(c["test_case_ids"]) for c in infra_failed_chunks)
        print(f"🚨 {len(infra_failed_chunks)} chunk(s) covering {lost} case(s) failed for an "
              f"ACCOUNT/QUOTA/RATE-LIMIT reason that outlasted the retry budget — see "
              f"{artifact_path(feature_dir, 'coding_agent_infra_failures.json')}. Those cases have no code "
              f"and are NOT a modelling failure: fix the provider issue and re-run this "
              f"stage, don't rewrite the test cases.")

    if rejected:
        # File itself already written per-chunk above; this is just the summary print.
        print(f"⚠️  {len(rejected)} generated file(s) rejected as unsafe/incomplete — "
              f"see {artifact_path(feature_dir, 'coding_agent_rejected_files.json')}")

    if title_prefix_warnings:
        write_json(artifact_path(feature_dir, "coding_agent_title_prefix_warnings.json"), title_prefix_warnings)
        print(f"⚠️  {len(title_prefix_warnings)} file(s) have a test whose title doesn't start "
              f"with its Test Case ID — see {artifact_path(feature_dir, 'coding_agent_title_prefix_warnings.json')} "
              f"(files were still accepted; this weakens Bug Reporter's screenshot matching, not "
              f"case resolution — the generated_file contract's 'test_case_ids' is the join).")

    if inline_locator_warnings:
        write_json(artifact_path(feature_dir, "coding_agent_inline_locator_warnings.json"),
                   inline_locator_warnings)
        total_inline = sum(w["count"] for w in inline_locator_warnings)
        print(f"⚠️  {len(inline_locator_warnings)} spec file(s) define {total_inline} "
              f"locator(s) inline instead of behind a page object — see "
              f"{artifact_path(feature_dir, 'coding_agent_inline_locator_warnings.json')}. These files "
              f"work, but a locator that breaks must then be healed once PER SPEC rather "
              f"than once in the page object every spec shares.")

    if overwrite_warnings:
        write_json(artifact_path(feature_dir, "coding_agent_overwrite_warnings.json"), overwrite_warnings)
        print(f"⚠️  {len(overwrite_warnings)} shared file(s) were written more than once this run "
              f"with different content (e.g. a POM/helper touched by two case groups) — "
              f"see {artifact_path(feature_dir, 'coding_agent_overwrite_warnings.json')}")

    _clear_stale_diagnostics(feature_dir, {
        "coding_agent_rejected_files.json": rejected,
        "coding_agent_title_prefix_warnings.json": title_prefix_warnings,
        "coding_agent_inline_locator_warnings.json": inline_locator_warnings,
        "coding_agent_overwrite_warnings.json": overwrite_warnings,
        "coding_agent_infra_failures.json": infra_failed_chunks,
        "coding_agent_unverified_locators.json": unverified_locators,
    })

    append_context_summary(
        feature_dir, "coding_agent",
        f"Generated {len(written)} file(s), "
        f"{len(all_unresolved)} unresolved case(s)"
        + (f", {len(rejected)} rejected." if rejected else ".")
        + (f" 🚨 {len(infra_failed_chunks)} chunk(s) lost to an account/quota/rate-limit "
           f"failure — those cases are recoverable by re-running this stage."
           if infra_failed_chunks else "")
        + (f" {len(title_prefix_warnings)} file(s) with title-prefix convention warnings."
           if title_prefix_warnings else "")
        + (f" {len(overwrite_warnings)} file(s) overwritten by multiple chunks."
           if overwrite_warnings else "")
    )

    if len(accepted_files) < len(all_files):
        print(f"      {len(all_files) - len(accepted_files)} file(s) the model returned were "
              f"NOT accepted and are excluded from the result (so the Executor/Reviewer/"
              f"Self-Healer never see a path that isn't on disk).")

    return {"files": accepted_files, "unresolved": all_unresolved}


def _cli():
    """Standalone entry point — generate code straight from a reviewed
    sheet + planner doc, without running the rest of the pipeline."""
    import argparse

    from utils.env_config import resolve_app_env

    resolve_app_env()  # low-confidence DOM inspection below reads APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", required=True, help="Reviewed test case .xlsx path.")
    parser.add_argument("--cycle", default="", help="JIRA/Zephyr test cycle id (optional filter).")
    parser.add_argument("--planner-doc", default="", help="Path to planner_doc.json.")
    parser.add_argument("--conventions-file", default="")
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--pom-summary-file", default="",
                         help="Path to POM_SUMMARY.md (existing reusable locators/helpers).")
    parser.add_argument("--include-manual-cases", action="store_true",
                         help="Also automate rows marked Execution Type=Manual (default: "
                              "only Automated-marked rows are coded).")
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)
    pom_summary = read_text_file(args.pom_summary_file)

    result = run(
        sheet_path=Path(args.sheet), cycle_id=args.cycle,
        planner_doc_path=Path(args.planner_doc) if args.planner_doc else None,
        conventions_path=Path(args.conventions_file) if args.conventions_file else None,
        feature_dir=feature_dir, repo_output_dir=Path(args.repo_dir),
        existing_pom_summary=pom_summary, include_manual=args.include_manual_cases,
    )
    print(f"\n✅ {len(result['files'])} file(s) generated, "
          f"{len(result['unresolved'])} unresolved case(s).")


if __name__ == "__main__":
    _cli()
