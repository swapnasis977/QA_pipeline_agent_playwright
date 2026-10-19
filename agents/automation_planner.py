"""
Agent 2: Automation Planner
IN:  Test case (from sheet), feature context, existing repo code (optional)
OUT: Planner doc (markdown) — layer, locators/flow, API notes, target file/naming

Real DOM inspection should only run for flows NOT already covered by
`existing_pom_summary` — this is the main token-saving lever from the design
doc, so don't call it unconditionally.
"""

import json
import os
import random
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import read_text_file, write_json
from utils import dom_inspector
from utils import llm_client as _llm_client
from utils.llm_client import (
    append_context_summary,
    call_agent,
    parse_json_response,
    read_context_summary,
)
from utils.run_layout import artifact_path

# Standing instructions live in skills/automation_planner.skill.md (cached).



def _confidence(value, default: float = 1.0) -> float:
    """A model may emit null, "0.8" or "high" here; `None < 0.6` raised
    TypeError and took the whole planning stage down. Unusable -> default."""
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def inspect_live_dom(flow_description: str) -> dict:
    """Real headless-Playwright inspection (see utils/dom_inspector.py) — logs
    into APP_BASE_URL with APP_TEST_USER/APP_TEST_PASSWORD once (cached across
    calls in this process), best-effort navigates toward flow_description's
    keywords, and returns the actual visible interactive elements instead of
    a placeholder string."""
    return dom_inspector.inspect(
        flow_description, cache_ttl=float(os.environ.get("QA_ROUTE_SNAPSHOT_TTL", "600")))


def _inspection_context(plan: dict) -> str:
    """Builds the text dom_inspector.inspect() extracts its click keywords
    from, for ONE plan.

    inspect() keeps only 4+-letter words, in order, capped at
    dom_inspector._MAX_KEYWORDS — so ORDER decides which keywords survive
    the cap and get tried against the page. The Planner's own
    "navigation_keywords" (the app's real UI labels for the click path, see
    skills/automation_planner.skill.md) therefore go FIRST, ahead of
    flow_summary's prose: flow_summary is written in the test case's domain
    wording, which frequently doesn't appear anywhere in the app's UI, and
    letting its filler words ("Log", "then", "verify") consume the keyword
    budget is what leaves inspection stranded on the dashboard."""
    keywords = plan.get("navigation_keywords") or []
    if isinstance(keywords, str):  # tolerate a model emitting a single string
        keywords = [keywords]
    nav_text = " ".join(str(k) for k in keywords if k)
    return f"{nav_text} {plan.get('flow_summary', '') or ''}".strip()


#: A snapshot whose element count is below this told us almost nothing about
#: the target screen — a real app screen with any interactive content has
#: more than a couple of visible controls. (Zero elements = inspection
#: unavailable; one or two = usually a bare error/loading shell.)
_MIN_USEFUL_ELEMENTS = 3


def _grade_inspection(result: dict) -> str:
    """Classifies what a snapshot is actually WORTH to the Coding Agent, so a
    failed inspection stops looking identical to a successful one in
    planner_doc.json.

    Nothing downstream previously distinguished "here are 40 verified
    locators for the target screen" from "APP_BASE_URL isn't set" — both
    just appeared as a populated `dom_inspection_result` key, and the Coding
    Agent's re-inspection trigger keyed off the model's SELF-REPORTED
    confidence, which knows nothing about whether the browser actually got
    anywhere. Returns one of:
      "ok"           — real elements from a screen we navigated to
      "not_navigated"— real elements, but never clicked through to the target
      "login_page"   — login wasn't completed; elements are the login form
      "empty"        — inspection ran but found (almost) nothing usable
      "unavailable"  — inspection couldn't run at all (no APP_BASE_URL, error)
    """
    if not isinstance(result, dict):
        return "unavailable"
    if result.get("error") or (result.get("note") and "elements" not in result):
        return "unavailable"
    elements = result.get("elements") or []
    if len(elements) < _MIN_USEFUL_ELEMENTS:
        return "empty" if elements else "unavailable"
    if not result.get("logged_in"):
        return "login_page"
    if not result.get("navigated_by_keyword"):
        return "not_navigated"
    return "ok"


#: Grades that mean the snapshot can't be trusted as the target screen's real
#: DOM. A plan graded one of these has its dom_inspection_confidence pulled
#: DOWN below coding_agent._DOM_CONFIDENCE_THRESHOLD, which is what makes the
#: Coding Agent re-inspect that case live (with the richer Test Step/Expected
#: Result text) instead of generating code off a login-page snapshot.
_UNUSABLE_GRADES = {"login_page", "empty", "unavailable"}

