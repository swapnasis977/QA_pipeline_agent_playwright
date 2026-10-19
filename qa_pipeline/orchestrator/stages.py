"""The stage graph — thirteen stages, four phases, fixed order.

Fixed and deterministic, never a model deciding what to do next.
Predictability is the feature: an operator can say in advance what will
happen, a resume knows exactly where it stopped, and a failure is
attributable to one named stage rather than to an emergent plan.

Every model stage is wrapped in `verifiers.repair_loop`, so the shape is
always propose -> verify -> repair -> verify -> accept. Nothing reaches the
Executor unverified.
"""

from __future__ import annotations

import os
import re as _re
import time
from pathlib import Path

from agents import self_healer as _self_healer
from agents.artifact_io import read_json, read_text_file, write_json
from qa_pipeline import brain, verifiers
from qa_pipeline.agents import adapters, framework_scaffolder
from qa_pipeline.brain import catalogue, explore, ingest, writeback
from qa_pipeline.contracts import HEAL_OUTCOME, write_validated

# Moved to qa_pipeline/orchestrator/code_support.py; re-exported so existing imports keep working.
from qa_pipeline.orchestrator.code_support import (  # noqa: F401
    _MAX_REJECTIONS_IN_REPAIR,
    _failing_scope,
    _merge_repair,
    _merge_split_specs,
    _report_missing_test_data,
    _restore_best_attempt,
    _write_repair_history,
    empty_generation_result,
    memoised_verify,
    report_gate_outcome,
)

# Moved to qa_pipeline/orchestrator/heal_support.py; re-exported so existing imports keep working.
from qa_pipeline.orchestrator.heal_support import (  # noqa: F401
    _ENVIRONMENT_LOCATIONS,
    _ENVIRONMENT_MARKERS,
    _HEADLINE_LINES,
    _IMPORT_RE,
    _dependent_specs,
    _heal_diff,
    _imported_page_objects,
    _raw_error_for,
    _regressions,
    _restore_originals,
    _stalled_in_navigation,
    _write_heal_record,
    environment_failure_reason,
    heal_succeeded,
)

# Moved to qa_pipeline/orchestrator/resume_support.py; re-exported so existing imports keep working.
from qa_pipeline.orchestrator.resume_support import (  # noqa: F401
    _rehydrate_code,
    _rehydrate_heal,
    _rehydrate_plan,
    _rehydrate_review,
    _rehydrate_run,
    _rehydrate_test_cases,
)

# Moved to qa_pipeline/orchestrator/review_support.py; re-exported so existing imports keep working.
from qa_pipeline.orchestrator.review_support import (  # noqa: F401
    _classify_every_failure,
    _merge_known_flaky,
    _persist_added_verdicts,
    clear_stale_verdicts,
    nothing_to_review,
)
from qa_pipeline.orchestrator.demo_support import settle_demonstrations
from qa_pipeline.verifiers import repair_loop
from utils import case_index as _case_index
from utils import dom_inspector as _dom_inspector
from utils import failure_dom as _failure_dom
from utils.run_layout import artifact_path

from . import budget, gates
from .runner import Stage, StageFailed

MAX_HEAL_ATTEMPTS = 2


# ===========================================================================
# Phase A — grounding
# ===========================================================================

def stage_ingest(ctx) -> dict:
    report = ingest.from_documents(
        ctx.ledger, ctx.profile.knowledge_dir, ctx.feature_dir)
    # Every chunk failing (bad key, exhausted quota) used to return normally as
    # "0 new card(s)", i.e. a successful ingest of nothing, and the pipeline went
    # on to write cases with no knowledge behind them.
    if report.get("chunks") and not report.get("cards") and report.get("rejected"):
        raise StageFailed(
            f"Ingest read {report['documents']} document(s) but produced no cards; every "
            f"chunk was rejected. First reason: {report['rejected'][0]}")
    _absorb_recordings(ctx)
    ctx.ledger.save()
    return report


def _absorb_recordings(ctx):
    """Folds human `playwright codegen` recordings into the ledger.

    These are touchpoint 2's output and the highest-confidence source in the
    system — a person demonstrated the flow, so the locators are ground truth
    by construction rather than by inference. The existing
    `utils/recording_parser` already turns them into page objects; this adds
    the ledger half, so a recorded locator is reusable by every future run and
    not just by the file the parser generated.
    """
    directory = ctx.profile.recordings_dir
    if not directory.is_dir() or not any(directory.glob("*.ts")):
        return
    try:
        from utils.recording_parser import generate
        result = generate(directory, ctx.repo_dir)
    except Exception as exc:
        print(f"      ⚠️  Could not process recordings ({exc}) — continuing without them.")
        return

    from utils import locator_verifier
    added = 0
    for entry in result.get("generated", []):
        fallback_screen = entry.get("class_name") or entry.get("test_case_id") or "recorded"
        for prop in entry.get("properties") or []:
            # `recording_parser.generate` returns each property as the tuple
            # (property_name, locator_chain, actions). This read it as a dict,
            # so `prop.get` never applied, `str(prop)` stringified the whole
            # tuple, no locator could be extracted from that, and every card
            # was dropped by the `if not name` guard below. The result was a
            # silent one: recordings parsed fine, page objects were written,
            # and the ledger gained NOTHING — so the pipeline kept asking for
            # a demonstration it had already been given, and the highest-
            # confidence source in the system never reached a single agent.
            if isinstance(prop, dict):
                property_name = prop.get("name") or ""
                expression = prop.get("locator") or ""
            elif isinstance(prop, (tuple, list)) and len(prop) >= 2:
                property_name, expression = str(prop[0]), str(prop[1])
            else:
                continue

            # THE SCREEN A RECORDING'S SECTION HEADER NAMES, when it has one.
            #
            # Every element from a recording used to be filed under the
            # generated CLASS name — "recordedregistertc001page" — so a whole
            # multi-screen journey landed on one imaginary screen, and
            # `resolve_element(name, screen)` could never tell the capture
            # screen's controls from the result screen's. That is how a
            # recapture step got grounded on a "Next Capture button" the
            # demonstration only ever touched two screens later. The human
            # wrote "// --- Capture device (R100) ---" above the right group;
            # `recording_parser` now carries it through, and this is where it
            # becomes the screen the ledger files the card under.
            section = ""
            if isinstance(prop, dict):
                section = prop.get("section") or ""
            elif isinstance(prop, (tuple, list)) and len(prop) >= 4:
                section = str(prop[3] or "")
            screen = section or fallback_screen
            if not expression:
                continue
            parsed = locator_verifier.extract_locators(str(expression))
            name = (parsed[0].get("name") if parsed else "")
            if not name:
                # The recording parser already knows how to name a raw
                # `.locator('#startCapture')` / `[id="4000"]` step. Reuse that
                # rather than falling back to the TypeScript PROPERTY name,
                # which is sanitized for the compiler ("element4000") and is
                # therefore the one string a test case's wording will never
                # fuzzy-match against.
                from utils.recording_parser import _extract_display_name
                name = (_extract_display_name(str(expression))[1] or "")
            name = name or property_name
            if not name:
                continue
            ctx.ledger.upsert(brain.cards.element(
                screen=screen, logical_name=name, locator=str(expression),
                source="recorded", accessible_name=name,
                evidence=f"recording: {entry.get('test_case_id', '')}"))
            added += 1
    if added:
        print(f"      {added} locator(s) from human recordings folded into the ledger "
              f"(highest-confidence source available).")


