"""The artifacts that cross a stage boundary, and their contracts.

One rule governs this module: **if two stages both need to agree on a value,
that value is declared here and validated on write AND on read.** Anything
a stage keeps to itself does not belong in this file.

The seven contracts, and the guarantee each one buys:

  knowledge_card    every fact carries a source, a confidence and a date.
                    Nothing in the ledger is anonymous, so a wrong locator
                    can always be traced to whatever asserted it.
  test_case         stable id, ordered steps, execution type, traced back to
                    an acceptance criterion.
  automation_plan   case id -> target file -> the elements it needs, every
                    one of which must resolve in the ledger before code is
                    written.
  generated_file    path, content, and THE JOIN: which case ids this file
                    implements. Declared by the agent that wrote the file,
                    not re-derived later by three other stages guessing.
  run_result        per-case status plus the failure-time evidence.
  classification    a verdict AND the citation supporting it.
  heal_outcome      what was patched, whether it verified, and what to write
                    back to the ledger.

PROPOSAL FIELDS. Every artifact a model produces carries `confidence`,
`source`, `declined` and `decline_reason`. This is the "decline over guess"
contract made structural rather than left to prose in a skill file: a stage
that cannot do its job honestly says so in a field the orchestrator reads,
instead of emitting a plausible guess that costs a 30-second Playwright
timeout to disprove. `self_healer` already proved this pattern works — it
was the only agent that could decline, and it is the only agent that never
looped without converging.
"""

from __future__ import annotations

from .schema import Field, Schema

# ---------------------------------------------------------------------------
# Shared vocabularies
#
# These are `choices=` sets rather than free strings because deterministic
# code downstream switches on every one of them. A model inventing a sixth
# provenance or a fourth verdict does not produce a slightly-off artifact;
# it produces one that silently falls through an if/elif chain.
# ---------------------------------------------------------------------------

#: Ordered worst -> best. `brain.trust` derives its ranking from this list's
#: ORDER, so the ordering here is load-bearing, not cosmetic.
PROVENANCE = [
    "inferred",        # a model said so and nothing corroborated it. Never usable for a locator.
    "legacy_testcase", # lifted from an imported sheet written for a previous release.
    "figma",           # read off a design frame; the built app may differ.
    "design_doc",      # a human wrote it down, possibly a while ago.
    "crawled",         # the App Explorer observed it on the live app this session.
    "recorded",        # a human demonstrated it under `playwright codegen`.
    "confirmed",       # a test using it actually passed against the live app.
]

CARD_TYPES = [
    "glossary",   # tester's word -> the app's real UI label
    "screen",     # a named screen, its route, its purpose
    "nav_edge",   # from_screen -> to_screen via an action
    "element",    # an interactive element and its verified locator
    "rule",       # a business or validation rule
    "env_quirk",  # "this device is mocked in QA"
    "test_data",  # a reusable data set
]

EXECUTION_TYPES = ["Automated", "Manual"]

#: These strings are taken VERBATIM from `skills/reviewer.skill.md`, which
#: already ships them to the model, and match the equality checks the heal
#: stage branches on ("fixable_test_issue", "known_flaky"). Renaming any of
#: them to something tidier — "product_bug" for "genuine_bug", say — would
#: mean the contract and the live pipeline disagreed about the one field the
#: Self-Healer branches on, so the tidier name is not worth having.
#: "infra_failure" is the one addition: the existing Reviewer had no way to
#: say "this did not fail, it never ran", which is what `executor.run`'s
#: failure_kind == "infra" actually means.
VERDICTS = [
    "genuine_bug",         # the app is wrong. Report it, never "heal" it.
    "known_flaky",         # recurring, environmental, already seen.
    "fixable_test_issue",  # our code is wrong in a mechanical way.
    "infra_failure",       # the run itself did not happen properly.
]

SEVERITIES = ["blocker", "major", "minor", "n/a"]

TEST_KINDS = ["ui", "api", "e2e"]


def _proposal_fields(what: str) -> list:
    """The four fields every model-produced artifact carries.

    `confidence` is the model's own estimate, and it is NOT trusted as a
    number — the orchestrator only ever compares it against a threshold to
    decide whether to escalate to the App Explorer. Its real value is that
    asking for it measurably reduces confident nonsense.
    """
    return [
        Field("confidence", "float", required=False, default=0.0,
              min_value=0.0, max_value=1.0,
              desc=f"how sure the agent is about this {what}, 0.0-1.0"),
        Field("source", "str", required=False, default="inferred", choices=set(PROVENANCE),
              desc="where this came from; see PROVENANCE"),
        Field("declined", "bool", required=False, default=False,
              desc="true if the agent refused to guess rather than produce this"),
        Field("decline_reason", "str", required=False, default="",
              desc="required when declined is true; what a human or the Explorer must supply"),
    ]


# ---------------------------------------------------------------------------
# L1 — the ledger
# ---------------------------------------------------------------------------