#: What a plan's confidence is forced to when its inspection came back
#: unusable — just under coding_agent._DOM_CONFIDENCE_THRESHOLD (0.5).
_UNUSABLE_CONFIDENCE = 0.2


def _inspect_plans(plans: list) -> None:
    """Runs live inspection for every plan flagged needs_dom_inspection,
    grades each result, and downgrades the confidence of any plan whose
    snapshot turned out unusable (see _UNUSABLE_GRADES).

    Results are memoized on the inspection context text: a feature's cases
    routinely share one target screen (10 cases on the same form), and each
    inspect() call re-drives a real browser through up to 4 clicks. Without
    this, planning 10 same-screen cases pays that navigation 10 times for 10
    byte-identical snapshots."""
    cache = {}
    for plan in plans:
        if not plan.get("needs_dom_inspection"):
            continue
        context_text = _inspection_context(plan)
        if context_text in cache:
            result = cache[context_text]
            plan["dom_inspection_reused_from_cache"] = True
        else:
            result = inspect_live_dom(context_text)
            cache[context_text] = result
        plan["dom_inspection_result"] = result
        plan["dom_inspection_context"] = context_text

        grade = _grade_inspection(result)
        plan["dom_inspection_quality"] = grade
        if grade in _UNUSABLE_GRADES:
            # Deliberately overrides whatever the model reported: its
            # confidence was about its own reuse JUDGMENT, made before any
            # browser ran, and cannot account for the inspection failing.
            plan["dom_inspection_confidence"] = min(
                _confidence(plan.get("dom_inspection_confidence")), _UNUSABLE_CONFIDENCE)


#: Hard ceiling on test cases per LLM call, regardless of content size.
#: Each case's output (layer, flow_summary, reused_pom, api_endpoints,
#: target_file, coverage_notes) runs several hundred tokens, so a feature
#: with many cases blows past any single max_tokens budget in one call — 80
#: cases truncated even at 16000. Planning is independent per case (no
#: cross-case reasoning needed), so batching is safe: split, call
#: repeatedly, concatenate.
_BATCH_SIZE = 15

#: Soft ceiling on a batch's serialized INPUT size — a batch of 15 short
#: cases and a batch of 15 cases with long multi-step Given/When/Then bodies
#: are not equivalent risk: the latter's OUTPUT (one plan object per case)
#: scales with input verbosity too, so a fixed case-COUNT cap alone still
#: let a batch of unusually verbose cases approach truncation. Batches split
#: on whichever limit (count or size) is hit first.
_BATCH_MAX_CHARS = 12000

#: Concurrent planning calls. Mirrors coding_agent._MAX_PARALLEL_CHUNKS and is
#: capped for the same reason: these all go out on ONE API key, so the ceiling
#: is the provider's per-minute limit, not the machine's cores. Env-tunable
#: because that limit is an account property, not a property of this code —
#: set QA_MAX_PARALLEL_BATCHES=1 to restore the old strictly-sequential
#: behavior when debugging a provider issue.
try:
    _MAX_PARALLEL_BATCHES = max(1, int(os.environ.get("QA_MAX_PARALLEL_BATCHES", 4)))
except (TypeError, ValueError):
    _MAX_PARALLEL_BATCHES = 4


#: Per-batch ceilings on the two blocks every planning call re-sends.
_MAX_PRIOR_CONTEXT_CHARS = 6000
_MAX_POM_SUMMARY_CHARS = 12000