def stage_explore(ctx) -> dict:
    """Crawls the screens this story is about, before anything is planned."""
    if not ctx.allow_crawl:
        print("      Skipped: no reachable app for this run "
              "(--skip-execution, or no base_url configured).")
        return {"skipped": True}

    # Shared with `qa-pipeline explore` — see `cli._explore_targets`. The two
    # entry points previously built these independently and disagreed: the CLI
    # dropped the profile's `context`, so a declared route map never matched
    # there and every screen came back as the dashboard.
    from qa_pipeline.cli import _explore_targets
    targets = _explore_targets(ctx.profile, ctx.explore_screens or None)
    if ctx.explore_screens:
        print(f"      Crawling {len(targets)} of "
              f"{len(ctx.profile.get('app.screens') or [])} declared screen(s) "
              f"(--screens): {', '.join(t['screen'] for t in targets)}")

    if not targets:
        print("      No screens declared in the profile (app.screens) — the Explorer "
              "will run on demand instead, when the Planner hits an element it "
              "cannot resolve.")
        return {"skipped": True, "reason": "no declared targets"}

    report = explore.crawl(ctx.ledger, targets)
    ctx.ledger.save()

    # A SCREEN THE CRAWLER COULD NOT REACH IS A RESULT, NOT A FOOTNOTE.
    #
    # These were being collected into report["failed"] and then dropped: the
    # stage summary counted only what was VISITED, so an operator whose crawl
    # reached one screen out of four saw "1 screen(s), 20 card(s)" and no
    # indication that anything had failed. Every element on the three missing
    # screens then arrived at the Planner as "not in the ledger", which reads
    # like a planning problem rather than the crawl gap it actually is.
    #
    # Some screens genuinely cannot be crawled — this application's Personal
    # Details and Review screens sit behind a completed biometric capture the
    # crawler cannot perform. That is exactly what touchpoint 2 (a recorded
    # demonstration) exists for, so the honest thing is to name them and say
    # so, rather than let them look like an absence of knowledge.
    failed = report.get("failed") or []
    if failed:
        print(f"      ⚠️  {len(failed)} declared screen(s) could NOT be reached:")
        for failure in failed:
            print(f"           - {failure.get('screen', '?')}: {failure.get('reason', '')}")
        print("         Elements on those screens will arrive unresolved. If a screen "
              "sits behind a step the crawler cannot perform (a biometric capture, a "
              "hardware gate, a seeded record), record it once with "
              "`npx playwright codegen` into the profile's recordings/ folder — that "
              "is touchpoint 2, and it is asked once per screen ever.")
        write_json(ctx.artifact("unreachable_screens.json"), failed)

    return report


# ===========================================================================
# Phase B — authoring
# ===========================================================================

def stage_test_cases(ctx) -> dict:
    sheet = ctx.sheet_path

    # Reuse before regenerate: rebuilding the sheet writes a brand-new
    # workbook with Status blank, destroying exactly the human "Reviewed"
    # marks the gate below requires. Without this the gate is unsatisfiable —
    # every run wipes the marks it then demands.
    if sheet.exists() and "testcase_writer" not in ctx.force_stages:
        print(f"      Reusing existing {sheet.name} "
              f"(pass --force testcase_writer to rebuild).")
        cases = adapters.read_test_cases(sheet)

    elif ctx.import_testcases:
        print(f"      Importing existing test cases from {ctx.import_testcases}")
        cases = adapters.import_test_cases(
            sheet, ctx.story_key, ctx.epic_key, ctx.feature_dir,
            source_file=ctx.import_testcases)

    elif ctx.import_from_jira:
        print(f"      Importing existing test cases from JIRA/Zephyr Scale "
              f"({ctx.import_from_jira})")
        cases = adapters.import_test_cases(
            sheet, ctx.story_key, ctx.epic_key, ctx.feature_dir,
            jira_source=ctx.import_from_jira)

    else:
        # Resolved HERE, on this thread. `ctx.ledger` is built lazily and
        # memoized (see `context.RunContext.ledger`), and the provider below
        # is called from up to four concurrent story threads inside the
        # writer — so the ledger is forced open now and those threads only
        # ever read an already-loaded one.
        ledger = ctx.ledger

        def knowledge_provider(query: str) -> str:
            """Ledger knowledge for one story. The writer supplies the query.

            The stage used to build this block itself and pass the story and
            epic KEYS as the query, then drop the result: `knowledge` was
            assigned, printed, and never handed to `write_test_cases`, which
            had no parameter for it. So the stage reported grounding the
            writer while the writer saw nothing but JIRA and the design doc —
            and even a wired-up version of that call would have retrieved
            almost nothing, because an issue key shares no vocabulary with a
            card. The writer now queries with the story's title and
            acceptance criteria instead.
            """
            block = brain.retrieve.for_agent(ledger, "testcase_writer", query)
            if block:
                print(f"      Grounding the writer in {len(block)} chars of ledger "
                      f"knowledge, drawn from {ledger.stats()['total']} card(s).")
            else:
                # NAME THE SOURCES THAT ACTUALLY REMAIN, not a fixed pair.
                #
                # This said "the writer runs on JIRA and the design doc alone"
                # unconditionally, which became wrong the moment a run could
                # have neither: a document-derived run with an empty ledger
                # reported falling back to JIRA when no JIRA call happens at
                # all, and to a design doc that had not been supplied. An
                # operator reading it would go looking for a JIRA problem
                # that does not exist.
                remaining = []
                if ctx.story_key and not ctx.derive_from_documents:
                    remaining.append("the JIRA story")
                if ctx.design_doc:
                    remaining.append("the design doc")
                if ctx.api_spec:
                    remaining.append("the API spec")
                if ctx.figma_url:
                    remaining.append("Figma")
                if ctx.reference_testcases:
                    remaining.append("the coverage baseline")
                total = ledger.stats()["total"]
                reason = ("holds no cards yet — run `qa-pipeline ingest`"
                          if not total else
                          f"holds {total} card(s), none relevant to this story")
                print(f"      Ledger {reason}; the writer runs on "
                      + (", ".join(remaining) if remaining else "NOTHING ELSE")
                      + " alone.")
            return block

        # Existing cases as a COVERAGE BASELINE, not as content to reproduce:
        # the writer is asked for what they do NOT already cover. Distinct
        # from `--import-testcases`, which REPLACES generation — this one
        # informs it, so unlike the import flags it is not exclusive with
        # generating.
        reference_cases = []
        if ctx.reference_testcases:
            reference_cases = adapters.read_test_cases(ctx.reference_testcases)
            print(f"      Coverage baseline: {len(reference_cases)} existing case(s) "
                  f"from {Path(ctx.reference_testcases).name} — asking the writer "
                  f"for what these do NOT already cover.")

        # An EMPTY story key when the run has no story, not the DOC-1
        # placeholder the feature directory is named from: the writer treats
        # any non-empty key as a real tracker item and fetches it, so passing
        # the placeholder through sent it to JIRA looking for issue "DOC-1".
        # It re-synthesizes the same key itself, so the sheet filename
        # (`{epic}_{story}_TestCases.xlsx`) is identical either way.
        writer_story_key = "" if ctx.derive_from_documents else (
            getattr(ctx, "story_source", "") or ctx.story_key)

        cases = adapters.write_test_cases(
            sheet, writer_story_key, ctx.epic_key, ctx.feature_dir,
            figma_url=ctx.figma_url, design_doc=ctx.design_doc,
            api_spec=ctx.api_spec, knowledge_provider=knowledge_provider,
            reference_cases=reference_cases,
            # Whether the ledger actually HOLDS anything, not merely
            # whether a provider was wired — the writer uses this to decide
            # if there is any input at all to derive cases from.
            knowledge_available=ledger.stats().get("total", 0) > 0,
            feature_link=ctx.feature_link)

    # The story's acceptance criteria, so the coverage gate can actually run.
    # Without them `coverage_check` skips silently, which reads exactly like a
    # pass — and the check it skips (does any criterion have no test case?) is
    # the one thing a human cannot do by reading forty prose cases.
    story_context = read_json(
        ctx.artifact(f"story_context_{ctx.story_key}.json"), default={}) or {}
    criteria = story_context.get("acceptance_criteria") or None
    if not criteria:
        print("      No acceptance criteria recorded for this story — the coverage "
              "gate can only check for duplicates, not for gaps.")

    result = verifiers.gate_test_cases(
        cases, criteria,
        strict_coverage=ctx.profile.get("gates.strict_coverage", False))
    for warning in result.warnings:
        print(f"      ⚠️  {warning}")
    if not result.ok:
        for error in result.errors:
            print(f"      ✗ {error}")
        ctx.metrics.gate_failed("coverage_check")
        # A duplicate id is documented as "a hard failure, always" but was only
        # printed: rows then collide on one id downstream (heal matching, bug
        # keys, screenshot lookup) and a human could still approve the sheet.
        duplicates = [e for e in result.errors if e.startswith("duplicate test_case_id")]
        if duplicates:
            raise StageFailed("The sheet has duplicate Test Case IDs, which every later "
                              "stage joins on: " + "; ".join(duplicates[:5]))

    write_validated(ctx.artifact("test_cases.json"), "test_case", cases, many=True)
    failed = read_json(ctx.artifact("testcase_writer_failed_stories.json"), default=[]) or []
    return {"cases": cases, "verify": result.to_dict(), "sheet": str(sheet),
            "failed_stories": [f.get("story_key") for f in failed if isinstance(f, dict)]}