KNOWLEDGE_CARD = Schema(
    "knowledge_card",
    [
        Field("id", "str", pattern=r"^[a-z_]+:[A-Za-z0-9_.:\-/ ]+$",
              desc="'<type>:<natural key>', e.g. 'element:LoginPage/Sign in'"),
        Field("type", "str", choices=set(CARD_TYPES)),
        Field("key", "str", desc="the natural key within its type, e.g. a screen name"),
        Field("value", "any", desc="the fact itself; shape depends on type"),
        Field("source", "str", choices=set(PROVENANCE)),
        Field("confidence", "float", min_value=0.0, max_value=1.0),
        Field("last_verified", "str", desc="UTC ISO8601 of the last time this was corroborated"),
        Field("scope", "str", required=False, default="app",
              desc="'app', or a story/epic key when the fact is release-specific"),
        Field("evidence", "str", required=False, default="",
              desc="URL, file path, or run id that backs this card up"),
        Field("superseded_by", "str", required=False, default="",
              desc="id of the card that replaced this one; set instead of deleting"),
    ],
    desc="one atomic, sourced fact about the target application",
)

#: The `value` shape for an `element` card. Kept separate because this is the
#: one card type whose contents deterministic code reads field-by-field —
#: `verifiers.locator_check` resolves against it, and a malformed one there
#: means a hallucinated locator reaching generated code.
ELEMENT_VALUE = Schema(
    "element_value",
    [
        Field("screen", "str", desc="the screen card key this element lives on"),
        Field("logical_name", "str", desc="what a test case would call it"),
        Field("locator", "str", desc="the Playwright expression, verbatim"),
        Field("role", "str", required=False, default="", desc="ARIA role if known"),
        Field("accessible_name", "str", required=False, default="",
              desc="the element's real accessible name, for near-miss repair"),
        Field("unique", "bool", required=False, default=True,
              desc="false if this matched more than one node when observed"),
    ],
    desc="an interactive element and the locator that reaches it",
)

SCREEN_VALUE = Schema(
    "screen_value",
    [
        Field("name", "str"),
        Field("route", "str", required=False, default="", desc="URL path if stable"),
        Field("purpose", "str", required=False, default=""),
        Field("reached_by", "list", of="str", required=False, default=[],
              desc="ordered UI labels to click from the app root"),
    ],
    desc="a named screen in the application",
)


# ---------------------------------------------------------------------------
# L3 — stage artifacts
# ---------------------------------------------------------------------------

TEST_CASE = Schema(
    "test_case",
    [
        Field("test_case_id", "str", pattern=r"^[A-Za-z0-9_\-]+$",
              desc="stable across regeneration; the join key for the whole pipeline"),
        Field("title", "str"),
        Field("preconditions", "str", required=False, default=""),
        Field("steps", "list", of="str", min_items=1),
        Field("expected_result", "str"),
        Field("execution_type", "str", choices=set(EXECUTION_TYPES),
              desc="Manual rows are never coded unless --include-manual is passed"),
        Field("test_kind", "str", required=False, default="e2e", choices=set(TEST_KINDS)),
        Field("acceptance_criterion", "str", required=False, default="",
              desc="the AC text this case covers; coverage_check reads this"),
        Field("priority", "str", required=False, default="Medium"),
        Field("tags", "list", of="str", required=False, default=[]),
        Field("status", "str", required=False, default="",
              desc="the human review gate; must read exactly 'Reviewed' to proceed"),
        Field("depends_on", "list", of="str", required=False, default=[],
              desc="case ids that must run first; drives execution ordering"),
        *_proposal_fields("test case"),
    ],
    desc="one manual test case, in the format the rest of the pipeline consumes",
)

PLAN_ELEMENT = Schema(
    "plan_element",
    [
        Field("logical_name", "str", desc="what the step calls it"),
        Field("screen", "str"),
        Field("card_id", "str", required=False, default="",
              desc="the ledger card backing this; EMPTY means unresolved -> Explorer"),
        Field("locator", "str", required=False, default="",
              desc="filled from the ledger by the planner verifier, never by the model"),
    ],
    desc="one element a plan step needs, and its ledger backing",
)

AUTOMATION_PLAN = Schema(
    "automation_plan",
    [
        Field("test_case_id", "str"),
        Field("target_file", "str",
              desc="repo-relative, forward-slash. The Executor treats this as a REGEX, "
                   "so it must never be absolute or contain backslashes"),
        Field("test_kind", "str", choices=set(TEST_KINDS)),
        Field("page_objects", "list", of="str", required=False, default=[],
              desc="scaffold-relative paths, existing or to be created"),
        Field("components", "list", of="str", required=False, default=[]),
        Field("fixtures", "list", of="str", required=False, default=[]),
        Field("data_file", "str", required=False, default=""),
        Field("elements", "list", of=PLAN_ELEMENT, required=False, default=[],
              desc="every element the case touches; all must resolve before coding"),
        Field("navigation_keywords", "list", of="str", required=False, default=[],
              desc="the app's REAL UI labels for the click path to this screen"),
        Field("notes", "str", required=False, default=""),
        *_proposal_fields("plan"),
    ],
    desc="how one approved test case maps into the framework scaffold",
)