def _compact(obj) -> str:
    """Model-bound JSON: no indentation or padding (also what batch size is measured on)."""
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def _cap_head(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... (truncated)"


def _cap_tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "(earlier notes omitted) ...\n" + text[-limit:]


def _make_batches(cases: list) -> list:
    """Splits cases into batches bounded by BOTH _BATCH_SIZE (count) and
    _BATCH_MAX_CHARS (serialized size) — whichever limit a case would
    exceed first ends the current batch. A single case larger than
    _BATCH_MAX_CHARS on its own still gets its own batch rather than being
    dropped (better to risk one oversized call than silently skip a case)."""
    if not cases:
        return [[]]
    batches, current, current_chars = [], [], 0
    for case in cases:
        # Measured on the same compact form the prompt is built from.
        case_chars = len(_compact(case))
        if current and (len(current) >= _BATCH_SIZE or current_chars + case_chars > _BATCH_MAX_CHARS):
            batches.append(current)
            current, current_chars = [], 0
        current.append(case)
        current_chars += case_chars
    if current:
        batches.append(current)
    return batches


def _verify_reused_pom_claims(plan: dict, existing_pom_summary: str) -> list:
    """Returns the subset of plan['reused_pom'] entries that do NOT appear
    (case-insensitive substring) anywhere in existing_pom_summary — a
    deterministic, zero-cost check for a reuse claim the model can't
    actually back up. An empty/missing existing_pom_summary means NOTHING
    can be verified as reused, so every claimed entry counts as unverified
    in that case (there was nothing to reuse from).

    Checks each DOT-SEPARATED component separately ("LoginPage.submit" needs
    both "LoginPage" and "submit" to appear SOMEWHERE in the summary, not the
    literal substring "LoginPage.submit") — a hand-written POM_SUMMARY.md
    typically lists a class name and its methods separately (e.g. nested
    bullets), not concatenated with a dot, so requiring the exact dotted
    string would false-positive on real, legitimately-reused methods."""
    reused = plan.get("reused_pom") or []
    if not reused:
        return []
    summary_lower = (existing_pom_summary or "").lower()
    unverified = []
    for name in reused:
        parts = [p for p in str(name).lower().split(".") if p]
        if not parts or not all(part in summary_lower for part in parts):
            unverified.append(name)
    return unverified


#: Sheet columns that carry NO planning-relevant signal — last execution
#: outcome and the human review-gate marker — but are still present on
#: every row read straight off the sheet (see coding_agent.read_test_case_sheet).
#: Planning needs Name/Description/Test Step/Expected Result/etc, not these;
#: stripping them before serializing into every batch's prompt is pure token
#: waste removal, not a behavior change (nothing downstream of the Planner
#: reads them off ITS output either).
_PLANNING_IRRELEVANT_COLUMNS = ("Result", "Status")


def _strip_planning_irrelevant_fields(cases: list) -> list:
    return [{k: v for k, v in case.items() if k not in _PLANNING_IRRELEVANT_COLUMNS}
            for case in cases]


#: A plan that says "no inspection needed" (needs_dom_inspection: false) with
#: a self-reported confidence BELOW this is treated as a guess, not a
#: verified skip — see the spot-check below run().
_SPOT_CHECK_CONFIDENCE_THRESHOLD = 0.6

#: Bounded, not "verify every low-confidence case" — the whole point of
#: selective inspection is keeping live-inspection cost minimal; spot-
#: checking is a safety net against a systematically overconfident model,
#: not a second full inspection pass.
_MAX_SPOT_CHECKS = 3


def _coerce_plan_list(parsed, where: str) -> list:
    """Normalizes one planning call's parsed response into a list of plan
    dicts, tolerating the {"plans": [...]} / {"test_cases": [...]} wrappers
    models wrap the array in. Raises on anything that isn't coercible, naming
    `where` (which batch / the repair pass) so the failure is locatable."""
    if isinstance(parsed, dict):
        for key in ("plans", "test_cases", "cases", "items"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list) or not all(isinstance(p, dict) for p in parsed):
        raise ValueError(
            f"automation_planner: {where} expected a JSON array of plan objects, "
            f"got {type(parsed).__name__}."
        )
    return parsed


def plan_cases(cases: list, existing_pom_summary: str, feature_dir: Path, *,
               extra_system: str = "", prior_context: str = None,
               header: str = "Test cases:", label: str = "batch") -> list:
    """The raw planning call(s) for `cases` — batched, parsed, concatenated.

    Deliberately does NOTHING else: no live inspection, no coverage/collision
    detection, no artifact write, no summary append. That is what makes it
    reusable by the callers that must NOT re-run those side effects —
    `run()`'s own main pass, the coverage-gap repair below, and
    `qa_pipeline.agents.adapters.replan` (the contract layer's per-case repair,
    which re-plans ONE case against a verifier's diagnosis and must not
    clobber the planner_doc.json the other cases are already in).

    `extra_system` is prepended to the cached skill's system prompt — the seam
    the repair prompt travels through. Before it existed, the orchestrator's
    repair loop had nowhere to put a verifier's diagnosis and simply discarded
    it, so every "repair attempt" re-issued a byte-identical planning call and
    paid full price for the same answer.
    """
    if prior_context is None:
        prior_context = read_context_summary(feature_dir)

    batches = _make_batches(cases)

    # Both blocks are re-sent with EVERY batch, so they are bounded. The summary
    # is append-only (newest last), so the tail is kept; the POM summary is
    # per-repo and could be arbitrarily large (reuse CLAIMS are still verified
    # against the full text in _verify_reused_pom_claims).
    prompt_context = _cap_tail(prior_context or "", _MAX_PRIOR_CONTEXT_CHARS)
    prompt_pom_summary = _cap_head(existing_pom_summary or "", _MAX_POM_SUMMARY_CHARS)

    def _plan_one_batch(batch_num, batch):
        if len(batches) > 1:
            print(f"      Planning {label} {batch_num}/{len(batches)} "
                  f"({len(batch)} case(s))...")

        # Full test-case rows are still passed here because the Planner
        # genuinely needs per-case detail (Given/When/Then) to plan
        # locators/flows — that's real task content, not re-derivable from a
        # short summary. The saving here is not re-fetching/re-deriving
        # JIRA+Figma context a second time; that's covered by `prior_context`
        # instead of re-sending those payloads.
        user_prompt = f"""Feature context so far (from upstream agents):
{prompt_context}

{header}
{_compact(batch)}

Existing POM / reusable helpers summary:
{prompt_pom_summary or "(none provided — assume nothing exists yet)"}

Produce the planner doc now."""

        raw = call_agent(
            agent_name="automation_planner",
            user_prompt=user_prompt,
            feature_dir=feature_dir,
            max_tokens=8000,
            extra_system=extra_system,
        )
        # A response cut off at max_tokens is invalid JSON (or silently drops the
        # last cases). Each plan carries an `elements` array, so a full batch can
        # exceed 8000 tokens. Nothing read the flag: the whole batch failed to
        # parse and was re-planned at the same size. Plan the halves instead.
        if len(batch) > 1 and _llm_client.last_usage.get("truncated"):
            mid = len(batch) // 2
            print(f"      ⚠️  Planning {label} {batch_num}/{len(batches)} was truncated — "
                  f"planning its {len(batch)} case(s) as two smaller batches.")
            return _plan_one_batch(batch_num, batch[:mid]) + _plan_one_batch(batch_num, batch[mid:])
        return _coerce_plan_list(
            parse_json_response(raw, feature_dir, "automation_planner"),
            f"{label} {batch_num}/{len(batches)}")

    # Batches are INDEPENDENT LLM calls — the module docstring above
    # _BATCH_SIZE says so explicitly ("Planning is independent per case (no
    # cross-case reasoning needed), so batching is safe") — yet they ran
    # strictly one after another while the Coding Agent parallelized exactly
    # the same shape of work. This is I/O-bound waiting on the provider, so
    # the pool cuts wall-clock time without changing cost.
    #
    # ORDER IS PRESERVED regardless of completion order: results are collected
    # by batch index and concatenated in order, because the downstream Coding
    # Agent groups by target_file and a reshuffled plan list changes how cases
    # group into generation calls (see orchestrator/stages.py's "Order is
    # preserved from the first pass" note).
    #
    # A FAILING BATCH IS ISOLATED, not fatal. Previously one batch raising
    # discarded every batch that had already succeeded AND sent run_stage off
    # to retry the whole stage, re-planning and re-billing work that was
    # already done. The cases in a dropped batch simply come back unplanned,
    # which `_detect_coverage_gaps` already detects and `_repair_missing_plans`
    # already recovers in one bounded pass — the machinery for this exact
    # situation was in place, it just was not being allowed to run.
    results, failures = {}, {}
    workers = min(_MAX_PARALLEL_BATCHES, len(batches))
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_plan_one_batch, n, b): n
                       for n, b in enumerate(batches, start=1)}
            for future in as_completed(futures):
                batch_num = futures[future]
                try:
                    results[batch_num] = future.result()
                except Exception as exc:  # noqa: BLE001 — see above
                    failures[batch_num] = exc
                    print(f"      ⚠️  Planning {label} {batch_num}/{len(batches)} failed "
                          f"({type(exc).__name__}: {exc}) — its {len(batches[batch_num - 1])} "
                          f"case(s) will be picked up by the coverage-gap repair pass.")
    else:
        results[1] = _plan_one_batch(1, batches[0])

    # EVERY batch failing is not a gap, it is an outage — a dead key, an
    # exhausted quota, a provider down hard enough to outlast the per-call
    # retry budget. Raising lets the stage-level policy in orchestrator.runner
    # do its job (auto-retry, or ask the operator), whereas returning [] would
    # hand the repair pass an impossible job and report a systemic failure as
    # forty individually unplanned cases.
    if failures and not results:
        raise next(iter(failures.values()))

    planned = []
    for batch_num in range(1, len(batches) + 1):
        planned.extend(results.get(batch_num, []))
    return planned