def stage_review_gate(ctx) -> dict:
    gates.enforce_review(
        ctx.sheet_path,
        required=ctx.profile.get("gates.require_human_review", True))
    return {"approved": True}


def stage_jira_upload(ctx) -> dict:
    project = ctx.profile.get("connectors.jira.project", "")
    issue_type = ctx.profile.get("connectors.jira.issue_type", "Test")
    path = adapters.upload_to_jira(ctx.sheet_path, ctx.feature_dir, project, issue_type)
    # Carry each Jira key back to the sheet and the workspace, so every script maps to its Jira test.
    from qa_pipeline import jira_link
    linked = jira_link.link(ctx.profile, path, sheets=[ctx.sheet_path])
    if linked["keys"]:
        print(f"      Linked {linked['keys']} Jira key(s) to the sheet and {linked['map']}")
    return {"path": str(path)}


# ===========================================================================
# Phase C — build
# ===========================================================================

def stage_scaffold(ctx) -> dict:
    return framework_scaffolder.run(ctx.repo_dir)


def stage_plan(ctx) -> dict:
    cases = ctx.state.get("testcase_writer", {}).get("cases") or \
        adapters.read_test_cases(ctx.sheet_path)
    automatable = adapters.filter_automatable(cases, ctx.include_manual)

    skipped = len(cases) - len(automatable)
    if skipped:
        print(f"      {skipped} Manual-marked case(s) skipped "
              f"(pass --include-manual to automate them too).")
    if not automatable:
        return {"plans": [], "unresolved": [], "cases": cases}

    # LOGIN SMOKE CHECK — pay for ONE login attempt now, not 4 repair
    # attempts x every case. A bad login (empty credentials, a broken IDP, a
    # wrong APP_BASE_URL) used to be discovered independently by each case's
    # own repair loop — on an 89-case run that's up to 356 wasted LLM calls
    # before the very same root cause is reported the 89th time. `allow_crawl`
    # gates this: a run with crawling disabled never needs a live login.
    if ctx.allow_crawl and os.environ.get("APP_BASE_URL"):
        login_ok, reason = _dom_inspector.verify_login()
        if not login_ok:
            raise RuntimeError(
                f"[HALT] Cannot log into the app under test — {reason}.\n"
                f"       Every one of the {len(automatable)} case(s) below would "
                f"otherwise independently rediscover\n"
                f"       this same failure through its own repair loop. Fix the "
                f"login (check APP_BASE_URL/\n"
                f"       APP_TEST_USER/APP_TEST_PASSWORD and TEST_ENV in .env), "
                f"then re-run the same command —\n"
                f"       it resumes from here.")

    pom_summary = read_text_file(Path(ctx.repo_dir) / "POM_SUMMARY.md")

    # One full planning pass, then repair PER CASE. The whole-stage loop that
    # used to sit here re-planned every case whenever any single one failed a
    # gate — the exact behaviour `repair_loop.run_per_item` exists to avoid
    # ("one malformed case out of forty should cost one repair call for that
    # case, not a regeneration of the whole sheet"), and it perturbed the
    # plans that had already passed.
    # The Planner's element vocabulary. Without it, it names elements out of
    # the test case's prose and invents selectors to match; the gate then
    # rejects them and the repair loop pays to guess again with no new
    # information. See qa_pipeline.brain.catalogue.
    element_catalogue = catalogue.render(ctx.ledger)
    if element_catalogue:
        screens = catalogue.collect(ctx.ledger)
        total = sum(len(v) for v in screens.values())
        print(f"      Planner vocabulary: {total} groundable element(s) "
              f"across {len(screens)} screen(s).")
    else:
        print("      ⚠️  The ledger has no groundable elements — the Planner is "
              "naming elements blind, and the plan gate will reject most of them. "
              "Run `explore`, or add a recording (touchpoint 2).")

    first_pass = adapters.plan(automatable, pom_summary, ctx.feature_dir,
                               element_catalogue=element_catalogue)
    case_by_id = {c.get("test_case_id"): c for c in automatable}

    def produce_one(plan_entry, repair):
        # No repair prompt yet = the first pass's plan for this case, already
        # paid for. Only a FAILED gate costs a second call, and that call now
        # carries the gate's diagnosis (the missing seam: the repair text used
        # to be discarded, so every retry re-asked the identical question).
        if not repair:
            return plan_entry
        case = case_by_id.get(plan_entry.get("test_case_id"))
        if not case:
            return plan_entry
        repaired = adapters.replan([case], pom_summary, ctx.feature_dir, repair,
                                   element_catalogue=element_catalogue)
        # A repair that came back about some other case (or about nothing) is
        # not an improvement to THIS plan — keep the one we have and let the
        # gate fail it honestly rather than swapping in an unrelated plan.
        for candidate in repaired:
            if candidate.get("test_case_id") == plan_entry.get("test_case_id"):
                # MERGED, not substituted. `replan` re-runs the planning call
                # and nothing else — it never drives a browser — so a plain
                # swap deleted the first pass's DOM snapshot for this case.
                # See adapters.merge_replanned.
                return adapters.merge_replanned(plan_entry, candidate)
        return plan_entry

    def verify_plan(_plan, artifact):
        result = verifiers.gate_plan(artifact, ctx.ledger, allow_crawl=ctx.allow_crawl)
        ctx.note_crawls(result)  # charged as they happen, not after the stage
        return result

    outcomes = repair_loop.run_per_item(
        first_pass, produce_one, verify_plan,
        max_repairs=ctx.max_repairs, label="plan",
        key=lambda p: p.get("test_case_id") or "<unknown>",
        should_stop=lambda: budget.over_budget(ctx))

    tally = outcomes.pop("_summary")
    _write_repair_history(ctx, "automation_planner",
                          {key: o.history for key, o in outcomes.items()})
    # Order is preserved from the first pass: the Coding Agent groups by
    # target_file, and a reshuffled plan list changes how cases group.
    plans = [outcomes[p.get("test_case_id") or "<unknown>"].artifact or p
             for p in first_pass]
    outcome = repair_loop.RepairOutcome(
        plans, verifiers.combine("gate_plan",
                                 [o.result for o in outcomes.values() if o.result]),
        max((o.attempts for o in outcomes.values()), default=1))
    if tally["repaired"]:
        print(f"      {tally['repaired']} plan(s) repaired against the gate's "
              f"diagnosis; {tally['failed']} still failing.")

    unresolved = []
    for plan_entry in plans:
        for element in plan_entry.get("elements") or []:
            if not element.get("locator"):
                unresolved.append({
                    "test_case_id": plan_entry.get("test_case_id"),
                    "logical_name": element.get("logical_name"),
                    "screen": element.get("screen"),
                    "reason": ctx.ledger.unresolved_reason(
                        element.get("logical_name", ""), element.get("screen", "")),
                })
                ctx.metrics.rung(4)
            else:
                ctx.metrics.rung(1 if element.get("card_id") else 2)

    ctx.metrics.data["cases_planned"] = len(plans)
    ctx.metrics.absorb_verify(outcome.result)
    ctx.ledger.save()

    write_validated(ctx.artifact("automation_plan.json"), "automation_plan",
                    plans, many=True)
    # The existing Coding Agent reads the Planner's own file, so it is kept in
    # place and in its original shape; the contract-validated copy above is
    # what every NEW stage reads.
    planner_doc_path = artifact_path(ctx.feature_dir, "planner_doc.json")
    # ALWAYS rewritten. The Coding Agent reads THIS file, so a repaired plan
    # that only lived in memory would have been verified here and then never
    # used — the gate's work thrown away and the original, failing plan
    # generated from instead. Normalization preserves the Planner's own extra
    # fields (dom_inspection_result and friends), so rewriting it costs
    # nothing the Coding Agent needs.
    #
    # The old `if tally["repaired"] or not exists()` guard looked like a
    # harmless optimization and was the second half of a real divergence: on
    # the ordinary no-repair run it left the RAW planner output on disk, so
    # `target_file` never received the normalization the contract requires
    # and the Coding Agent grouped by a different string than every other
    # stage used. (The reader normalizes too now — see
    # coding_agent._load_planner_plans — but the two files should not
    # disagree in the first place.)
    write_json(planner_doc_path, plans)

    if unresolved:
        write_json(ctx.artifact("needs_demonstration.json"), unresolved)

    index = _case_index.build([adapters._case_to_planner_row(c) for c in cases], plans)
    _case_index.write(ctx.feature_dir, index)

    return {"plans": plans, "unresolved": unresolved, "cases": cases,
            "case_index": index, "planner_doc": str(planner_doc_path)}


