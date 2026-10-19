"""qa-pipeline — the command line.

Every command takes `--profile`, because the engine holds no project
knowledge: what to test, where it lives and how much to spend are all in the
profile. That constraint is what makes this shareable, and the CLI is where it
is most visible — there is no `--repo-dir`, no `--base-url`, no `--figma`
default anywhere in this file.

    qa-pipeline init-profile myapp
    qa-pipeline ingest      --profile myapp
    qa-pipeline explore     --profile myapp --screen Login --screen Dashboard
    qa-pipeline run         --profile myapp --story PROJ-1234 --epic PROJ-1000
    qa-pipeline brain       --profile myapp
    qa-pipeline contracts   --show generated_file
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

# The engine imports the existing `agents`/`utils` packages, which sit beside
# this one at the repository root. Adding that root to sys.path keeps
# `python -m qa_pipeline.cli` working from anywhere without an editable
# install — which matters most in CI, where the checkout path is not fixed.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline import profiles  # noqa: E402
from qa_pipeline.contracts import REGISTRY  # noqa: E402
from utils.dotenv import load_env_file  # noqa: E402
from utils.runlog import tee_to  # noqa: E402

#: Holds the run-log tee for the lifetime of one command; closed in main().
_run_log = contextlib.ExitStack()


def _load_dotenv():
    """Loads `.env` into the process environment before anything reads it.

    This used to be the entire reason `run_with_env.py` existed as a separate
    wrapper: nothing in the pipeline reads `.env` on its own.
    `utils/env_config.resolve_app_env()` normalizes the LOCAL_/CENTRAL_
    variables but assumes they are already IN the environment, and
    `utils/llm_client` reads AI_PROVIDER at import time — so invoking the
    pipeline directly, with a `.env` sitting right beside it, ran with no
    provider key and no APP_BASE_URL.

    Folding it in here is what lets the three `run_*_with_env.py` wrappers
    go away: there is one entry point, and it does the right thing by itself.

    `override=False` so a variable already exported in the shell wins over
    the file — which is what someone debugging against a one-off environment
    expects, and matches how `Profile.apply_environment` behaves.
    """
    for candidate in (Path.cwd() / ".env", _ROOT / ".env"):
        if candidate.is_file():
            loaded = load_env_file(candidate, override=False)
            if loaded:
                print(f"Loaded {len(loaded)} setting(s) from {candidate}")
            return


def _load(args) -> profiles.Profile:
    profile = profiles.load(args.profile)
    profile.ensure_dirs()
    profile.apply_environment()
    return profile


# ---------------------------------------------------------------------------

def cmd_init_profile(args) -> int:
    from qa_pipeline.doctor import VALID_NAME
    if not VALID_NAME.match(args.name):
        print(f"{args.name!r} is not a good profile name. Use lowercase letters, digits, "
              f"'-' and '_' (start with a letter or digit) - names are used in folder "
              f"paths and shell commands, where spaces and symbols break things. "
              f"Example: {_suggest_name(args.name)}")
        return 1
    profile = profiles.init(args.name)
    print(f"Created {profile.root}\n")
    print(profile.describe())
    print("\nNext:")
    print(f"  1. Set app.base_url in {profile.root / 'project.json'}")
    print(f"  2. Drop design docs into {profile.knowledge_dir}")
    print(f"     (run `qa-pipeline doctor --profile {args.name}` any time to check your setup)")
    print(f"  3. qa-pipeline ingest --profile {args.name}")
    return 0


def _suggest_name(raw: str) -> str:
    import re
    cleaned = re.sub(r"[^a-z0-9_-]+", "_", str(raw).lower()).strip("_-")
    return cleaned or "myapp"


def cmd_doctor(args) -> int:
    from qa_pipeline import doctor

    profile = None
    if args.profile:
        profile = profiles.load(args.profile)
        profile.apply_environment()
    results = doctor.run_checks(profile, online=args.online)
    print("qa-pipeline doctor" + (f" - profile {args.profile!r}" if args.profile else "") + "\n")
    print(doctor.render(results))
    if not args.profile:
        print("\n  Tip: add --profile <name> to also check that profile's inputs and workspace.")
    return doctor.exit_code(results)


def cmd_ingest(args) -> int:
    from qa_pipeline.brain import ingest, open_ledger

    profile = _load(args)
    ledger = open_ledger(profile.brain_dir)
    feature_dir = profile.context_root / "_ingest"
    feature_dir.mkdir(parents=True, exist_ok=True)

    report = ingest.from_documents(ledger, profile.knowledge_dir, feature_dir)
    ledger.save()

    print(f"\nLedger: {ledger.path}")
    print(json.dumps(ledger.stats(), indent=2))
    if report.get("rejected"):
        print(f"\n{len(report['rejected'])} rejected proposal(s):")
        for entry in report["rejected"][:10]:
            print(f"  - {entry}")
    return 0


def _normalize_screen(name) -> str:
    """Screen names are typed by hand on the command line; compare them the
    way a person means them, not byte-for-byte."""
    return " ".join(str(name or "").split()).casefold()


def _explore_targets(profile, screen_args=None) -> list:
    """`{"screen", "context"}` targets for the App Explorer.

    `context` is the free-text flow description `dom_inspector.inspect`
    navigates by — and, crucially, what `route_map.match` searches for a
    declared route in. It defaults to the screen name only when the profile
    gives no explicit one, never instead of it.

    Shared by `qa-pipeline explore` and the pipeline's own explore stage so
    the two cannot drift.
    """
    targets = []
    for screen in profile.get("app.screens") or []:
        if isinstance(screen, dict):
            name = screen.get("name", "")
            targets.append({"screen": name,
                            "context": screen.get("context") or name})
        else:
            targets.append({"screen": str(screen), "context": str(screen)})

    if not screen_args:
        return targets

    # FILTER, DON'T REBUILD. This used to return
    # `{"screen": s, "context": s}` for each requested name, discarding the
    # profile's declared context — and the context is exactly what
    # `route_map.match` searches for a declared route. Naming a screen
    # therefore downgraded it from a precise declared route to blind keyword
    # navigation, which is how screens came back UNREACHABLE. Match the
    # profile's own entry when there is one, and only invent a target for a
    # name the profile does not declare.
    declared = {_normalize_screen(t["screen"]): t for t in targets}
    chosen = []
    for wanted in screen_args:
        match = declared.get(_normalize_screen(wanted))
        chosen.append(match or {"screen": wanted, "context": wanted})
    return chosen


def cmd_explore(args) -> int:
    from qa_pipeline.brain import explore, open_ledger

    profile = _load(args)
    if not profile.base_url:
        print("app.base_url is not set in this profile — nothing to crawl.")
        return 1

    ledger = open_ledger(profile.brain_dir)

    # Targets are built EXACTLY as `orchestrator.stages.stage_explore` builds
    # them, including the profile's per-screen `context`. This command used to
    # pass the screen NAME as the navigation context, which silently discarded
    # that field — and `context` is the half that matters: it is what
    # `utils.route_map.match` looks for a declared route in, so a profile with
    # a perfectly good route map still fell through to keyword guessing and
    # every screen came back as a snapshot of the dashboard. Two entry points
    # disagreeing about one config key is exactly the drift a shared builder
    # prevents.
    targets = _explore_targets(profile, args.screen)
    if not targets:
        print("No screens given. Pass --screen NAME (repeatable), or declare "
              "app.screens in the profile.")
        return 1

    report = explore.crawl(ledger, targets)
    ledger.save()
    explore.close()

    print(f"\nVisited {len(report['visited'])}, failed {len(report['failed'])}, "
          f"{report['cards']} card(s) folded in.")
    for failure in report["failed"]:
        print(f"  ! {failure['screen']}: {failure['reason']}")
    print(json.dumps(ledger.stats(), indent=2))
    return 0


def cmd_check_routes(args) -> int:
    """Dry-runs every declared route against the live app and reports the first
    step that fails, so a stale routes.json is found in one command instead of
    inside a 30-minute run."""
    from utils import dom_inspector, route_map

    _load(args)  # loads .env and resolves LOCAL_/CENTRAL_ vars for this profile
    routes = route_map.load(force=True)
    if not routes:
        print(f"No routes declared ({route_map.route_map_path()}).")
        return 1
    bad = 0
    for name, route in routes.items():
        page = dom_inspector._get_page()
        if page is None:
            print("Cannot open the app — check APP_BASE_URL / credentials.")
            return 1
        taken = dom_inspector._navigate_declared_route(page, route)
        expected = len(route.get("clicks") or []) + (1 if route.get("url") else 0)
        landed = bool(taken) and dom_inspector._route_landed(page, route)
        ok = landed and len(taken) >= expected
        bad += 0 if ok else 1
        print(f"  {'OK  ' if ok else 'FAIL'} {name}: {len(taken)}/{expected} step(s) done"
              + ("" if landed else " - did not land"))
    dom_inspector.close()
    return 1 if bad else 0


def cmd_brain(args) -> int:
    from qa_pipeline.brain import open_ledger

    profile = _load(args)
    ledger = open_ledger(profile.brain_dir)

    if args.demote:
        # The manual correction path. Without it, a card that a person proved
        # wrong by hand — including one sourced from a human RECORDING, which
        # sits at 0.95 — stayed authoritative forever, because the only
        # automatic downgrade fires when the Self-Healer replaces a locator at
        # run time.
        card = ledger.get(args.demote)
        if card is None:
            print(f"No card with id {args.demote!r}. Run `qa-pipeline brain "
                  f"--profile {args.profile}` to list what the ledger holds.")
            return 1
        demoted = ledger.demote(args.demote, args.reason or "disproved by hand")
        ledger.save()
        print(f"Demoted {args.demote}\n"
              f"  source     {demoted.get('source')} (unchanged — it really did say this)\n"
              f"  confidence {card.get('confidence', 0):.2f} -> {demoted.get('confidence', 0):.2f}\n"
              f"  reason     {demoted.get('disproved_reason')}\n"
              f"It will no longer be used to ground generated code.")
        return 0

    if args.query:
        from qa_pipeline.brain import retrieve
        print(retrieve.for_agent(ledger, args.agent or "coding_agent", args.query))
        return 0

    print(f"{ledger.path}\n")
    print(json.dumps(ledger.stats(), indent=2))
    screens = ledger.screens()
    if screens:
        print(f"\nScreens known ({len(screens)}):")
        for name in screens[:40]:
            print(f"  - {name}")
    return 0


def cmd_scaffold(args) -> int:
    from qa_pipeline.agents import framework_scaffolder

    profile = _load(args)
    framework_scaffolder.run(profile.workspace)
    print(f"\n{framework_scaffolder.describe(profile.workspace)}")
    return 0


def cmd_contracts(args) -> int:
    if args.show:
        schema = REGISTRY.get(args.show)
        if schema is None:
            print(f"Unknown contract {args.show!r}. Known: {', '.join(sorted(REGISTRY))}")
            return 1
        print(schema.describe())
        return 0
    for name in sorted(REGISTRY):
        print(f"  {name:20s} {REGISTRY[name].desc}")
    return 0


def cmd_import_folder(args) -> int:
    """Batch-import a folder of existing sheets, one file per story.

    Stops after import on purpose. The later stages need per-story
    configuration this mode has no way to collect per file, and guessing it
    for forty stories at once produces forty subtly wrong suites.
    """
    from qa_pipeline.agents import adapters

    profile = _load(args)
    results = adapters.import_test_cases_folder(
        args.folder, args.epic, profile.context_root)
    ok = [r for r in results if r.get("status") == "ok"]

    print(f"\n{len(ok)}/{len(results)} stories imported.\n")
    for entry in ok:
        print(f"  {entry['epic_key']}_{entry['story_key']}: {entry['output_path']}")
    print("\nNext, for each story:")
    print("  1. Open the sheet and set every row's Status to 'Reviewed'.")
    print(f"  2. qa-pipeline run --profile {args.profile} "
          f"--story <STORY> --epic <EPIC>")
    return 0 if ok else 1


#: Per-agent .env defaults for arguments that are otherwise typed on every
#: invocation. `(attribute, env var)` — the AGENT NAME is the prefix, so a
#: reader can tell from the variable alone which stage consumes it.
#:
#: These are DEFAULTS, never overrides: an explicit flag always wins, which
#: is the only ordering that keeps a one-off run (`--import-testcases
#: other.xlsx`) from being silently ignored because .env named a different
#: file. Applied here rather than as `add_argument(default=...)` because
#: `main` loads the .env AFTER the parser is built, so an argparse default
#: would read the environment before it exists and always come back empty.
#: Kept in step with `agents.testcase_writer._DOCUMENT_STORY_KEY` — the CLI
#: names the feature directory from it before the writer is ever imported, so
#: the two must agree or the run would write its sheet into a directory the
#: writer does not key its cases to.
_DOCUMENT_STORY_KEY = "DOC-1"

_ENV_DEFAULTS = (
    ("import_testcases", "TESTCASE_WRITER_IMPORT_FILE"),
    ("design_doc", "TESTCASE_WRITER_DESIGN_DOC"),
    ("api_spec", "TESTCASE_WRITER_API_SPEC"),
    ("figma", "TESTCASE_WRITER_FIGMA_URL"),
    ("reference_testcases", "TESTCASE_WRITER_REFERENCE_CASES"),
    ("feature_link", "JIRA_FEATURE_LINK"),
)


def _apply_env_defaults(args) -> None:
    """Fills unset `run` arguments from their per-agent .env variable.

    Announces every value it takes from the environment. A path the operator
    did not type on the command line, silently steering which documents a run
    is grounded in, is exactly the kind of invisible input that makes two
    runs differ for no visible reason — so it is printed, with the variable
    name, before anything uses it.

    A path that does not exist is reported and DROPPED rather than passed on:
    the downstream loaders treat an unreadable design doc as "no extra
    context" (see utils/app_design_doc.load), so a typo'd .env path would
    otherwise degrade grounding with no message anywhere.
    """
    for attribute, variable in _ENV_DEFAULTS:
        if getattr(args, attribute, ""):
            continue                      # an explicit flag always wins
        # The two import flags are mutually exclusive on the command line. An
        # env default is applied AFTER argparse, so it bypassed that: passing
        # --import-from-jira with TESTCASE_WRITER_IMPORT_FILE in .env set both,
        # and the stage silently preferred the file.
        sibling = {"import_testcases": "import_from_jira",
                   "import_from_jira": "import_testcases"}.get(attribute)
        if sibling and getattr(args, sibling, ""):
            continue
        value = (os.environ.get(variable) or "").strip()
        if not value:
            continue
        # JIRA_FEATURE_LINK is an opaque identifier (a link OR a bare issue
        # key like "PROJ-68308"), never a local path — the generic URL/path
        # heuristic below would otherwise treat a bare key as a path and
        # report it as "does not exist".
        if variable.endswith(("_URL", "_LINK")) or value.startswith(("http://", "https://")):
            # Printed WITHOUT its query string: a link can carry a token there.
            print(f"      {variable} -> --{attribute.replace('_', '-')} = "
                  f"{value.split('?')[0]}")
            setattr(args, attribute, value)
            continue
        path = Path(value).expanduser()
        if not path.exists():
            print(f"      ⚠️  {variable} points at {path}, which does not exist — "
                  f"ignoring it for this run.")
            continue
        print(f"      {variable} -> --{attribute.replace('_', '-')} = {path}")
        setattr(args, attribute, str(path))


def _resolve_workers(args, profile):
    """CLI flag > profile `execution.workers` > None (Playwright's own default).

    The profile value was never read, so `execution.workers: 1` in project.json was
    ignored and every pipeline run used Playwright's default of half the CPU cores
    (7 here). On an application that shares one login across tests that was enough
    load to make the app time out, and the failures were blamed on the tests.
    """
    if args.workers is not None:
        return args.workers
    configured = profile.get("execution.workers")
    try:
        return int(configured) if configured else None
    except (TypeError, ValueError):
        return None


def cmd_execute(args) -> int:
    """Run the generated suite as it stands (parallel, Allure report, no model, no API key)."""
    from qa_pipeline.execute_suite import cmd_execute as execute_suite

    _apply_env_defaults(args)
    return execute_suite(args, _load(args))


def cmd_decline(args) -> int:
    """Decline one case in the run JSON, the plan, the spec and every workbook at once."""
    from qa_pipeline.decline import cmd_decline as decline

    return decline(args, _load(args))


def cmd_link_jira(args) -> int:
    """Write the Jira keys of an upload back into the sheets and the workspace."""
    from qa_pipeline.jira_link import cmd_link_jira as link_jira

    return link_jira(args, _load(args))


def cmd_coverage(args) -> int:
    """Which controls the scripts exercise, from the code."""
    from qa_pipeline.coverage_report import cmd_coverage as coverage

    return coverage(args, _load(args))


def cmd_status(args) -> int:
    """Where each case stands: locators -> page object -> script."""
    from qa_pipeline.case_status import cmd_status as status

    return status(args, _load(args))


def cmd_bundle(args) -> int:
    """Copy each story's specs next to its test cases and write a README there."""
    from qa_pipeline.story_bundle import cmd_bundle as bundle

    return bundle(args, _load(args))


def cmd_run(args) -> int:
    from qa_pipeline.agents import adapters
    from qa_pipeline.orchestrator import (
        DemonstrationRequired,
        ReviewRequired,
        RunContext,
        build_pipeline,
    )
    from qa_pipeline.orchestrator.budget import BudgetExceeded
    from qa_pipeline.orchestrator.budget import report as budget_report
    from qa_pipeline.orchestrator.runner import ModelUnavailable, StageFailed

    _apply_env_defaults(args)

    profile = _load(args)
    print(profile.describe())
    print()

    # --story may be a path to a local JIRA XML export for orgs that cannot
    # reach JIRA from where this runs. Resolve the real keys now so the
    # feature directory and sheet are named "PROJ-68308", not the file path.
    story_key, epic_key = args.story, args.epic
    peeked = adapters.peek_story_from_xml(args.story) if args.story else None
    if peeked:
        story_key = peeked["story_key"]
        epic_key = args.epic or peeked["epic_key"] or "EPIC"
        print(f"Resolved {Path(args.story).name} -> story {story_key}, epic {epic_key}\n")
    elif not args.story:
        # NO STORY IS A SUPPORTED MODE, not a missing argument.
        #
        # A tracker item is the PREFERRED source of what to test, never the
        # only one: a design document, a Figma file, an API spec, an existing
        # regression suite or the profile's ingested knowledge are each enough
        # on their own (see testcase_writer.run, which synthesizes a story
        # from whichever of those are present). The story key here only names
        # the feature directory and prefixes Test Case IDs, so it is given the
        # same DOC-1 value the writer uses — a sheet derived from documents is
        # then visibly not traceable to a tracker item, on sight, from its IDs.
        #
        # The writer itself refuses genuinely empty input, and it is the only
        # layer that can: it is what knows whether the design doc loaded,
        # whether Figma returned sections, and whether the ledger has anything
        # relevant. Duplicating that judgement here would mean two places
        # deciding what counts as "enough", which is how they drift apart.
        story_key = _DOCUMENT_STORY_KEY
        epic_key = epic_key or "DOC"
        print(f"No --story given — deriving test cases from the other inputs "
              f"(design doc / Figma / API spec / --reference-testcases / ingested "
              f"knowledge). Sheet will be keyed {epic_key}_{story_key}.\n")
    elif not epic_key:
        print("--epic is required unless --story points at a local JIRA XML "
              "export carrying an Epic Link field.")
        return 1

    ctx = RunContext(
        profile, story_key=story_key, epic_key=epic_key,
        figma_url=args.figma, api_spec=args.api_spec, cycle_id=args.cycle,
        include_manual=args.include_manual, skip_execution=args.skip_execution,
        workers=_resolve_workers(args, profile), shard=args.shard,
        interactive=not args.non_interactive,
        force_stages=args.force or (), design_doc=args.design_doc,
        import_testcases=args.import_testcases,
        reference_testcases=args.reference_testcases,
        derive_from_documents=not args.story,
        import_from_jira=args.import_from_jira,
        skip_bug_report=args.skip_bug_report,
        max_run_tokens=args.max_run_tokens,
        explore_screens=args.screens or (),
        feature_link=args.feature_link,
    )

    # The XML path the user gave, kept beside the RESOLVED key. The writer reads
    # a local export only when it is handed the path; handed the resolved key
    # ("PROJ-68308") it fetched the live tracker instead — the exact case the
    # export exists for (no tracker access).
    ctx.story_source = args.story if peeked else ""

    # Everything printed from here on is also kept in <run folder>/_internal/run.log
    # (closed by main(), so every return path below is covered).
    _run_log.enter_context(tee_to(ctx.artifact("run.log")))
    print(f"      Run log: {ctx.artifact('run.log')}")
    pipeline = build_pipeline()
    try:
        pipeline.run(ctx, only=set(args.only) if args.only else None, until=args.until)
    except ReviewRequired as exc:
        print(exc)
        return 2
    except DemonstrationRequired as exc:
        print(exc)
        return 3
    except BudgetExceeded as exc:
        print(f"\n[HALT] {exc}")
        return 4
    except ModelUnavailable as exc:
        print(f"\n[HALT] {exc}")
        return 5
    except StageFailed as exc:
        print(f"\n[FAILED] {exc}")
        return 1
    finally:
        try:
            from qa_pipeline.brain import explore
            explore.close()
        except Exception:
            pass
        path = ctx.save_metrics()
        print("\n--- This run ---")
        print(f"  {budget_report(ctx)}")
        print(json.dumps(ctx.metrics.rates(), indent=2))
        print(f"  metrics -> {path}")

    from qa_pipeline.story_bundle import refresh_bundles
    refresh_bundles(profile)
    return _result_exit_code(ctx, args, profile)


# ---------------------------------------------------------------------------

#: A run that finished normally, but tests are still red after self-healing.
#: Distinct from 1 (the PIPELINE failed): here the pipeline worked and the
#: application - or a test it could not heal - did not.
EXIT_TESTS_FAILING = 6


def _result_exit_code(ctx, args, profile) -> int:
    """0 only when the suite is actually green.

    Without this a run with 20 failing tests exited 0, so a CI job went green on
    a red suite. Skipped for partial runs (`--until`, `--only`), which stop on
    purpose before there is anything to judge, and when the operator opts out
    with `--allow-failures` or `gates.fail_on_test_failures: false`.
    """
    from qa_pipeline.orchestrator.stages import unresolved_failures

    if args.allow_failures or args.until or args.only:
        return 0
    if not profile.get("gates.fail_on_test_failures", True):
        return 0
    failing = unresolved_failures(ctx)
    if failing:
        shown = ", ".join(str(f) for f in failing[:8])
        more = f" (+{len(failing) - 8} more)" if len(failing) > 8 else ""
        print(f"\n[TESTS FAILING] {len(failing)} case(s) still failing after self-healing: "
              f"{shown}{more}\n  Exit {EXIT_TESTS_FAILING}. See BUG_LIST.md; "
              f"pass --allow-failures to exit 0 anyway.")
        return EXIT_TESTS_FAILING
    return 0


_EXIT_CODES_HELP = """\
exit codes (CI can branch on these without parsing output):
  0 ok   1 failed   2 review required   3 demonstration required
  4 budget exceeded   5 model API unusable   6 tests still failing after healing
first time?  qa-pipeline doctor            check your setup
             qa-pipeline init-profile myapp
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qa-pipeline",
        description="Zero-touch QA pipeline: nobody writes manual test cases, "
                    "nobody writes automation scripts.",
        epilog=_EXIT_CODES_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--debug", action="store_true",
                        help="show the full traceback on an unexpected error")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-profile", help="create a new profile skeleton")
    p.add_argument("name")
    p.set_defaults(func=cmd_init_profile)

    p = sub.add_parser("ingest", help="read the profile's documents into the ledger")
    p.add_argument("--profile", required=True)
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("explore", help="crawl the live app into the ledger")
    p.add_argument("--profile", required=True)
    p.add_argument("--screen", action="append",
                   help="screen to visit; repeatable. Defaults to app.screens.")
    p.set_defaults(func=cmd_explore)

    p = sub.add_parser("check-routes",
                       help="dry-run every declared route against the live app")
    p.add_argument("--profile", required=True)
    p.set_defaults(func=cmd_check_routes)

    p = sub.add_parser("brain", help="inspect or query the ledger")
    p.add_argument("--profile", required=True)
    p.add_argument("--query", help="show what an agent would retrieve for this task")
    p.add_argument("--agent", help="which agent's weighting to use for --query")
    p.add_argument("--demote", metavar="CARD_ID",
                   help="mark a card disproved so it stops grounding generated code "
                        "— use when you have proved a locator wrong by hand")
    p.add_argument("--reason", help="why the --demote'd card is wrong")
    p.set_defaults(func=cmd_brain)

    p = sub.add_parser("scaffold", help="write the framework skeleton")
    p.add_argument("--profile", required=True)
    p.set_defaults(func=cmd_scaffold)

    p = sub.add_parser("doctor", help="check this machine is ready (tools, keys, profile, workspace)")
    p.add_argument("--profile", help="also check this profile's inputs, workspace and base URL")
    p.add_argument("--online", action="store_true",
                   help="also check that the application URL is reachable (needs --profile)")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("contracts", help="show the artifact contracts")
    p.add_argument("--show", help="print one contract's full shape")
    p.set_defaults(func=cmd_contracts)

    p = sub.add_parser("import-folder",
                       help="batch-import a folder of existing sheets, one per story")
    p.add_argument("--profile", required=True)
    p.add_argument("--folder", required=True,
                   help="folder of .xlsx/.xlsm/.csv files named "
                        "'<EPIC>_<STORY>.xlsx' or '<STORY>.xlsx'")
    p.add_argument("--epic", default="",
                   help="default epic for files that do not embed one")
    p.set_defaults(func=cmd_import_folder)

    p = sub.add_parser("execute", help="run the existing suite in parallel with an Allure "
                                       "report (no model call, no API key)")
    p.add_argument("--profile", required=True)
    p.add_argument("--workers", type=int, default=None,
                   help="parallel workers (default: the profile's execution.workers, else 4)")
    p.add_argument("--shard", default=None, help="run one slice, e.g. 1/3 (for CI matrix jobs)")
    p.add_argument("--grep", action="append",
                   help="run only the tests whose title contains this case id; repeatable")
    p.add_argument("--retries", type=int, default=0,
                   help="retries per failing test (default 0: one honest attempt)")
    p.add_argument("--no-allure", dest="allure", action="store_false",
                   help="skip building the Allure HTML report")
    p.add_argument("--allow-failures", action="store_true",
                   help="exit 0 even when tests fail (default: exit 6)")
    p.add_argument("--rerun-failed", choices=("none", "infra", "all"), default="all",
                   help="give each failed test one more attempt, alone on 1 worker, to tell flaky "
                        "from real failures: 'all' (default), 'infra' (page load / network errors "
                        "only) or 'none'. A test that fails again stays failed.")
    p.add_argument("--no-progress", dest="progress", action="store_false",
                   help="do not print one line per test while the suite runs")
    p.set_defaults(func=cmd_execute, allure=True, progress=True)

    p = sub.add_parser("decline", help="take one test case out of play everywhere it lives: the run "
                                       "JSON, the plan, its spec (test.skip) and every workbook")
    p.add_argument("--profile", required=True)
    p.add_argument("--case", required=True, help="the case id, e.g. PROJ-68294-AUTO-TC-065")
    p.add_argument("--reason", required=True, help="why; it is written into every sheet")
    p.add_argument("--dry-run", action="store_true", help="show what would change, change nothing")
    p.set_defaults(func=cmd_decline)

    p = sub.add_parser("link-jira", help="write the Jira keys from an upload back into the sheets and "
                                         "the workspace (jira-map.json), so each script maps to its Jira test")
    p.add_argument("--profile", required=True)
    p.add_argument("--results", help="jira_upload_result.json (default: the newest under the profile's runs)")
    p.add_argument("--sheet", action="append", help="a workbook to fill (repeatable; default: every "
                                                     "workbook in the profile's runs)")
    p.set_defaults(func=cmd_link_jira)

    p = sub.add_parser("coverage", help="which page-object controls do the scripts actually use "
                                        "(untested / thin), read from the code, not from test names")
    p.add_argument("--profile", required=True)
    p.set_defaults(func=cmd_coverage)

    p = sub.add_parser("status", help="per-case progress: locators -> page object -> script "
                                      "(reads/refreshes each run's pipeline_state.yml, no model)")
    p.add_argument("--profile", required=True)
    p.add_argument("--run", help="one run folder name, e.g. PROJ-68294_FEATURE (default: every run)")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("bundle", help="one folder per story: copy its specs next to its test cases "
                                      "and write a README (the framework stays in the workspace)")
    p.add_argument("--profile", required=True)
    p.add_argument("--run", help="one run folder name, e.g. PROJ-68294_FEATURE (default: every run)")
    p.set_defaults(func=cmd_bundle)

    p = sub.add_parser("run", help="run the pipeline for one story")
    p.add_argument("--profile", required=True)
    p.add_argument("--story", default="",
                   help="JIRA story key, OR a path to a local JIRA XML export "
                        "(Tools > Export > XML) when JIRA is unreachable from here. "
                        "Optional: with no story, cases are derived from whatever "
                        "else is supplied (design doc, Figma, API spec, "
                        "--reference-testcases, or the profile's ingested "
                        "knowledge) and the sheet is keyed DOC-1")
    p.add_argument("--epic", default="",
                   help="required unless --story is an XML export carrying an Epic "
                        "Link, or no --story is given at all (then it is the sheet's "
                        "key prefix and defaults to DOC)")
    p.add_argument("--feature-link", default="",
                   help="JIRA Feature/Epic/Story/Test link or key — the writer walks "
                        "it into ancestor issues (Feature/Epic) and linked Zephyr Test "
                        "issues, each with its description, comments and attachments, "
                        "and folds all of it into generation. Independent of --story: "
                        "the story being generated for and the link that supplies its "
                        "surrounding context need not be the same issue. Same as "
                        "JIRA_FEATURE_LINK in .env.")
    p.add_argument("--figma", default="")
    p.add_argument("--api-spec", default="",
                   help="OpenAPI/Swagger JSON spec (path or URL) for the service "
                        "under test, so the writer produces API test cases for "
                        "criteria stated as endpoints and status codes")
    p.add_argument("--cycle", default="")
    p.add_argument("--design-doc", default="")
    imports = p.add_mutually_exclusive_group()
    imports.add_argument("--import-testcases", default="",
                         help="path to an existing .xlsx/.xlsm/.csv to import and "
                              "normalize instead of generating cases from the story")
    imports.add_argument("--import-from-jira", default="",
                         help="Zephyr Scale cycle id, or comma-separated case keys, "
                              "to import instead of generating. Needs ZEPHYR_API_TOKEN.")
    p.add_argument("--reference-testcases", default="",
                   help="path to an existing .xlsx/.xlsm/.csv used as a COVERAGE "
                        "BASELINE: the writer generates only what those cases do "
                        "not already cover. Unlike --import-testcases (which "
                        "replaces generation) this one informs it, so it can be "
                        "combined with a story, design doc, Figma or API spec.")
    p.add_argument("--include-manual", action="store_true",
                   help="also automate rows the sheet marks Execution Type=Manual")
    p.add_argument("--skip-execution", action="store_true",
                   help="stop after code generation (no live environment)")
    p.add_argument("--skip-bug-report", action="store_true",
                   help="skip writing BUG_LIST.md after review/self-heal")
    p.add_argument("--allow-failures", action="store_true",
                   help="exit 0 even when tests are still failing after self-healing "
                        "(default: exit 6, so CI cannot go green on a red suite)")
    p.add_argument("--max-run-tokens", type=int, default=None,
                   help="halt if this run exceeds N tokens. Overrides the profile; "
                        "0 means no cap.")
    p.add_argument("--workers", type=int, default=None)
    p.add_argument("--shard", default=None)
    p.add_argument("--force", action="append",
                   help="re-run a stage whose output already exists; repeatable")
    p.add_argument("--only", action="append", help="run only these stages; repeatable")
    p.add_argument("--screens", action="append",
                   help="crawl only these screens in the App Explorer stage; "
                        "repeatable. Omit to crawl every screen the profile "
                        "declares. Each named screen keeps its declared route")
    p.add_argument("--until", help="stop after this stage")
    p.add_argument("--non-interactive", action="store_true",
                   help="never prompt on failure; abort instead (use in CI)")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv()
    # Must run before anything reads APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD
    # (DOM inspection in automation_planner/coding_agent/self_healer/executor) —
    # without it those stay empty and every login silently fails. Previously
    # only each agent's own standalone `_cli()` called this; the orchestrator
    # path never did.
    from utils.env_config import resolve_app_env
    target = resolve_app_env()
    print(f"      TEST_ENV={target!r} -> APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD "
          f"resolved from {target.upper()}_* vars")
    try:
        return args.func(args)
    except (FileNotFoundError, FileExistsError, RuntimeError) as exc:
        print(f"{type(exc).__name__}: {exc}")
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130
    except Exception as exc:
        # An unexpected error is a bug or an environment problem, not something the
        # user should have to read a traceback to understand. The traceback is one
        # flag away for whoever has to fix it.
        if getattr(args, "debug", False) or os.environ.get("QA_DEBUG"):
            raise
        print(f"\n[ERROR] {type(exc).__name__}: {exc}\n"
              f"  Run `qa-pipeline doctor` to check your setup, or re-run with --debug "
              f"for the full traceback.")
        return 1
    finally:
        _run_log.close()      # restores stdout/stderr and closes run.log, whichever way we left


if __name__ == "__main__":
    raise SystemExit(main())