#: Ceiling on how many unplanned cases one repair pass will re-send. A gap of
#: a handful of cases is the ordinary failure this repairs (a batch's model
#: call quietly skipped or renumbered a few rows). A gap of dozens is
#: SYSTEMIC — a bad skill/prompt/model, or truncation at a level re-asking
#: won't fix — and re-sending everything is then just paying the planning
#: bill twice for the same outcome. The remainder is reported, not silently
#: dropped, so the human at the review gate still sees it.
_MAX_REPAIR_CASES = 30

#: Prepended to the repair call's system prompt (the cached skill still does
#: the actual planning work). Says the one thing the skill can't know: these
#: specific rows came back unplanned, and their ids must be echoed EXACTLY —
#: a repaired plan whose id doesn't match an input row joins to nothing
#: downstream and is discarded again by the acceptance check below.
_REPAIR_SYSTEM_NOTE = (
    "REPAIR PASS. The cases below were sent for planning already and came back "
    "with NO plan object. Emit exactly one plan object for EVERY case listed, and "
    "copy each case's 'Test Case ID' verbatim into that plan's 'test_case_id'. "
    "Do not plan any case that is not in this list, and do not merge cases."
)


def _repair_missing_plans(missing_ids: list, test_cases: list, existing_pom_summary: str,
                          prior_context: str, feature_dir: Path) -> tuple:
    """One bounded re-plan pass for cases `_detect_coverage_gaps` found had no
    plan at all. Returns (accepted_plans, record).

    Mirrors the Coding Agent's locator repair posture
    (coding_agent._repair_unverified_locators): ONE extra pass, accepted only
    where it demonstrably helps, and never allowed to make things worse — a
    failed or partial repair leaves the plans that DID come back untouched
    and the run continues, because an unplanned case still gets code
    generated downstream (just from the `_UNASSIGNED_BUCKET`), so a raise here
    would throw away a mostly-good planning run over a recoverable gap.

    "Accepted" is deliberately strict: a repaired plan is kept only if its
    test_case_id is one of the ids we asked about and hasn't already been
    returned in this pass. That drops both hallucinated ids (the UNKNOWN
    failure mode `_detect_coverage_gaps` documents) and a repair that
    re-plans a case that was never missing — neither can improve the join,
    and the latter would silently override the plan the main pass produced.
    """
    wanted = list(dict.fromkeys(missing_ids))
    attempted_ids, deferred_ids = wanted[:_MAX_REPAIR_CASES], wanted[_MAX_REPAIR_CASES:]

    rows_by_id = {c.get("Test Case ID"): c for c in test_cases if c.get("Test Case ID")}
    cases = [rows_by_id[cid] for cid in attempted_ids if cid in rows_by_id]
    if not cases:
        return [], {"attempted": False,
                    "outcome": "skipped — no input row matched the unplanned id(s)"}

    accepted, rejected, wanted_set = [], [], set(attempted_ids)
    seen = set()
    try:
        for plan in plan_cases(
                cases, existing_pom_summary, feature_dir,
                extra_system=_REPAIR_SYSTEM_NOTE, prior_context=prior_context,
                header="Test cases that were NOT planned on the first pass — "
                       "plan every one of them:",
                label="repair batch"):
            case_id = plan.get("test_case_id")
            if case_id not in wanted_set or case_id in seen:
                rejected.append(case_id)
                continue
            seen.add(case_id)
            # Marks the plan as second-pass work for anyone reading
            # planner_doc.json — nothing downstream branches on it.
            plan["repaired"] = True
            accepted.append(plan)
    except Exception as exc:  # noqa: BLE001 — see the docstring: never fatal
        return accepted, {
            "attempted": True,
            "outcome": f"failed after recovering {len(accepted)} plan(s): {exc}",
            "requested": attempted_ids,
            "recovered": sorted(seen),
            "deferred_over_cap": deferred_ids,
        }

    still_missing = [cid for cid in attempted_ids if cid not in seen]
    return accepted, {
        "attempted": True,
        "outcome": (f"recovered {len(accepted)}/{len(attempted_ids)} unplanned case(s)"
                    if accepted else
                    f"recovered nothing for {len(attempted_ids)} unplanned case(s)"),
        "requested": attempted_ids,
        "recovered": sorted(seen),
        "still_missing": still_missing,
        # Ids the repair returned that we did not ask about (hallucinated, or
        # already planned) — dropped rather than merged into the plan list.
        "rejected_ids": [cid for cid in rejected if cid],
        "deferred_over_cap": deferred_ids,
    }