def stage_code(ctx) -> dict:
    plan_state = ctx.state.get("automation_planner", {})
    plans = plan_state.get("plans") or []
    if not plans:
        return {"files": [], "unresolved": [], "skipped": "nothing planned"}

    # Rung 4: ask for demonstrations BEFORE generating, not after. Generating
    # code around an element nobody can locate produces a file that compiles,
    # runs, and times out — the exact expensive failure the gates exist to
    # prevent.
    # Re-checked first, deterministically: a recording added since the plan was
    # made is absorbed and only what is STILL unresolved is asked for again.
    unresolved = settle_demonstrations(ctx, plan_state, _absorb_recordings)
    if unresolved and ctx.profile.get("gates.require_locator_grounding", True):
        gates.request_demonstrations(unresolved, ctx.profile.recordings_dir)

    pom_summary = read_text_file(Path(ctx.repo_dir) / "POM_SUMMARY.md")
    conventions = ctx.profile.conventions_file
    kind_by_case = {p.get("test_case_id"): p.get("test_kind", "e2e") for p in plans}
    # EVERY screen the case visits, in the order the plan lists them — not
    # just the first. A locator for the Review screen was being resolved
    # against the Dashboard, which is how a correct `Search, button` was
    # failed for "its real accessible name is 'Click here to Search'". See
    # locator_check._resolve_in_screens. The first entry is still the flow's
    # entry point, which is what a crawl needs to navigate to.
    screens_by_case = {}
    for plan_entry in plans:
        ordered = []
        for element in plan_entry.get("elements") or []:
            name = element.get("screen")
            if name and name not in ordered:
                ordered.append(name)
        screens_by_case[plan_entry.get("test_case_id")] = ordered
    screen_by_case = {case: names[0] for case, names in screens_by_case.items() if names}

    #: The files the BEST attempt so far produced, so a repair EDITS them
    #: instead of rewriting from scratch. The diagnosis alone told the agent
    #: what was wrong but never showed it what it had written, so each
    #: attempt was an independent 200-line rewrite — which is why repairs
    #: oscillated (13 -> 5 -> 10 -> 9 problems on one real case) rather than
    #: converging.
    #:
    #: BEST, not last: `repair_loop.run` below returns the best-scoring
    #: attempt across the whole loop, not the final one, because a repair can
    #: regress (ARCHITECTURE.md's documented case — attempt 2 passes, attempt
    #: 3 produces nothing). `last_attempt` used to be overwritten after every
    #: single call regardless of whether it was an improvement, so a
    #: regression on attempt 2 fed attempt 3 a WORSE baseline than attempt 1's
    #: — the repair machinery quietly diverging from the best code already on
    #: file while the orchestrator still planned to hand that attempt-1 code
    #: downstream. Scored with the loop's own `_score` so "best" means the
    #: same thing in both places.
    last_attempt = {}
    best_score = None
    last_produced = []
    best_files, best_results = [], None

    def verify(files):
        # ZERO SPECS IS A FAILURE, NOT AN EMPTY PASS. Checked before the
        # per-file loop, which has nothing to iterate in that case and so
        # combines to PASS. See empty_generation_result.
        if not any(f.get("kind") == "spec" for f in files):
            return [empty_generation_result(
                [p.get("test_case_id") for p in plans],
                read_json(ctx.artifact("coding_agent_rejected_files.json"),
                          default=[]) or [])]

        results = []
        for generated in files:
            first_case = (generated.get("test_case_ids") or [""])[0]
            results.append(verifiers.gate_code(
                generated, ctx.ledger, ctx.repo_dir,
                screen=screen_by_case.get(first_case, ""),
                screens=screens_by_case.get(first_case) or [],
                test_kind=kind_by_case.get(first_case, ""),
                allow_crawl=ctx.allow_crawl,
                plans=plans))
            ctx.note_crawls(results[-1])  # charged as they happen, not after the stage
        return results

    # `produce` scores each attempt and `repair_loop.run` gates it again: one
    # gate run per distinct set of files, not two.
    verify = memoised_verify(verify)

    def produce(repair):
        # `repair` is the gate's diagnosis of the PREVIOUS attempt, threaded
        # into the agent's system prompt. Dropping it — as this did — meant
        # every repair attempt re-issued an identical call and bought an
        # identical failure at full price.
        nonlocal best_score, last_produced, best_files, best_results
        common = dict(
            sheet_path=ctx.sheet_path, cycle_id=ctx.cycle_id,
            planner_doc_path=plan_state.get("planner_doc") or artifact_path(ctx.feature_dir, "planner_doc.json"),
            conventions_path=conventions if conventions.is_file() else None,
            feature_dir=ctx.feature_dir, repo_dir=ctx.repo_dir,
            pom_summary=pom_summary, include_manual=ctx.include_manual,
            design_doc=ctx.design_doc, repair_prompt=repair or "")

        scope = _failing_scope(best_files, best_results) if repair else None
        if scope:
            # TARGETED REPAIR: regenerate only the cases whose files failed, keep
            # every file that passed. A rejected file is removed only once a
            # regenerated one covers its cases (see below).
            failing, case_ids = scope
            print(f"      coding_agent: repairing only the {len(failing)} rejected file(s) "
                  f"({len(case_ids)} case(s)); keeping the {len(best_files) - len(failing)} "
                  f"that passed.")
            partial = adapters.generate_code(
                **common, only_case_ids=case_ids,
                prior_files={g["path"]: g.get("content", "") for g in failing
                             if g.get("path")} or None)["files"]
            from agents.artifact_io import resolve_within
            produced, delete_paths, restore = _merge_repair(best_files, failing, partial)
            for path in delete_paths:
                target = resolve_within(ctx.repo_dir, path)
                if target is not None:
                    target.unlink(missing_ok=True)
            for generated in restore:
                target = resolve_within(ctx.repo_dir, generated.get("path"))
                if target is not None and generated.get("content"):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(generated["content"], encoding="utf-8")
        else:
            produced = adapters.generate_code(
                **common, prior_files=dict(last_attempt) or None)["files"]

        # Scored the same way repair_loop scores it (deterministic, cheap —
        # calling the gate here too, ahead of repair_loop's own call, costs
        # no model tokens), so "best" agrees between the two places that
        # track it. Only an IMPROVEMENT (or the first attempt) replaces the
        # baseline the next repair edits from.
        last_produced = produced
        raw = verify(produced)
        score = repair_loop._score(verifiers.combine("gate_code", raw) if isinstance(raw, list) else raw)
        if best_score is None or score < best_score:
            best_score = score
            best_files = produced
            best_results = raw if isinstance(raw, list) and len(raw) == len(produced) else None
            last_attempt.clear()
            last_attempt.update({f["path"]: f.get("content", "")
                                 for f in produced if f.get("path")})
        return produced

    outcome = repair_loop.run(produce, verify, max_repairs=ctx.max_repairs,
                              label="coding_agent",
                              should_stop=lambda: budget.over_budget(ctx))
    report_gate_outcome(outcome)
    files = outcome.artifact or []
    restored = _restore_best_attempt(ctx, files, last_produced)
    if restored:
        print(f"      coding_agent: the last attempt was not the best one — "
              f"restored {restored} file(s) from the best attempt to the workspace.")
    _write_repair_history(ctx, "coding_agent", {"<generation>": outcome.history})

    if outcome.attempts == 1 and outcome.ok:
        ctx.metrics.bump("first_attempt_ok", len(files))
    elif outcome.declined:
        ctx.metrics.bump("declined")
    elif outcome.attempts > 1:
        ctx.metrics.bump("repaired", len(files))

    ctx.metrics.data["files_generated"] = len(files)
    ctx.metrics.absorb_verify(outcome.result)
    budget.check_files(ctx, files)
    ctx.ledger.save()

    files = _merge_split_specs(ctx, files)
    write_validated(ctx.artifact("generated_files.json"), "generated_file",
                    files, many=True)
    _report_missing_test_data(ctx, files)

    # AN ACCOUNT/QUOTA OUTAGE IS A FAILED RUN, NOT AN EMPTY ONE.
    #
    # Every chunk failure is caught per-chunk so one bad chunk cannot discard
    # nine others' paid-for files (see coding_agent's completion loop), and
    # that swallowing is correct for a CONTENT failure. It is wrong for an
    # infra one: when the provider refuses every call, the stage produced no
    # files for a reason that has nothing to do with the test cases, and
    # returning normally reports that as success. Observed exactly that way —
    # a workspace spend cap refused all 18 chunks and `run` still exited 0,
    # so CI branching on the exit code would have gone green on a run that
    # generated nothing.
    #
    # Raising here (rather than at the agent) keeps the diagnostics the agent
    # already writes and only changes what the ORCHESTRATOR concludes, so the
    # infra-failures file is on disk for the operator either way. Deliberately
    # not gated on `files` being empty: a partial outage that cost some cases
    # their code is still an outage, and those cases are recoverable by
    # re-running the stage once the provider issue is fixed.
    infra_failures = read_json(
        ctx.artifact("coding_agent_infra_failures.json"), default=[]) or []
    if infra_failures:
        lost = sum(len(c.get("test_case_ids") or []) for c in infra_failures)
        raise StageFailed(
            f"coding_agent: {len(infra_failures)} chunk(s) covering {lost} case(s) "
            f"failed for an account/quota/rate-limit reason that outlasted the retry "
            f"budget — {len(files)} file(s) generated. This is a provider/billing "
            f"failure, NOT a modelling one: fix the provider issue and re-run this "
            f"stage. See {ctx.artifact('coding_agent_infra_failures.json')}.")

    return {"files": files, "verify": outcome.result.to_dict() if outcome.result else None,
            "declined": outcome.declined}