GENERATED_FILE = Schema(
    "generated_file",
    [
        Field("path", "str",
              desc="repo-relative, forward-slash, never absolute"),
        Field("content", "str"),
        # THE JOIN. Declared by the agent that wrote the file, which is what
        # replaces _resolve_case_file's four-tier guess.
        #
        # Optional at the SCHEMA level, mandatory for a spec: a page object,
        # component or fixture implements no case of its own, and requiring a
        # non-empty list of every file made the contract unsatisfiable for
        # every supporting file the Coding Agent legitimately emits —
        # `write_validated` then raised and killed the stage AFTER the files
        # were already on disk. `scaffold_check.check_file` enforces the real
        # rule ("a spec must declare at least one case id") where the file's
        # kind is actually known.
        Field("test_case_ids", "list", of="str", required=False, default=[],
              desc="every case id this file serves; REQUIRED and non-empty for kind='spec'"),
        Field("kind", "str", required=False, default="spec",
              choices={"spec", "page", "component", "fixture", "data", "util"}),
        Field("locators_used", "list", of="str", required=False, default=[],
              desc="every locator expression in the file, for the ledger writeback"),
        *_proposal_fields("file"),
    ],
    desc="one file the Coding Agent wrote, and the cases it implements",
)

CASE_RESULT = Schema(
    "case_result",
    [
        Field("test_case_id", "str"),
        Field("file", "str"),
        Field("status", "str", choices={"passed", "failed", "timedOut", "skipped", "interrupted"}),
        Field("duration_ms", "int", required=False, default=0),
        Field("error_message", "str", required=False, default=""),
        Field("failure_dom", "str", required=False, default="",
              desc="the DOM captured AT failure — the single best evidence a heal can have"),
        Field("screenshots", "list", of="str", required=False, default=[]),
        Field("trace", "str", required=False, default=""),
    ],
    desc="what happened to one case in one execution",
)

RUN_RESULT = Schema(
    "run_result",
    [
        Field("returncode", "int",
              desc="None/absent means the Executor never ran — a distinct outcome from "
                   "'ran and failed', and the reason executor.run returns rather than raises"),
        Field("ran", "bool"),
        # The values here are `executor._INFRA` / `executor._TEST_RESULTS`
        # VERBATIM. They were paraphrased ("test" for "test_results"), which
        # made this field unsatisfiable: every run that actually produced a
        # Playwright report carries "test_results", so `write_validated` raised
        # inside stage_execute and turned every successful execution into a
        # stage failure. "test" is still accepted so a run_result.json written
        # by an older build still reads back.
        Field("failure_kind", "str", required=False, default="",
              choices={"", "infra", "test_results", "test"},
              desc="'infra' means the environment broke, not the app; "
                   "'test_results' means Playwright ran and produced a report"),
        Field("error", "str", required=False, default=""),
        Field("stats", "dict", required=False, default={}),
        Field("cases", "list", of=CASE_RESULT, required=False, default=[]),
        Field("allure_report", "str", required=False, default=""),
        Field("test_files", "list", of="str", required=False, default=[]),
    ],
    desc="one Executor invocation's outcome",
)

CLASSIFICATION = Schema(
    "classification",
    [
        Field("test_case_id", "str"),
        Field("file", "str", required=False, default=""),
        Field("verdict", "str", choices=set(VERDICTS),
              desc="maps from the existing Reviewer's 'classification' field"),
        Field("severity", "str", required=False, default="n/a", choices=set(SEVERITIES),
              desc="only meaningful for genuine_bug; 'n/a' otherwise"),
        Field("reason", "str"),
        Field("citation", "str",
              desc="the trace/error text this verdict rests on. An uncited verdict is "
                   "rejected — it is the difference between a diagnosis and a guess"),
        Field("suggested_fix", "str", required=False, default=""),
        *_proposal_fields("classification"),
    ],
    desc="the Reviewer's call on one failure, with its evidence",
)

HEAL_OUTCOME = Schema(
    "heal_outcome",
    [
        Field("test_case_id", "str"),
        Field("file", "str"),
        Field("attempted", "bool"),
        Field("healed", "bool"),
        Field("attempts", "int", required=False, default=0),
        Field("from_memory", "bool", required=False, default=False,
              desc="true when locator memory answered it with no model call"),
        Field("old_locator", "str", required=False, default=""),
        Field("new_locator", "str", required=False, default=""),
        Field("patch_applied", "bool", required=False, default=False),
        Field("verified", "bool", required=False, default=False,
              desc="the re-run actually passed. A patch that was not verified is not a heal"),
        *_proposal_fields("heal"),
    ],
    desc="what self-healing did to one case, and whether it actually worked",
)


#: Registry, so `contracts.validate(name, data)` works by string and the CLI
#: can print any contract on request (`qa-pipeline contracts --show test_case`).
REGISTRY = {
    s.name: s for s in (
        KNOWLEDGE_CARD, ELEMENT_VALUE, SCREEN_VALUE,
        TEST_CASE, PLAN_ELEMENT, AUTOMATION_PLAN, GENERATED_FILE,
        CASE_RESULT, RUN_RESULT, CLASSIFICATION, HEAL_OUTCOME,
    )
}