def run(test_cases: list, existing_pom_summary: str, feature_dir: Path,
        element_catalogue: str = "") -> Path:
    """`element_catalogue` is the ledger's real element vocabulary, rendered
    by `qa_pipeline.brain.catalogue`. Without it the Planner names elements
    out of the test case's prose and invents selectors to match — the gate
    then rejects them and the repair loop pays to guess again. Passing it
    turns naming from generation into selection from a closed list.

    Defaults to "" so a caller with no ledger (a direct
    `python agents/automation_planner.py` invocation) behaves exactly as
    before rather than being handed an instruction it cannot follow."""
    prior_context = read_context_summary(feature_dir)

    test_cases = _strip_planning_irrelevant_fields(test_cases)
    plans = plan_cases(test_cases, existing_pom_summary, feature_dir,
                       extra_system=element_catalogue,
                       prior_context=prior_context)

    # Did every case that went in actually come back with a plan? Checked
    # BEFORE live inspection so the warning reaches the console ahead of the
    # browser work, not buried under it. See _detect_coverage_gaps.
    coverage_gaps = _detect_coverage_gaps(test_cases, plans)
    repair_record = {}
    if coverage_gaps and coverage_gaps["missing_plans"]:
        missing = coverage_gaps["missing_plans"]
        print(f"      ⚠️  {len(missing)} case(s) came back with NO plan "
              f"({missing[:5]}) — re-planning them in one repair pass.")
        recovered, repair_record = _repair_missing_plans(
            missing, test_cases, existing_pom_summary, prior_context, feature_dir)
        if recovered:
            plans.extend(recovered)
            # Re-derived rather than patched: the repair can itself come back
            # short, and the artifact/summary below must describe what is
            # actually in `plans` now, not what the first pass produced.
            coverage_gaps = _detect_coverage_gaps(test_cases, plans)
        print(f"      -> coverage-gap repair: {repair_record.get('outcome')}.")

    if repair_record:
        write_json(artifact_path(feature_dir, "planner_coverage_gaps.json"),
                   {**_coverage_report(test_cases, plans), "repair": repair_record})
    elif coverage_gaps:
        write_json(artifact_path(feature_dir, "planner_coverage_gaps.json"), coverage_gaps)
    if coverage_gaps:
        if coverage_gaps["missing_plans"]:
            print(f"      ⚠️  {len(coverage_gaps['missing_plans'])} case(s) are STILL unplanned "
                  f"({coverage_gaps['missing_plans'][:5]}) — the Coding Agent will treat them as "
                  f"unassigned (no target_file, no POM reuse, no DOM inspection).")
        if coverage_gaps["unknown_test_case_ids"]:
            print(f"      ⚠️  {len(coverage_gaps['unknown_test_case_ids'])} plan(s) name a "
                  f"test_case_id that isn't on the sheet "
                  f"({coverage_gaps['unknown_test_case_ids'][:5]}) — these join to nothing "
                  f"downstream and are effectively discarded.")
        if coverage_gaps["duplicate_plans"]:
            print(f"      ⚠️  {len(coverage_gaps['duplicate_plans'])} case(s) were planned more "
                  f"than once ({coverage_gaps['duplicate_plans'][:5]}) — only the last plan "
                  f"survives the downstream id->target_file join.")
        print(f"      See {artifact_path(feature_dir, 'planner_coverage_gaps.json')}.")

    # Deterministic, free check BEFORE any inspection: a plan claiming
    # "reused_pom": ["LoginPage.submit"] is only trustworthy if that name
    # actually appears somewhere in existing_pom_summary — the model can
    # (and in practice does) invent a plausible-sounding method name that
    # ISN'T actually in the summary it was given, especially under batching
    # pressure. Caught here for $0 extra cost; a claim that doesn't hold up
    # overrides needs_dom_inspection to True regardless of what the model
    # itself said, since "reuse an unverified locator" is exactly the
    # silent-hallucination risk this pipeline's "never fabricate" rule
    # (instructions/conventions.md) exists to catch.
    forced_inspection_count = 0
    for plan in plans:
        if plan.get("needs_dom_inspection"):
            continue
        unverified = _verify_reused_pom_claims(plan, existing_pom_summary)
        if unverified:
            plan["reused_pom_unverified"] = unverified
            plan["needs_dom_inspection"] = True
            forced_inspection_count += 1
    if forced_inspection_count:
        print(f"      Overrode {forced_inspection_count} plan(s) to needs_dom_inspection=true — "
              f"their claimed 'reused_pom' name(s) don't actually appear in existing_pom_summary.")

    # Only now, selectively, do live DOM inspection for flagged cases —
    # graded and confidence-corrected (see _inspect_plans), so a snapshot
    # that never reached the target screen is visibly distinct downstream
    # from one that did.
    _inspect_plans(plans)
    unusable = [p for p in plans if p.get("dom_inspection_quality") in _UNUSABLE_GRADES]
    if unusable:
        print(f"      ⚠️  {len(unusable)} live inspection(s) came back unusable "
              f"({sorted({p['dom_inspection_quality'] for p in unusable})}) — "
              f"confidence lowered so the Coding Agent re-inspects those cases.")

    # Spot-check a bounded sample of LOW-CONFIDENCE "false" decisions: a plan
    # that skips inspection because it (wrongly) believes existing_pom_summary
    # already covers this flow gets ZERO verification otherwise — the boolean
    # alone can't distinguish a confident correct skip from a confident wrong
    # one. Missing dom_inspection_confidence (an older/smaller model that
    # ignores the field) defaults to 1.0 — i.e. NOT spot-checked — so this is
    # purely additive and never fires on a skill/model that predates the field.
    low_confidence = [
        p for p in plans
        if not p.get("needs_dom_inspection")
        and _confidence(p.get("dom_inspection_confidence")) < _SPOT_CHECK_CONFIDENCE_THRESHOLD
    ]
    if low_confidence:
        # Seeded (not the global random.sample) so reruns of the same feature
        # spot-check the same cases instead of a different random sample
        # each time — reproducibility matters more here than unpredictability.
        sample = random.Random(42).sample(low_confidence, min(_MAX_SPOT_CHECKS, len(low_confidence)))
        for plan in sample:
            context_text = _inspection_context(plan)
            result = inspect_live_dom(context_text)
            plan["dom_inspection_result"] = result
            plan["dom_inspection_context"] = context_text
            # Graded here too — a spot-check snapshot is what the Coding
            # Agent will read for this case, so it needs the same "is this
            # actually the target screen?" marker as a flagged inspection.
            plan["dom_inspection_quality"] = _grade_inspection(result)
            plan["spot_checked"] = True
        print(f"      Spot-checked {len(sample)}/{len(low_confidence)} low-confidence "
              f"'no inspection needed' plan(s) (dom_inspection_confidence < "
              f"{_SPOT_CHECK_CONFIDENCE_THRESHOLD}).")

    out_path = artifact_path(feature_dir, "planner_doc.json")
    # write_json: utf-8 + atomic. planner_doc.json now embeds real DOM
    # snapshots, whose accessible names carry whatever characters the app's
    # UI actually uses — the locale-codepage default could not encode them.
    write_json(out_path, plans)

    # Each batch above is an INDEPENDENT LLM call with no visibility into any
    # other batch's target_file choices — nothing stops two batches from
    # independently assigning the same target_file to cases that don't
    # actually belong in the same file (coding_agent would then group them
    # together and generate one file mixing unrelated flows/layers). A cheap,
    # deterministic signal for that: the SAME target_file assigned to cases
    # of DIFFERENT layers (UI vs API vs E2E) almost never belongs in one
    # file under normal Playwright conventions — flag it for human review
    # rather than silently letting the Coding Agent merge them.
    collisions = _detect_target_file_collisions(plans)
    if collisions:
        write_json(artifact_path(feature_dir, "planner_target_file_collisions.json"), collisions)
        print(f"      ⚠️  {len(collisions)} target_file collision(s) detected across batches — "
              f"see {artifact_path(feature_dir, 'planner_target_file_collisions.json')}")

    new_ui_count = sum(1 for p in plans if p.get("needs_dom_inspection"))
    spot_checked_count = sum(1 for p in plans if p.get("spot_checked"))
    append_context_summary(
        feature_dir, "automation_planner",
        f"Planned {len(plans)} cases. {new_ui_count} needed live DOM inspection "
        f"(rest reused existing POM); {spot_checked_count} more spot-checked despite "
        f"a 'no inspection needed' call, for low self-reported confidence. "
        f"{len(unusable)} inspection(s) did not reach a usable target screen "
        f"(login page / empty / unavailable). Target files: "
        f"{sorted(set(p.get('target_file', '?') for p in plans))}."
        + (f" ⚠️ {len(collisions)} target_file collision(s) — see "
           f"planner_target_file_collisions.json." if collisions else "")
        + (f" Coverage-gap repair pass: {repair_record.get('outcome')}."
           if repair_record else "")
        + (f" ⚠️ Coverage gaps: {len(coverage_gaps['missing_plans'])} case(s) unplanned, "
           f"{len(coverage_gaps['unknown_test_case_ids'])} unknown id(s), "
           f"{len(coverage_gaps['duplicate_plans'])} duplicate(s) — see "
           f"planner_coverage_gaps.json." if coverage_gaps else "")
    )

    return out_path