# ===========================================================================
# Phase D — run and learn
# ===========================================================================

def _archive_evidence(ctx) -> int:
    """Copies the first run's screenshots/traces somewhere a re-run cannot erase.

    Playwright empties `test-results/` at the start of every run, and each heal
    re-runs the spec, so by the time the Bug Reporter looked, the evidence of an
    unhealed failure was gone and the bug said "none captured". The report's
    attachment paths are rewritten to the archived copies. Returns files kept.
    """
    import shutil
    report_path = ctx.artifact("run_results.json")
    report = read_json(report_path, default=None)
    if not isinstance(report, dict):
        return 0
    evidence = ctx.artifact("evidence")
    kept = 0

    def walk(suites):
        nonlocal kept
        for suite in suites or []:
            for spec in suite.get("specs") or []:
                for test in spec.get("tests") or []:
                    for attempt, result in enumerate(test.get("results") or []):
                        for attachment in result.get("attachments") or []:
                            source = Path(str(attachment.get("path") or ""))
                            if not source.is_file():
                                continue
                            # Per spec FILE, case AND attempt: Playwright names the
                            # files identically every attempt (trace.zip,
                            # test-failed-1.png), so keying on the title alone made
                            # a retry's evidence overwrite - or be skipped in favour
                            # of - the first attempt's, and two specs sharing a title
                            # collided.
                            stem = Path(str(spec.get("file") or "spec")).stem
                            label = _re.sub(r"[^\w.-]+", "_", str(spec.get("title") or "case"))[:80]
                            bucket = evidence / stem / label / f"attempt{attempt}"
                            bucket.mkdir(parents=True, exist_ok=True)
                            target = bucket / source.name
                            if not target.exists():
                                shutil.copy2(source, target)
                            attachment["path"] = str(target)
                            kept += 1
            walk(suite.get("suites"))

    walk(report.get("suites"))
    if kept:
        write_json(report_path, report)
        print(f"      Archived {kept} screenshot/trace file(s) under {evidence} "
              f"(heal re-runs would have erased them).")
    return kept


def stage_execute(ctx) -> dict:
    files = ctx.state.get("coding_agent", {}).get("files") or []
    test_files = sorted({f["path"] for f in files if f.get("kind") == "spec" and f.get("path")})

    if not test_files:
        # "No scope" must never become "run everything". Falling through to a
        # full-suite run here once executed 39 unrelated spec files from
        # previous features against the live app, headed, for hours, to verify
        # code that run had never produced.
        #
        # BUT "nothing to run" IS NOT ALWAYS BENIGN, and conflating the two
        # cases is how a run that achieved nothing reported success. Observed:
        # the Coding Agent spent 77k tokens over three repair cycles, had every
        # attempt rejected, wrote zero spec files — and because this stage
        # returned a plain `skipped`, the pipeline continued and exited 0. A
        # green CI run for a feature with no test is worse than a red one.
        #
        # So: if the Planner produced plans, specs were OWED. Their absence is
        # a failure of this run, not an empty scope.
        planned = ctx.state.get("automation_planner", {}).get("plans") or []
        if planned:
            # RAISED, not returned. A returned {"ran": False} only makes the
            # stage `halt`, which breaks the stage loop and still exits 0 —
            # the exact silent-success path this guard exists to close.
            # Raising routes through `runner.run_stage`, which logs it to
            # pipeline_errors.json and re-raises as StageFailed → exit code 1.
            ids = ", ".join(str(p.get("test_case_id")) for p in planned[:8])
            raise RuntimeError(
                f"{len(planned)} case(s) were planned but the Coding Agent produced no "
                f"spec file for any of them ({ids}), so there is nothing to execute. "
                f"See coding_agent_rejected_files.json for why each generation was "
                f"rejected. Failing the run rather than reporting an empty success.")
        print("      Skipped: this run produced no spec files, so there is nothing "
              "to execute. (Not running the repo's existing suite — those tests "
              "belong to other runs.)")
        return {"ran": False, "skipped": "no spec files generated this run"}

    result = adapters.execute(ctx.repo_dir, ctx.feature_dir, test_files=test_files,
                              workers=ctx.workers, shard=ctx.shard,
                              fresh_reports=True)
    if result.get("ran"):
        _archive_evidence(ctx)
        stalled = _stalled_in_navigation(read_json(ctx.artifact("run_results.json"), default={}) or {})
        if stalled:
            write_json(ctx.artifact("environment_failures.json"), stalled)
            total = len([c for c in result.get("cases") or [] if c.get("test_case_id")])
            print(f"      ⚠️  {len(stalled)} of {total} case(s) failed before their own steps "
                  f"(network/application/sign-in), not in the test. Re-run before triaging: "
                  f"{', '.join(sorted(stalled)[:6])}{' ...' if len(stalled) > 6 else ''}. "
                  f"See environment_failures.json.")

    if not result["ran"]:
        # RAISED, not returned — the same reasoning as the "planned but no
        # specs" guard above, one step later. Returning {"ran": False} only
        # makes the stage `halt`: the loop breaks and the process still exits
        # 0, so a run whose test environment was broken (Playwright config
        # error, missing browser binaries, npx not on PATH) reported success
        # having verified nothing. Specs were generated and execution was
        # asked for — `--skip-execution` is how an operator says they have no
        # environment, and this stage does not even apply in that case.
        raise RuntimeError(
            f"The Executor could not run the suite, so nothing this run generated "
            f"has been verified: {result['error']}\n"
            f"  This is an environment failure, not a test failure "
            f"(failure_kind={result.get('failure_kind') or 'unknown'}). Fix the "
            f"workspace, or re-run with --skip-execution if you have no live test "
            f"environment and only want the code generated.")

    if ctx.profile.get("execution.allure", True):
        try:
            from agents.executor import generate_allure_report
            allure = generate_allure_report(Path(ctx.repo_dir))
            if allure.get("error"):
                print(f"      ⚠️  Allure report not generated: {allure['error']}")
            else:
                result["allure_report"] = allure.get("report_dir", "")
                print(f"      Allure report: {result['allure_report']}")
        except Exception as exc:
            print(f"      ⚠️  Allure report not generated: {exc}")

    passed = sum(1 for c in result["cases"] if c["status"] == "passed")
    ctx.metrics.data["cases_passed_first_run"] = passed
    write_validated(ctx.artifact("run_result.json"), "run_result", result)
    return result