def _detect_coverage_gaps(test_cases: list, plans: list) -> dict:
    """Compares the case ids that went IN against the ones that came back.

    Every batch is an independent LLM call asked to emit one plan object per
    case, and nothing verified it did. Two silent failure modes, both seen in
    this pipeline's own artifacts:

      - MISSING: a case the model skipped (or renumbered) has no plan, so
        coding_agent._group_cases_by_target_file drops it into
        `_UNASSIGNED_BUCKET` — where it still gets code generated, just with
        no planned target_file, no reused_pom, and no DOM inspection. That is
        indistinguishable downstream from a case the Planner deliberately
        judged needed nothing, which is exactly the wrong default for a case
        it never actually looked at.
      - UNKNOWN: a plan whose test_case_id matches no input row at all — a
        hallucinated or mistyped id. It joins to nothing in case_index.json,
        so every later stage silently ignores it, and the effort spent
        planning it is simply lost.

    Deterministic and free (no extra LLM call), reported rather than raised:
    the run can still legitimately proceed, and a human at the review gate is
    better placed to judge a partially-planned case than this function is.
    Same posture as _detect_target_file_collisions above.
    """
    input_ids = [c.get("Test Case ID") for c in test_cases if c.get("Test Case ID")]
    planned_ids = [p.get("test_case_id") for p in plans if p.get("test_case_id")]

    missing = [cid for cid in input_ids if cid not in set(planned_ids)]
    unknown = [cid for cid in planned_ids if cid not in set(input_ids)]
    # A duplicate means two batches both planned the same case; the later one
    # wins every downstream dict build, so the earlier plan's target_file is
    # silently discarded.
    duplicated = sorted({cid for cid in planned_ids if planned_ids.count(cid) > 1})

    report = {
        "cases_in": len(input_ids),
        "plans_out": len(planned_ids),
        "missing_plans": missing,
        "unknown_test_case_ids": unknown,
        "duplicate_plans": duplicated,
    }
    # {} means "nothing to report", which is what every `if coverage_gaps:`
    # branch keys off. `_coverage_report` below is for the caller that needs
    # the numbers even when they are all zero — the repair pass, whose
    # artifact has to say a gap was found AND closed, not go silent because
    # the repair worked.
    if not (missing or unknown or duplicated):
        return {}
    return report


def _coverage_report(test_cases: list, plans: list) -> dict:
    """`_detect_coverage_gaps` without the empty-means-clean convention: always
    the full shape, so a clean result is still a readable record."""
    gaps = _detect_coverage_gaps(test_cases, plans)
    if gaps:
        return gaps
    planned = [p.get("test_case_id") for p in plans if p.get("test_case_id")]
    return {
        "cases_in": len([c.get("Test Case ID") for c in test_cases if c.get("Test Case ID")]),
        "plans_out": len(planned),
        "missing_plans": [],
        "unknown_test_case_ids": [],
        "duplicate_plans": [],
    }


def _detect_target_file_collisions(plans: list) -> list:
    """Flags a target_file assigned to cases of more than one distinct
    'layer' (UI/API/E2E) — a strong, cheap signal that two independent
    batches' LLM calls picked the same filename for unrelated flows, since
    normal Playwright conventions don't mix layers in one spec file. Not
    exhaustive (same-layer collisions on genuinely unrelated flows aren't
    caught), but exact-duplicate-filename-different-layer is unambiguous
    enough to flag without needing another LLM call to judge it."""
    by_file = {}
    for p in plans:
        target_file = p.get("target_file")
        if target_file:
            by_file.setdefault(target_file, []).append(p)

    collisions = []
    for target_file, group in by_file.items():
        layers = {p.get("layer") for p in group if p.get("layer")}
        if len(layers) > 1:
            collisions.append({
                "target_file": target_file,
                "layers": sorted(layers),
                "test_case_ids": [p.get("test_case_id") for p in group],
            })
    return collisions


def _cli():
    """Standalone entry point — plan cases straight from a reviewed sheet,
    without running the rest of the pipeline."""
    import argparse

    from agents.coding_cases import filter_automatable_cases, read_test_case_sheet
    from utils.env_config import resolve_app_env

    resolve_app_env()  # DOM inspection below reads APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sheet", required=True, help="Reviewed test case .xlsx path.")
    parser.add_argument("--pom-summary-file", default="",
                         help="Path to POM_SUMMARY.md (existing reusable locators/helpers).")
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--include-manual-cases", action="store_true",
                         help="Also plan rows marked Execution Type=Manual (default: "
                              "only Automated-marked rows are planned).")
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)
    test_cases = filter_automatable_cases(read_test_case_sheet(Path(args.sheet)), args.include_manual_cases)
    pom_summary = read_text_file(args.pom_summary_file)

    out_path = run(test_cases, pom_summary, feature_dir)
    print(f"\n✅ Planner doc written: {out_path}")


if __name__ == "__main__":
    _cli()