def stage_review(ctx) -> dict:
    run_result = ctx.state.get("executor") or {}
    if not run_result.get("ran"):
        return {"classifications": [], "skipped": "executor did not run"}

    files = ctx.state.get("coding_agent", {}).get("files") or []
    known_flaky = read_json(ctx.artifact("known_flaky.json"), default=[]) or []
    conventions = ctx.profile.conventions_file

    raw_report = read_json(ctx.artifact("run_results.json"), default={}) or {}
    if nothing_to_review(run_result, raw_report):
        # No failing case, so there is no verdict to buy: no model call.
        stale = artifact_path(ctx.feature_dir, "review_report.json")
        clear_stale_verdicts(stale)
        write_validated(ctx.artifact("classifications.json"), "classification", [], many=True)
        print("      Every case passed - Reviewer not called.")
        out = {"classifications": [], "skipped": "all green"}
        if stale.is_file():  # lets the Bug Reporter refresh a previous run's list
            out["review_path"] = str(stale)
        return out
    classifications, review_path = adapters.review(
        raw_report or run_result, files,
        conventions if conventions.is_file() else None,
        known_flaky, ctx.feature_dir,
        case_index=ctx.state.get("automation_planner", {}).get("case_index"))

    reviewed_ids = {c.get("test_case_id") for c in classifications}
    classifications = _classify_every_failure(raw_report or run_result, classifications)
    _persist_added_verdicts(review_path, [c for c in classifications
                                          if c.get("test_case_id") not in reviewed_ids])
    _merge_known_flaky(ctx, classifications, known_flaky)
    write_validated(ctx.artifact("classifications.json"), "classification",
                    classifications, many=True)
    return {"classifications": classifications, "review_path": str(review_path)}


def stage_heal(ctx) -> dict:
    classifications = ctx.state.get("reviewer", {}).get("classifications") or []
    fixable = [c for c in classifications if c.get("verdict") == "fixable_test_issue"]
    stalled = _stalled_in_navigation(read_json(ctx.artifact("run_results.json"), default={}) or {})
    skipped_outcomes = []
    if stalled:
        for entry in [c for c in fixable if c.get("test_case_id") in stalled]:
            skipped_outcomes.append(HEAL_OUTCOME.coerce({
                "test_case_id": entry.get("test_case_id"), "file": entry.get("file", ""),
                "attempted": False, "healed": False, "declined": True,
                "decline_reason": stalled[entry.get("test_case_id")]}))
        fixable = [c for c in fixable if c.get("test_case_id") not in stalled]
        print(f"      Not healing {len(skipped_outcomes)} case(s) that never reached their "
              f"screen (navigation/environment stall) — they are reported, not patched.")
    if not fixable:
        print("      No fixable_test_issue cases — nothing to heal.")
        return {"outcomes": skipped_outcomes, "healed": [],
                "still_failing": [o["test_case_id"] for o in skipped_outcomes]}

    files = ctx.state.get("coding_agent", {}).get("files") or []
    plans = ctx.state.get("automation_planner", {}).get("plans") or []

    # THE JOIN, as one lookup. The contract guarantees every generated file
    # declares the case ids it implements, which is what replaces the old
    # four-tier `_resolve_case_file` fallback chain.
    file_by_case = {case_id: f for f in files for case_id in (f.get("test_case_ids") or [])}
    content_by_path = {f["path"]: f.get("content", "") for f in files}
    nav_by_case = {p.get("test_case_id"): " ".join(p.get("navigation_keywords") or [])
                   for p in plans}

    # THE LEDGER, FOR THE HEALER. Every other agent is grounded in it — the
    # Planner is handed this same rendering so it selects from a closed list
    # instead of inventing — while the one agent whose entire job is to
    # produce a correct locator saw only the broken file, the failure text and
    # a single DOM snapshot. When that snapshot came back empty it had nothing
    # left and could only decline, with the element it needed sitting in the
    # ledger, crawled and confirmed. Scoped to the screens this story's plans
    # actually touch, so the block stays small and on-topic.
    screens = sorted({e.get("screen") for p in plans
                      for e in (p.get("elements") or []) if e.get("screen")})
    known_locators = catalogue.render(ctx.ledger, screens=screens or None, only_screens=True)

    raw_report = read_json(ctx.artifact("run_results.json"), default={}) or {}
    fixable_ids = {c.get("test_case_id") for c in fixable if c.get("test_case_id")}
    dom_by_case = _failure_dom.extract(raw_report, fixable_ids) or {}
    timeline_by_case = _failure_dom.extract_timeline(raw_report, fixable_ids) or {}
    if dom_by_case:
        print(f"      Using the DOM captured at failure for {len(dom_by_case)}/"
              f"{len(fixable_ids)} case(s) — no re-navigation needed.")

    # Group by file: several cases sharing one broken login flow must cost one
    # heal and one verification, not five of each overwriting one another.
    groups = {}
    outcomes, healed = list(skipped_outcomes), []
    still_failing = [o["test_case_id"] for o in skipped_outcomes]
    for entry in fixable:
        case_id = entry.get("test_case_id")
        generated = file_by_case.get(case_id)
        if not generated:
            still_failing.append(case_id)
            outcomes.append(HEAL_OUTCOME.coerce({
                "test_case_id": case_id, "file": entry.get("file", ""),
                "attempted": False, "healed": False,
                "declined": True,
                "decline_reason": "no generated file declares this case id",
            }))
            continue
        groups.setdefault(generated["path"], []).append(entry)

    # A WALL-CLOCK CEILING for the heal stage. The token budget is only checked
    # BETWEEN stages, so this stage - groups x attempts x a re-run that can itself
    # wait out a 30-minute timeout - had no limit at all. 0 (the default) keeps the
    # old behaviour; set budgets.max_run_seconds in the profile to enforce one.
    # budgets.max_heal_attempts was documented and defaulted in the profile but never
    # read: the heal stage always used the module constant. Honoured now, clamped to a
    # sane range so a typo cannot mean 0 attempts or a runaway.
    try:
        max_attempts = int(ctx.profile.get("budgets.max_heal_attempts", MAX_HEAL_ATTEMPTS))
    except (TypeError, ValueError):
        max_attempts = MAX_HEAL_ATTEMPTS
    max_attempts = max(1, min(max_attempts, 4))
    max_seconds = float(ctx.profile.get("budgets.max_run_seconds", 0) or 0)
    deadline = time.monotonic() + max_seconds if max_seconds > 0 else None

    for path, group in groups.items():
        if deadline is not None and time.monotonic() > deadline:
            for entry in group:
                cid = entry.get("test_case_id")
                still_failing.append(cid)
                outcomes.append(HEAL_OUTCOME.coerce({
                    "test_case_id": cid, "file": path, "attempted": False, "healed": False,
                    "declined": True,
                    "decline_reason": f"not attempted: the heal stage reached its "
                                      f"{int(max_seconds)}s time budget (budgets.max_run_seconds)"}))
            continue
        representative = group[0]
        case_ids = [c.get("test_case_id") for c in group]
        label = case_ids[0] if len(case_ids) == 1 else \
            f"{case_ids[0]} (+{len(case_ids) - 1} sharing this file)"

        fixed = declined = False
        reason = representative.get("reason", "")
        prior = None
        attempts = 0
        from_memory = False
        #: A patch actually reached the file. Distinct from "an attempt was
        #: made": `self_healer.run` RAISES when it judges a patch unsafe to
        #: apply, so a group whose every attempt was rejected wrote nothing at
        #: all. Reporting that as patch_applied made `writeback.from_heal`
        #: count it under "ignored_unverified", i.e. as a patch that merely
        #: went unproven, which is a different and much less alarming thing.
        patched = False
        #: Every file this group could touch, as it was BEFORE any attempt. A failed
        #: attempt is rolled back to these, so (a) a case that stays red is not left
        #: carrying an LLM-modified spec, and (b) the next attempt - and the
        #: assertion guard - start from the ORIGINAL, not from the previous
        #: attempt's patch, which let weakening accumulate across attempts.
        originals = {path: read_text_file(Path(ctx.repo_dir) / path,
                                          default=content_by_path.get(path, ""))}
        for related_path, related_text in _imported_page_objects(
                ctx.repo_dir, path, originals[path]).items():
            originals.setdefault(related_path, related_text)
        ledger_old = ledger_new = ""

        for attempt in range(1, max_attempts + 1):
            attempts = attempt
            print(f"      Healing {label} (attempt {attempt}/{max_attempts}): {reason}")
            broken = read_text_file(Path(ctx.repo_dir) / path,
                                    default=content_by_path.get(path, ""))
            try:
                patch = _self_healer.run(
                    broken_file_content=broken, failure_reason=reason,
                    feature_dir=ctx.feature_dir, repo_output_dir=Path(ctx.repo_dir),
                    file_path=path, suggested_fix=representative.get("suggested_fix", ""),
                    attempt=attempt, prior_attempt_info=prior,
                    navigation_context=nav_by_case.get(case_ids[0], ""),
                    failure_dom=dom_by_case.get(case_ids[0]),
                    related_files=_imported_page_objects(ctx.repo_dir, path, broken),
                    known_locators=known_locators,
                    action_timeline=timeline_by_case.get(case_ids[0]),
                    raw_error=_raw_error_for(ctx, case_ids[0]),
                )
            except Exception as exc:
                # self_healer RAISES when a patch is unsafe to apply. That must
                # cost this group one attempt, not abort the whole run.
                print(f"      -> {label}: attempt {attempt} rejected ({exc})")
                prior = {"patched_content": f"(rejected: {exc})", "attempt": attempt,
                         "rejected": True, "rejected_reason": str(exc)}
                _write_heal_record(ctx, case_ids[0], attempt, "rejected", str(exc))
                continue

            from_memory = bool(patch.get("from_memory"))
            prior = {**patch, "attempt": attempt}

            if patch.get("declined"):
                declined = True
                reason = patch.get("unresolved_reason", "(no reason given)")
                print(f"      -> {label}: self-healer declined — {reason}")
                break

            patched = True

            # RE-READ WHAT IS NOW ON DISK, for every file this heal could
            # have touched. The healer may patch a PAGE OBJECT rather than
            # the spec (`patched_file_path` — most locators live there), and
            # `files` / `content_by_path` still held the pre-patch text. Any
            # later stage reading that state — the Bug Reporter quoting the
            # code, a subsequent heal attempt on the same group — worked from
            # a version that no longer exists on disk.
            healed_path = patch.get("patched_file_path") or path
            fresh = read_text_file(Path(ctx.repo_dir) / healed_path)
            if fresh:
                content_by_path[healed_path] = fresh
                for generated in files:
                    if generated.get("path") == healed_path:
                        generated["content"] = fresh

            # THIS attempt's claim only. They used to accumulate with `or` across
            # attempts, so attempt 1 (a locator swap that failed and was
            # blocklisted) and attempt 2 (a blur fix with no locator update) ended
            # as one outcome naming attempt 1's swap as verified, and the ledger
            # then confirmed a locator that had just been disproved.
            update = patch.get("locator_memory_update") or {}

            # A patch to a SHARED file (a page object) can break specs that were
            # passing. Re-run every spec that imports it, and fail the heal on any
            # regression, not just on the one case being fixed.
            dependents = [] if healed_path == path else _dependent_specs(files, path, healed_path)
            rerun = adapters.execute(ctx.repo_dir, ctx.feature_dir,
                                     test_files=[path, *dependents], workers=ctx.workers,
                                     out_filename=f"heal_rerun_{case_ids[0]}.json",
                                     grep_ids=None if dependents else case_ids,
                                     reuse_auth=True,
                                     # ONE attempt: a heal that only passed on a
                                     # retry has not been shown to work.
                                     retries=0)
            if not rerun["ran"]:
                print(f"      -> Executor unavailable ({rerun['error']}); cannot verify.")
                _self_healer.mark_heal_result(ctx.feature_dir, update, success=False)
                # The patch is applied but unverified, and the case is reported
                # still failing: put the originals back, as every other failed
                # attempt does.
                _restore_originals(ctx, originals, files, content_by_path)
                break

            success = heal_succeeded(rerun, case_ids)
            regressions = (_regressions(rerun, case_ids, ctx.state.get("executor") or {})
                           if success and dependents else [])
            if regressions:
                success = False
                print(f"      -> {label}: the patch fixed it but BROKE {len(regressions)} other "
                      f"case(s) that were passing: {', '.join(regressions[:5])} - rolling back.")
            _self_healer.mark_heal_result(ctx.feature_dir, update, success=success)
            _write_heal_record(
                ctx, case_ids[0], attempt, "verified" if success else "failed",
                _heal_diff(ctx.repo_dir, originals), model_claim=update)
            if success:
                print(f"      -> {label} fixed and confirmed passing.")
                fixed = True
                ledger_old = update.get("old_locator", "")
                ledger_new = update.get("new_locator", "")
                break
            _restore_originals(ctx, originals, files, content_by_path)

        for case_id in case_ids:
            outcomes.append(HEAL_OUTCOME.coerce({
                "test_case_id": case_id, "file": path,
                "attempted": True, "healed": fixed, "attempts": attempts,
                "from_memory": from_memory,
                "old_locator": ledger_old if fixed else "",
                "new_locator": ledger_new if fixed else "",
                "patch_applied": patched and fixed,
                "verified": fixed,
                "declined": declined,
                "decline_reason": reason if declined else "",
            }))
            (healed if fixed else still_failing).append(case_id)

    ctx.metrics.data["cases_healed"] = len(healed)
    ctx.metrics.data["cases_still_failing"] = len(still_failing)
    write_validated(ctx.artifact("heal_outcomes.json"), "heal_outcome",
                    outcomes, many=True)
    # The Bug Reporter reads this file; nothing wrote it, so a decline reason
    # lived only in heal_outcomes.json and never reached BUG_LIST.md.
    write_json(ctx.artifact("self_heal_declined.json"),
               {o.get("test_case_id"): o.get("decline_reason")
                for o in outcomes if o.get("declined") and o.get("decline_reason")})

    if still_failing:
        print(f"      {len(still_failing)} case(s) still need attention: {still_failing}")
    return {"outcomes": outcomes, "healed": healed, "still_failing": still_failing}


def unresolved_failures(ctx) -> list:
    """Case ids that are STILL failing once the whole run (including healing) is over.

    Derived from the executor's own results minus what the healer fixed, not from
    the healer's `still_failing`: that list only covers cases the healer attempted,
    so a genuine bug, a known flake or an environment failure - all red - would
    never have counted. A failing case with no id in its title is counted by its
    row, not dropped.
    """
    executor_state = ctx.state.get("executor") or {}
    if not executor_state.get("ran"):
        return []
    healed = set((ctx.state.get("self_healer") or {}).get("healed") or [])
    failing = []
    for case in executor_state.get("cases") or []:
        if case.get("status") in ("passed", "skipped", "expected"):
            continue
        key = case.get("test_case_id") or f"{case.get('file', '?')}"
        if key not in healed:
            failing.append(key)
    return failing


def stage_bug_report(ctx) -> dict:
    review_path = ctx.state.get("reviewer", {}).get("review_path")
    if not review_path:
        return {"skipped": "no review to report on"}
    still_failing = ctx.state.get("self_healer", {}).get("still_failing") or []
    # Cases that actually executed this run (passed or failed), so a bug is only
    # called "no longer failing" when its case really ran.
    ran_cases = (ctx.state.get("executor") or {}).get("cases") or []
    executed = ({c["test_case_id"] for c in ran_cases
                 if c.get("test_case_id") and c.get("status") != "skipped"}
                if ran_cases else None)
    path = adapters.report_bugs(
        executed_case_ids=executed,
        review_path=Path(review_path),
        run_result=read_json(ctx.artifact("run_results.json"), default={}) or {},
        repo_dir=ctx.repo_dir, feature_dir=ctx.feature_dir,
        still_failing_ids=still_failing,
        case_index=ctx.state.get("automation_planner", {}).get("case_index"))
    return {"path": str(path)}


def stage_feedback(ctx) -> dict:
    run_result = ctx.state.get("executor") or {}
    if not run_result.get("ran"):
        return {"skipped": "nothing ran, so nothing is confirmed"}

    files = ctx.state.get("coding_agent", {}).get("files") or []
    plans = ctx.state.get("automation_planner", {}).get("plans") or []
    outcomes = ctx.state.get("self_healer", {}).get("outcomes") or []

    tallies = {
        "from run": writeback.from_run(ctx.ledger, run_result, files, plans),
        "from heals": writeback.from_heal(ctx.ledger, outcomes),
    }
    ctx.ledger.save()
    print(f"      {writeback.summarize(tallies)}")
    print(f"      Ledger now holds {ctx.ledger.stats()['total']} card(s), "
          f"{ctx.ledger.stats()['groundable_elements']} usable element(s).")
    return tallies


# ===========================================================================
# Resume — rebuilding a skipped stage's state from its artifacts
#
# Every stage below writes a contract-validated artifact, so a resumed run can
# reload what it produced instead of leaving the next stage to read an empty
# dict and conclude there is nothing to do. The reload is deliberately
# ARTIFACT-BASED rather than a cached blob of the return value: the artifacts
# are the durable, validated record, and anything that cannot be reconstructed
# from them was never really persisted.
# ===========================================================================


# ===========================================================================
# The graph
# ===========================================================================

def build() -> list:
    return [
        Stage("doc_ingestor", "Brain — ingesting knowledge", stage_ingest,
              description="Reading your design docs, Figma exports and recordings, and\n"
                          "distilling them into typed facts the other agents look up.",
              summarize=lambda r: (f"{r['tally']['added']} new card(s), "
                                   f"{r['tally']['updated']} updated"
                                   if r.get("tally") else "")),

        Stage("app_explorer", "App Explorer — crawling the live app", stage_explore,
              description="Visiting the declared screens and recording their real elements.\n"
                          "This is where ground-truth locators come from.",
              # Unreachable screens belong in the ONE line an operator reads.
              # Reporting only what was visited made a crawl that reached one
              # screen out of four look like a success.
              summarize=lambda r: ("skipped" if r.get("skipped") else
                                   f"{len(r.get('visited', []))} screen(s), "
                                   f"{r.get('cards', 0)} card(s)"
                                   + (f", {len(r.get('failed') or [])} UNREACHABLE"
                                      if r.get("failed") else ""))),

        Stage("testcase_writer", "Test Case Writer", stage_test_cases,
              description="Turning the story into manual test cases, grounded in the app's\n"
                          "real vocabulary rather than guessed terminology.",
              rehydrate=_rehydrate_test_cases,
              summarize=lambda r: (f"{len(r.get('cases', []))} case(s) -> {Path(r['sheet']).name}"
                                   + (f", INCOMPLETE: no cases for {', '.join(r['failed_stories'])}"
                                      if r.get("failed_stories") else ""))),

        Stage("review_gate", "Human review gate", stage_review_gate,
              description="Waiting for a person to approve the sheet. Approving, not authoring.",
              summarize=lambda r: "approved",
              # Re-read on every run: `--force testcase_writer` rebuilds the
              # sheet with blank Status, and a gate remembered as "done" let
              # later stages proceed on cases nobody had reviewed.
              always_run=True),

        Stage("jira_uploader", "Jira Uploader", stage_jira_upload,
              applies=lambda ctx: bool(ctx.profile.get("connectors.jira.project")),
              skip_reason=lambda ctx: ("connectors.jira.project is blank in the profile's project.json, "
                                       "so nothing was uploaded. Set the Jira project key to enable it."),
              summarize=lambda r: r.get("path", "")),

        Stage("framework_scaffolder", "Framework scaffold", stage_scaffold,
              description="Writing the fixed POM skeleton. Deterministic — no AI here.",
              summarize=lambda r: (f"{len(r['created'])} file(s) created"
                                   if r.get("created") else "already scaffolded")),

        Stage("automation_planner", "Automation Planner", stage_plan,
              rehydrate=_rehydrate_plan,
              description="Placing each approved case into the scaffold, and resolving every\n"
                          "element it needs against the ledger before any code is written.",
              summarize=lambda r: (f"{len(r.get('plans', []))} plan(s), "
                                   f"{len(r.get('unresolved', []))} element(s) unresolved")),

        Stage("coding_agent", "Coding Agent", stage_code,
              rehydrate=_rehydrate_code,
              description="Writing the Playwright code, then compiling it and checking every\n"
                          "locator against the ledger before accepting it.",
              summarize=lambda r: (f"{len(r.get('files', []))} file(s) written"
                                   if not r.get("skipped") else r["skipped"])),

        Stage("executor", "Executor", stage_execute,
              rehydrate=_rehydrate_run,
              applies=lambda ctx: not ctx.skip_execution,
              description="Running the tests generated this run. No AI — just Playwright.",
              summarize=lambda r: (f"{r.get('stats', {}).get('expected', 0)} passed, "
                                   f"{r.get('stats', {}).get('unexpected', 0)} failed"
                                   + (f", {r['stats']['flaky']} flaky (passed only on retry)"
                                      if (r.get('stats') or {}).get('flaky') else "")
                                   if r.get("ran") else r.get("skipped", "did not run")),
              halts=lambda ctx, r: r.get("ran") is False and not r.get("skipped")),

        Stage("reviewer", "Automation Reviewer", stage_review,
              rehydrate=_rehydrate_review,
              applies=lambda ctx: not ctx.skip_execution,
              description="Deciding, with evidence: real bug, known flake, or fixable test issue.",
              summarize=lambda r: f"{len(r.get('classifications', []))} failure(s) classified"),

        Stage("self_healer", "Self-Healer", stage_heal,
              rehydrate=_rehydrate_heal,
              applies=lambda ctx: not ctx.skip_execution,
              description="Patching only what the Reviewer called mechanically fixable,\n"
                          "then re-running it to prove the patch worked.",
              summarize=lambda r: (f"{len(r.get('healed', []))} healed, "
                                   f"{len(r.get('still_failing', []))} still failing")),

        Stage("bug_reporter", "Bug Reporter", stage_bug_report,
              applies=lambda ctx: not ctx.skip_execution and not ctx.skip_bug_report,
              description="Writing up what still fails, with screenshots. No AI call.",
              summarize=lambda r: r.get("path", r.get("skipped", ""))),

        Stage("brain_feedback", "Brain feedback", stage_feedback,
              applies=lambda ctx: not ctx.skip_execution,
              description="Promoting everything that actually passed to confirmed knowledge,\n"
                          "so the next run is cheaper than this one.",
              summarize=lambda r: writeback.summarize(r) if not r.get("skipped") else r["skipped"]),
    ]
