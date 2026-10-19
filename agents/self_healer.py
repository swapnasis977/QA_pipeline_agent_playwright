"""
Agent 6: Self-Healer
IN:  Cases marked "fixable_test_issue" by Reviewer, failure trace/screenshot
OUT: Patched test file, updated locator memory, re-run result

Uses the cheap/fast model (see MODEL_ROUTING) since patches are narrow/mechanical.
"""

import json
import re
import sys
from pathlib import Path

if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.artifact_io import (
    read_json,
    read_text_file,
    resolve_within,
    write_json,
    write_text_file,
)
from agents.coding_cases import _MIN_REAL_CONTENT_LENGTH, _PLACEHOLDER_CONTENT_MARKERS
from agents.ts_check import _SYNTAX_FAIL, _SYNTAX_SKIPPED, _check_syntax_status
from utils import dom_inspector, heal_guard
from utils.llm_client import append_context_summary, call_agent, parse_json_response
from utils.run_layout import artifact_path

# Standing instructions live in skills/self_healer.skill.md (cached).
# Runs on the cheap/fast model (see MODEL_ROUTING) since patches are narrow.

#: A memory entry that was applied and did NOT actually fix the case (the
#: verification re-run still failed afterward) gets blocklisted here instead
#: of staying in locator_memory.json to be reused — and misapplied — again.
_FAILED_MEMORY_FILENAME = "locator_memory_failed.json"


def inspect_current_dom(context_hint: str) -> dict:
    """Real headless-Playwright inspection (see utils/dom_inspector.py),
    reusing the same logged-in session automation_planner.inspect_live_dom
    started — returns actual current elements instead of a placeholder."""
    return dom_inspector.inspect(context_hint)


def _memory_key(update: dict) -> str:
    return f"{update.get('old_locator', '')}::{update.get('new_locator', '')}"


def _looks_like_placeholder(content: str) -> bool:
    """Same check coding_agent.py applies to freshly-generated files (see
    its own _PLACEHOLDER_CONTENT_MARKERS/_MIN_REAL_CONTENT_LENGTH, reused
    here rather than duplicated) — applied to a self-heal patch for the
    same reason: a model can return syntactically valid JSON whose
    'patched_content' is itself a stub ("...", "TODO") rather than a real
    patch, and writing that to disk would silently CORRUPT a file that was
    at least partially working before the heal attempt."""
    stripped = (content or "").strip()
    return len(stripped) < _MIN_REAL_CONTENT_LENGTH or stripped in _PLACEHOLDER_CONTENT_MARKERS


#: Everything a "just wait longer" patch touches: a timeout value, a bare
#: sleep, a load-state wait. Normalising these away leaves the code that
#: actually DOES something, which is what a heal is supposed to change.
_TIMEOUT_VALUE_RE = re.compile(r"""(timeout\s*[:=]\s*)[0-9_]+""")
# The wait CALL itself, not its whole line: a real change sharing the line stays
# visible, while adding or removing a wait is still seen as a wait-only change.
_WAIT_CALL_RE = re.compile(
    r"""(?:await\s+)?[\w.]*\b(?:waitForTimeout|waitForLoadState|setTimeout|sleep)\s*\([^()]*\)\s*;?""")


def _normalize_for_timeout_compare(source: str) -> str:
    """The file with every timeout/wait knob flattened out. Ordinary numbers
    (`.nth(1)` -> `.nth(2)`, 'Step 1' -> 'Step 2', `toHaveCount(3)` -> `(4)`) are
    real changes and are deliberately NOT flattened."""
    text = _WAIT_CALL_RE.sub("", source or "")
    text = _TIMEOUT_VALUE_RE.sub(r"\1N", text)
    return " ".join(text.split())


#: Written into the header of every file `agents/pom_builder.py` generates.
GENERATED_MARKER = "GENERATED from the"


def _is_timeout_only_change(before: str, after: str) -> bool:
    """True when a patch changes nothing except how long the test waits.

    THE SELF-HEALER'S SIGNATURE FAILURE. Given a failure it cannot explain,
    the cheapest plausible-looking patch is a bigger number, and it will
    produce one every time — twice, since that exhausts MAX_HEAL_ATTEMPTS —
    while changing nothing that could affect the outcome.

    Observed end to end: a spec asserted `getByText('01/01/1990')` on a value
    that was on screen the whole time inside an `<input>`, where `getByText`
    can never match. Attempt 1 raised the timeout 20s -> 30s and added a
    `waitForLoadState('networkidle')`. Attempt 2 raised it again. Both were
    applied, both re-ran, both failed, and the real fix — assert on the input
    with `toHaveValue`, or blur the field so the app commits it — was never
    considered. The run cost two heals and two full executions to arrive
    exactly where it started.

    A timeout is a symptom. There IS a legitimate case for raising one — a
    step that genuinely takes longer than its budget — but then the patch is
    a deliberate decision the healer should have to state, not the default
    it falls back on when it has no diagnosis. So this is refused, and the
    refusal reason is fed back into the next attempt as prior_attempt_info
    (see orchestrator.stages.stage_heal), which is what makes the retry try
    something different rather than the same number again.
    """
    if not before or not after:
        return False
    if before.strip() == after.strip():
        return False  # no change at all is a different complaint
    return _normalize_for_timeout_compare(before) == _normalize_for_timeout_compare(after)


def _apply_patch(repo_output_dir: Path, file_path: str, patched_content: str,
                 origin: str) -> Path:
    """Writes a heal patch to disk, but only after two checks the previous
    direct `write_text` did not make. Returns the path written.

    1. PATH CONFINEMENT. `file_path` originates in the Reviewer LLM's
       "file" field (skills/reviewer.skill.md asks for a repo-relative
       path, but an instruction is not enforcement). Unconfined,
       `repo_output_dir / file_path` writes anywhere on disk for an
       absolute path or a "../.." — coding_agent.py already refuses exactly
       this for its own generated paths, and this is the same untrusted
       value reaching the same kind of write.

    2. SYNTAX. A heal OVERWRITES a file that was, by definition, already
       running (it produced a real test failure, not a collection error).
       A patch that doesn't compile therefore makes things strictly worse:
       the next verification re-run fails for a brand-new reason, which
       the Reviewer then re-diagnoses from scratch. The generation path
       already syntax-checks and rejects (coding_agent._check_syntax); the
       heal path wrote whatever it was handed. Now it type-checks the
       patched file and RESTORES the original on failure, so a bad patch
       leaves the repo exactly as it was rather than corrupted. A check
       that could not RUN (no node/tsc on PATH) is reported as unverified
       rather than counted as a pass — see _check_syntax_status.

    Raises ValueError in both cases — the heal stage treats a
    raised heal as a failed attempt and moves on/retries, which is the
    correct outcome for "this patch was not safe to apply".
    """
    target = resolve_within(repo_output_dir, file_path)
    if target is None:
        raise ValueError(
            f"self_healer: refusing to patch '{file_path}' — it resolves outside "
            f"'{repo_output_dir}'. A heal target must be a repo-relative path."
        )
    if not target.is_file():
        raise ValueError(
            f"self_healer: refusing to patch '{file_path}' — no such file under "
            f"'{repo_output_dir}'. Healing creates no new files, so this is a "
            f"stale or mis-resolved path from the Reviewer's 'file' field."
        )

    original = target.read_text(encoding="utf-8")
    write_text_file(target, patched_content)

    status, detail = _check_syntax_status(target, repo_root=Path(repo_output_dir).resolve())
    if status == _SYNTAX_FAIL:
        write_text_file(target, original)
        raise ValueError(
            f"self_healer: the {origin} patch for {file_path} does not compile, so it "
            f"was NOT applied (original file restored): {detail}"
        )
    if status == _SYNTAX_SKIPPED:
        # The patch is applied — refusing to heal because no toolchain is
        # installed would break the pipeline on any machine without
        # node/tsc, and the pre-existing behavior was to apply unchecked.
        # But it is NOT silently reported as verified: "couldn't check" and
        # "checked and fine" are different facts, and only the second one
        # justifies overwriting a file that was already running.
        print(f"      ⚠️  Applied the {origin} patch to {file_path} WITHOUT a syntax "
              f"check ({detail}) — if the verification re-run now fails to collect "
              f"this file, suspect the patch itself.")
    return target


#: How much of a file's head is searched for GENERATED_MARKER (it is in the header).
_GENERATED_HEAD_CHARS = 600

#: An `old_locator` at least this long is specific enough to trust without it also
#: being quoted by the failure; shorter ones ("button", "#a") match anywhere.
_MIN_UNCORROBORATED_LOCATOR_LEN = 8

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _strip_ansi(text) -> str:
    """Playwright colours its messages; the codes waste truncation budget and skew diffs."""
    return _ANSI_RE.sub("", str(text or ""))


def _memory_entry_relevant(old_locator: str, evidence_text: str) -> bool:
    """A remembered swap applies only when its old locator is specific, or this
    failure actually mentions it. Otherwise a short/generic string ("button") would
    match anywhere in any file and get rewritten blindly."""
    needle = " ".join(str(old_locator).split()).lower()
    if len(needle) >= _MIN_UNCORROBORATED_LOCATOR_LEN:
        return True
    quoteless = needle.replace('"', "'")
    haystack = " ".join(str(evidence_text).split()).lower().replace('"', "'")
    return bool(needle) and quoteless in haystack


def _inside_comment(source: str, pos: int) -> bool:
    """Whether `pos` sits on a `//` or `*` comment line (a mention, not a locator)."""
    line_start = source.rfind("\n", 0, pos) + 1
    return source[line_start:pos].lstrip().startswith(("//", "*", "/*"))


#: Elements of the DOM snapshot sent to the model (matches the Reviewer's cap).
_MAX_DOM_ELEMENTS = 40
_INTERACTIVE_ROLES = frozenset({
    "button", "link", "textbox", "searchbox", "combobox", "listbox", "option",
    "checkbox", "radio", "switch", "slider", "spinbutton", "tab", "menuitem",
    "menuitemcheckbox", "menuitemradio", "input", "select", "textarea", "a"})


def _cap_dom(dom_info: dict) -> dict:
    """The snapshot with at most _MAX_DOM_ELEMENTS elements, interactive ones first
    (stable, so the original order is kept within each group). The full list runs to
    hundreds of entries and the heal decision turns on the interactive ones."""
    elements = dom_info.get("elements") if isinstance(dom_info, dict) else None
    if not isinstance(elements, list) or len(elements) <= _MAX_DOM_ELEMENTS:
        return dom_info
    def interactive(el):
        return isinstance(el, dict) and str(el.get("role") or el.get("tag") or "").lower() in _INTERACTIVE_ROLES
    ranked = sorted(elements, key=lambda el: 0 if interactive(el) else 1)  # sorted() is stable
    capped = {**dom_info, "elements": ranked[:_MAX_DOM_ELEMENTS],
              "element_count": len(elements)}
    if isinstance(capped.get("raw_snapshot"), str):
        capped["raw_snapshot"] = capped["raw_snapshot"][:1500]
    return capped


def _fuzzy_find(content: str, old_locator: str):
    """Finds old_locator in content even if reformatted (different quote
    style, extra/collapsed whitespace) — an exact substring check missed
    this class of trivial reformatting, which was enough to make an
    otherwise-valid memory hit silently fall through to a fresh (paid) LLM
    call. Returns a compiled match object, or None if truly not present.
    Builds a regex from old_locator: literal text escaped, but runs of
    whitespace become `\\s+` and quote characters become a `['\"]` class."""
    if not old_locator:
        return None
    if old_locator in content:
        # Fast path — exact match, no need to build/run a regex for it.
        return re.compile(re.escape(old_locator)).search(content)

    pattern_parts = []
    for chunk in re.split(r"(\s+|['\"])", old_locator):
        if not chunk:
            continue
        if chunk.isspace():
            pattern_parts.append(r"\s+")
        elif chunk in ("'", '"'):
            pattern_parts.append(r"""['"]""")
        else:
            pattern_parts.append(re.escape(chunk))
    try:
        return re.compile("".join(pattern_parts)).search(content)
    except re.error:
        return None  # malformed pattern from unusual input — no match, not a crash


def run(broken_file_content: str, failure_reason: str, feature_dir: Path,
        repo_output_dir: Path, file_path: str, suggested_fix: str = "",
        attempt: int = 1, prior_attempt_info: dict = None,
        navigation_context: str = "", failure_dom: dict = None,
        related_files: dict = None, known_locators: str = "",
        action_timeline: dict = None, raw_error: str = "") -> dict:
    """`raw_error` is Playwright's OWN error text for the failing case (verbatim,
    truncated). `failure_reason` is the Reviewer's prose about it; the prose lost the
    expected/received values, which are exactly what separates "the element moved"
    (fixable) from "the app now shows different text" (a product change).

    `navigation_context` is the Planner's own `navigation_keywords` for
    this case — the app's real UI labels for the click path. It is what
    drives the DOM re-inspection below.

    Previously that inspection was driven by `failure_reason`, i.e. the
    REVIEWER'S PROSE ("Timeout 30000ms exceeded while waiting for locator
    ..."). dom_inspector.inspect() extracts 4+-letter words from whatever
    it's given and fuzzy-clicks them against the page, so as navigation
    input that text is noise: the browser never left the dashboard, and the
    healer then "fixed" a locator against a snapshot of the wrong screen.
    Attempt 2 escalated to a stronger model given the SAME wrong snapshot,
    which is why healing churned instead of converging.

    `related_files` is {path: content} for the page objects this spec
    imports. THE BROKEN LOCATOR IS USUALLY NOT IN THE SPEC. This pipeline's
    own conventions forbid a raw locator in a `.spec.ts` file, so every
    locator lives in a page object — and the healer used to be handed the
    spec alone. It diagnosed correctly and then had to decline, in its own
    words: "the fix must be applied in the page object file, but the
    Self-Healer can only modify the single file whose content was supplied".
    A healer that can only edit the one file guaranteed not to contain the
    defect cannot heal anything. It may now return `patched_file_path` to
    name which of these files its patch belongs to.

    `known_locators` is the ledger's verified element vocabulary — the same
    `brain.catalogue` rendering the Planner is grounded in, names plus the
    real selector each one resolves to on the running app.

    THIS AGENT WAS THE ONLY ONE WITHOUT IT. Its whole job is producing a
    correct locator, and it was the single agent with no access to the store
    that exists to hold correct locators; it saw the broken file, the failure
    text and one DOM snapshot, so when that snapshot came back empty it had
    nothing left and could only decline. Observed exactly that way: the healer
    said "the snapshot provides no element data ... a patch here would be a
    guess" while the ledger held the element it needed, crawled and confirmed.
    Defaults to "" so a caller with no ledger (a direct `python
    agents/self_healer.py` run) behaves exactly as before.

    `failure_dom`, when provided, is the accessibility snapshot captured by
    Playwright AT THE MOMENT OF FAILURE (see
    the scaffold's src/fixtures/index.ts failure-snapshot fixture, harvested by
    utils/failure_dom.py). It outranks any re-inspection: it is the actual
    failing screen at the actual failing moment, rather than a screen this
    process tried to navigate back to minutes later. When it's present the
    live re-inspection is skipped entirely — saving a browser launch per
    heal as well as being strictly better evidence.
    """
    # Locator memory is checked FIRST — if we've already healed this exact
    # locator before, we can potentially skip the DOM inspection + LLM call
    # entirely. This is the biggest per-fix saving: recurring flaky patterns
    # get patched from memory, not re-diagnosed from scratch every time.
    # Corrupt/hand-edited memory files are treated as absent rather than
    # crashing a heal that has nothing to do with them (see artifact_io).
    memory_path = artifact_path(feature_dir, "locator_memory.json")
    memory = read_json(memory_path, default=[])
    failed_path = artifact_path(feature_dir, _FAILED_MEMORY_FILENAME)
    failed_keys = set(read_json(failed_path, default=[]))

    # EVERY file this heal can touch, not just the spec.
    #
    # The lookup used to run against `broken_file_content` alone. That is the
    # spec — and this pipeline's conventions forbid a raw locator in a spec
    # (see coding_agent._inline_locators_in_spec, and this function's own
    # docstring). Every locator lives in a page object, which arrives in
    # `related_files`. So on a correctly generated suite the memory searched
    # the one file guaranteed not to contain the defect, essentially never
    # hit, and every recurrence of an already-solved failure paid for a fresh
    # diagnosis. The spec goes FIRST so the file actually under diagnosis
    # wins a tie and the same failure heals the same way on every run.
    searchable = {file_path: broken_file_content, **(related_files or {})}

    # Matched on the locator only (fuzzy — see _fuzzy_find), skipping any
    # pairing already confirmed NOT to fix things (see mark_heal_result).
    # The previous version also required m["reason"] == failure_reason —
    # byte equality between the Reviewer's prose and this agent's own
    # phrasing, two independently generated strings that essentially never
    # match, so the memory could never hit and every recurrence paid for a
    # fresh LLM call.
    raw_error = _strip_ansi(raw_error)
    # Generated files (page objects, routes) are rebuilt on every run and the LLM
    # path refuses to patch them; a remembered fix must not write into them either.
    searchable = {p: c for p, c in searchable.items()
                  if GENERATED_MARKER not in str(c)[:_GENERATED_HEAD_CHARS]}
    evidence_text = " ".join((failure_reason or "", raw_error, suggested_fix or ""))
    known_fix = match = memory_target = None
    for m in memory:
        if not m.get("old_locator") or _memory_key(m) in failed_keys:
            continue
        if not _memory_entry_relevant(m["old_locator"], evidence_text):
            continue
        for candidate_path, candidate_source in searchable.items():
            found = _fuzzy_find(candidate_source, m["old_locator"])
            if found and not _inside_comment(candidate_source, found.start()):
                known_fix, match, memory_target = m, found, candidate_path
                break
        if known_fix:
            break

    if known_fix:
        source = searchable[memory_target]
        patched = source[:match.start()] + known_fix["new_locator"] + source[match.end():]
        # Validated exactly like an LLM-produced patch: a remembered fix is
        # still only known to have worked on the file it was LEARNED from,
        # and this call is applying it to a different file whose surrounding
        # syntax it was never checked against.
        try:
            # The same "must not prove less" check an LLM patch gets: a remembered
            # fix skipped it, so a bad entry could change an assertion unchecked.
            weaker = heal_guard.weakened(source, patched)
            if weaker:
                raise ValueError("the remembered fix would make the test prove less - "
                                 + "; ".join(weaker))
            _apply_patch(repo_output_dir, memory_target, patched, origin="remembered")
        except ValueError as exc:
            # A REJECTED MEMORY ENTRY BLOCKLISTS ITSELF AND STANDS ASIDE.
            #
            # This used to propagate. The heal stage counted the raise as a
            # failed attempt, so `mark_heal_result` — the only thing that
            # blocklists — was never reached: the bad entry stayed in
            # locator_memory.json and re-poisoned every future heal of this
            # file, forever, without the LLM that could have fixed it
            # properly ever being asked. Blocklisting here and falling
            # through costs one LLM call ONCE, instead of failing every time.
            print(f"      ⚠️  A remembered fix for {memory_target} was rejected "
                  f"({exc}) — blocklisting it and diagnosing this failure fresh.")
            mark_heal_result(feature_dir, known_fix, success=False)
            failed_keys.add(_memory_key(known_fix))
        else:
            append_context_summary(
                feature_dir, "self_healer",
                f"Reused known fix for '{failure_reason}' on {memory_target} — "
                f"no LLM call needed."
            )
            return {"patched_content": patched, "patched_file_path": memory_target,
                    "locator_memory_update": known_fix, "from_memory": True}

    # Ground truth first: the failure-time snapshot, when the run captured
    # one. Only fall back to re-navigating a live browser when it didn't —
    # and even then, navigate by the Planner's real UI labels rather than by
    # the failure prose (see this function's docstring).
    if failure_dom:
        # Spell out that this snapshot IS the failing screen, and that its
        # `url` proves nothing about WHICH screen it is.
        #
        # Playwright took it on the page under test at the moment the step
        # threw, so by construction there is no other screen it could be. But
        # in a single-page app every screen is served from one address — this
        # project's target app reports
        # ".../dashboard/home/?customer_code=demo" on the dashboard, the
        # registration context popup and the biometric capture screen alike —
        # and a model reading a bare "captured at failure time" label next to
        # that url concludes it was handed the wrong page. Observed exactly
        # that way: a snapshot listing "Start Capture button", "Issue with
        # eyes?" and the camera dropdown (unmistakably the capture screen)
        # was dismissed as "the dashboard/home page", and the healer answered
        # with prose instead of a patch. The element list is the evidence; the
        # url is not.
        dom_info = {**failure_dom, "_source": (
            "captured at failure time: this IS the failing screen; ignore 'url' "
            "(single-page app, same url on every screen), judge by the elements.")}
        print(f"      Healing against the DOM captured AT FAILURE for {file_path} "
              f"(no re-navigation needed).")
    else:
        dom_info = inspect_current_dom(navigation_context or failure_reason)
        dom_info = {**dom_info, "_source": (
            "re-inspected live now, navigated by the Planner's navigation_keywords"
            if navigation_context else
            "re-inspected live now; NO navigation_keywords were available for this "
            "case, so the click path was derived from the failure text and this "
            "snapshot may not be the failing screen — treat its locators with "
            "suspicion and prefer declining over guessing")}

    # The Reviewer already diagnosed this failure and produced a concrete
    # suggested_fix; passing it through means this (cheaper, smaller) model
    # applies that diagnosis rather than re-deriving it from scratch.
    # (skills/self_healer.skill.md has a pointer describing this "prior
    # attempt" mechanism for future prompt editors.)
    prior_attempt_block = ""
    if isinstance(prior_attempt_info, dict) and prior_attempt_info:
        prior_update = prior_attempt_info.get("locator_memory_update") or {}
        if prior_update.get("old_locator"):
            prior_change = (f"changing {prior_update['old_locator']!r} to "
                             f"{prior_update['new_locator']!r}")
        else:
            prior_change = (prior_attempt_info.get("patched_content", "")[:200]
                             or "an unspecified change")
        prior_attempt_num = prior_attempt_info.get("attempt", attempt - 1)
        if prior_attempt_info.get("rejected"):
            prior_attempt_block = f"""

Attempt {prior_attempt_num} was REFUSED before it ran, so nothing was changed and
the original file is what you see above. The refusal was:
{str(prior_attempt_info.get("rejected_reason", ""))[:600]}
Fix that problem; the diagnosis itself may have been right."""
        else:
            prior_attempt_block = f"""

Attempt {prior_attempt_num} already tried {prior_change} and the re-run still
failed with the same/similar error — that change has been REVERTED, so the file
above is the original. Do NOT repeat that exact change; diagnose a different
root cause."""

    raw_error_block = ""
    if raw_error and raw_error.strip():
        raw_error_block = ("\nPlaywright's own error (verbatim; compare Expected vs Received "
                           "before deciding this is a locator problem):\n"
                           + raw_error.strip()[:1500] + "\n")

    related_block = ""
    if related_files:
        # Two groups, because only one of them may be edited. A GENERATED page
        # object is rebuilt on every Coding Agent run, so a patch there is lost
        # (and `run` now refuses it). Offering it as "patch here" wasted an
        # attempt every time the defect lived in one.
        editable = {p: c for p, c in related_files.items()
                    if GENERATED_MARKER not in str(c)[:600]}
        readonly = {p: c for p, c in related_files.items() if p not in editable}

        def _render(files):
            return "\n\n".join(f"--- {p} ---\n{c}" for p, c in files.items())

        parts = []
        if editable:
            parts.append(f"""
Page objects this spec imports that YOU MAY PATCH. To patch one, return its
exact path as "patched_file_path" and that whole file's new content as
"patched_content". Omit "patched_file_path" only when the fix belongs in the
spec.

{_render(editable)}""")
        if readonly:
            parts.append(f"""
GENERATED page objects - READ-ONLY context. They are rebuilt on every run, so
any patch to them is lost and will be REFUSED. If the broken locator is defined
here, do not patch it: decline with an "unresolved_reason" that names the element
and the file, so the fix is made in the plan or the ledger instead.

{_render(readonly)}""")
        related_block = "\n" + "\n".join(parts)

    # HOW FAR DID IT GET? A failure on step 1 of 20 and a failure on step 13
    # want opposite diagnoses, and nothing here could tell them apart. See
    # utils.failure_dom.extract_timeline for the run this misled.
    timeline_block = ""
    if isinstance(action_timeline, dict) and action_timeline:
        done = action_timeline.get("steps_completed", 0)
        lines = "\n".join(
            f"  {s.get('duration_ms', 0):>7}ms  {s.get('title', '')}"
            for s in action_timeline.get("steps") or [])
        early = action_timeline.get("failed_early")
        timeline_block = f"""

HOW FAR THE TEST GOT BEFORE FAILING ({done} step(s) completed in
{action_timeline.get('duration_ms', 0)}ms). Last step reached:
{action_timeline.get('last_step', '(none)')!r}
{lines}
""" + ("""
IT FAILED AT THE VERY START. Before touching any locator, ask whether the page
was the page at all: a session that did not authenticate, a navigation that did
not happen, or an app that redirected leaves EVERY locator on that screen
unfindable. A selector fix cannot repair a page that was never loaded, and
"element not found" looks identical in both cases.""" if early else """
It failed deep into the flow, so earlier steps — navigation and sign-in
included — demonstrably worked. That makes a genuine locator or state problem
at this step far more likely than a page-level one.""")

    # The ledger block goes ABOVE the snapshot: it is standing, confirmed
    # evidence, whereas the snapshot may be empty or of the wrong screen.
    ledger_block = ""
    if known_locators:
        ledger_block = f"""

VERIFIED LOCATORS FROM THIS PROJECT'S LEDGER. Each was confirmed against the
running application by a crawl or a human recording, and is the SAME
vocabulary the Planner was required to choose from. If the element you need
appears here, use its selector VERBATIM — prefer it over anything you would
otherwise compose, and over the DOM snapshot below when the two disagree.
This list is standing evidence; the snapshot is a single moment and can be
empty or of the wrong screen.
{known_locators}"""

    user_prompt = f"""Failing file content ({file_path}):
{broken_file_content}
{related_block}

Failure reason: {failure_reason}
{raw_error_block}
Reviewer's suggested fix: {suggested_fix or "(none provided)"}
{timeline_block}{ledger_block}

DOM info for the failing screen (see "_source" — it says whether these
elements were captured at the moment of failure or re-inspected afterward;
use ONLY locators that appear here, and decline rather than invent one):
{json.dumps(_cap_dom(dom_info), separators=(",", ":"), default=str)}
{prior_attempt_block}

Produce the patch now."""

    raw = call_agent(
        agent_name="self_healer",
        user_prompt=user_prompt,
        feature_dir=feature_dir,
        max_tokens=8000,
        attempt=attempt,
    )

    patch = parse_json_response(raw, feature_dir, "self_healer")
    if not isinstance(patch, dict) or not patch.get("patched_content"):
        # Per skills/self_healer.skill.md, the model may explicitly decline
        # to patch (a genuine product bug, or something else that isn't a
        # mechanical locator/timing/data fix it can confidently make) by
        # returning patched_content: null alongside a non-empty
        # unresolved_reason. That's a distinguishable, expected outcome —
        # NOT the same as a malformed/empty response — so it's surfaced as a
        # normal return value instead of an exception, letting
        # the heal stage stop retrying immediately instead of
        # burning a second attempt (and a second LLM call) re-asking a model
        # that already said this isn't a fix it can make.
        reason = isinstance(patch, dict) and patch.get("unresolved_reason")
        if isinstance(reason, str) and reason.strip():
            append_context_summary(
                feature_dir, "self_healer",
                f"Declined to auto-fix {file_path} for '{failure_reason}': {reason.strip()}"
            )
            return {
                "patched_content": None,
                "locator_memory_update": None,
                "declined": True,
                "unresolved_reason": reason.strip(),
            }
        raise ValueError(
            "self_healer: model response is missing 'patched_content'; "
            f"got keys {list(patch) if isinstance(patch, dict) else type(patch).__name__}."
        )
    if _looks_like_placeholder(patch["patched_content"]):
        raise ValueError(
            f"self_healer: 'patched_content' for {file_path} looks like a placeholder/stub, "
            f"not a real patch (got: {patch['patched_content'].strip()[:80]!r}) — refusing to "
            f"overwrite a file that was at least partially working with this. Retrying (if "
            f"attempts remain) will use a fresh call; see orchestrator.stages.MAX_HEAL_ATTEMPTS."
        )

    # A BIGGER NUMBER IS NOT A FIX. Refused here rather than discouraged in
    # the skill, because this is the failure mode the agent falls into
    # precisely when it has no diagnosis — exactly when a prompt rule is
    # least likely to hold. See _is_timeout_only_change.
    if _is_timeout_only_change(broken_file_content, patch["patched_content"]):
        raise ValueError(
            f"self_healer: the patch for {file_path} changes nothing but timeout values "
            f"and wait calls, so re-running it can only produce the same failure more "
            f"slowly. A timeout is a symptom — diagnose what the step is waiting for. "
            f"If an element never appears, fix the LOCATOR or the INTERACTION: check "
            f"whether the value is inside an input (assert toHaveValue, not getByText), "
            f"whether the control is disabled via a CSS class, whether the app commits "
            f"on `change` so the field needs a .blur() after .fill(), or whether the "
            f"click is being intercepted. Only if the step legitimately takes longer "
            f"than its budget is a larger timeout the answer — and then say so and "
            f"change nothing else."
        )

    # The model may target a page object instead of the spec. Only ever a
    # file this call actually offered it — never an arbitrary path out of a
    # model response, which is the same confinement rule `_apply_patch`
    # enforces on disk.
    target_path = patch.get("patched_file_path") or file_path
    # GENERATED FILES ARE NOT PATCHABLE. Page objects and the route helper are
    # rebuilt from the Planner's grounded locators and the route map on every
    # Coding Agent run, so a heal there is lost on the next run — and while it
    # lasts it bypasses the ledger (observed: a heal rewrote grounded toolbar
    # locators and pointed the upload at a fixture file that did not exist).
    # They stay visible to the healer as context; the fix belongs in the plan,
    # the ledger or routes.json.
    patchable = {path for path, source in (related_files or {}).items()
                 if GENERATED_MARKER not in str(source)[:600]}
    allowed = {file_path} | patchable
    if target_path not in allowed:
        reason = ("is generated (rebuilt on every run, so the fix would be lost)"
                  if target_path in (related_files or {}) else "was not offered to it")
        # REFUSED, not redirected. This used to fall back to the spec, but
        # `patched_content` is the OTHER file's text: the page object's source
        # was then validated and written over the spec. Only an accidental drop
        # in the assertion count stood between that and a destroyed test.
        raise ValueError(
            f"self_healer: the patch targets {target_path!r}, which {reason}. Nothing "
            f"was written. If the defect is in a generated file the fix belongs in the "
            f"plan, the ledger or routes.json - decline with an unresolved_reason.")
    patch["patched_file_path"] = target_path

    # A HEAL MUST NOT PROVE LESS THAN THE TEST DID. Held in code, not left to
    # the skill: a misclassified product bug is "fixed" by weakening its
    # assertion, the re-run goes green, and the defect is reported healed.
    original_target = (broken_file_content if target_path == file_path
                       else read_text_file(Path(repo_output_dir) / target_path))
    weaker = heal_guard.weakened(original_target, patch["patched_content"])
    if weaker:
        raise ValueError(
            f"self_healer: the patch for {target_path} would make the test prove less — "
            + "; ".join(weaker) + ". A heal may change how a step finds or drives an "
            "element, never what the test asserts. If the application genuinely behaves "
            "differently from the expectation, that is a PRODUCT defect: decline with an "
            "unresolved_reason instead of patching the assertion.")
    # A swap the verification re-run already DISPROVED must not be applied again.
    # The blocklist was only consulted on the remembered-fix path, so a model that
    # proposed the same failed pair twice had it applied twice.
    proposed = patch.get("locator_memory_update")
    if isinstance(proposed, dict) and proposed.get("old_locator") \
            and _memory_key(proposed) in failed_keys:
        raise ValueError(
            "self_healer: this exact locator change was already tried and failed "
            "verification, so it was NOT applied again. Diagnose a different cause.")
    _apply_patch(repo_output_dir, target_path, patch["patched_content"], origin="generated")

    # Only record a memory entry that's actually usable as one — a partial or
    # missing update would otherwise poison future lookups.
    update = patch.get("locator_memory_update")
    if (isinstance(update, dict) and update.get("old_locator") and update.get("new_locator")
            and not any(_memory_key(m) == _memory_key(update) for m in memory)):
        memory.append(update)
        write_json(memory_path, memory)

    append_context_summary(
        feature_dir, "self_healer",
        f"Patched {file_path} for '{failure_reason}' via LLM call, saved to locator memory."
    )

    return patch


def mark_heal_result(feature_dir: Path, locator_memory_update: dict, success: bool):
    """Called by the self-heal stage AFTER the verification
    re-run confirms whether a patch actually fixed the case. A patch that
    didn't (success=False) — despite maybe having been applied FROM memory,
    or having just been recorded to memory this attempt — gets its
    (old_locator, new_locator) pairing blocklisted so it's never reused or
    re-recorded again, instead of a bad fix silently staying in
    locator_memory.json to be applied (and fail) again on the next
    similar-looking case. A no-op if there's no usable update to act on."""
    if not isinstance(locator_memory_update, dict) or not locator_memory_update.get("old_locator"):
        return
    key = _memory_key(locator_memory_update)
    if success:
        return  # already recorded as a working fix by run() above; nothing to do

    failed_path = artifact_path(feature_dir, _FAILED_MEMORY_FILENAME)
    failed = set(read_json(failed_path, default=[]))
    if key not in failed:
        failed.add(key)
        write_json(failed_path, sorted(failed))
        append_context_summary(
            feature_dir, "self_healer",
            f"Blocklisted a locator fix that did not resolve the failure on re-run "
            f"(old_locator: {locator_memory_update.get('old_locator', '')[:80]!r}) — "
            f"will not be reused from memory again."
        )


def _cli():
    """Standalone entry point — heal one broken file on its own without
    going through the rest of the pipeline."""
    import argparse

    from utils.env_config import resolve_app_env

    resolve_app_env()  # DOM inspection below reads APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file-path", required=True,
                         help="Path (relative to --repo-dir) of the broken generated file.")
    parser.add_argument("--repo-dir", required=True)
    parser.add_argument("--failure-reason", required=True)
    parser.add_argument("--suggested-fix", default="")
    parser.add_argument("--feature-dir", required=True)
    parser.add_argument("--attempt", type=int, default=1)
    args = parser.parse_args()

    feature_dir = Path(args.feature_dir)
    feature_dir.mkdir(parents=True, exist_ok=True)
    repo_output_dir = Path(args.repo_dir)
    broken_target = resolve_within(repo_output_dir, args.file_path)
    if broken_target is None or not broken_target.is_file():
        parser.error(f"--file-path '{args.file_path}' does not resolve to a file "
                     f"inside --repo-dir '{repo_output_dir}'.")
    broken_file_content = broken_target.read_text(encoding="utf-8")

    patch = run(
        broken_file_content=broken_file_content, failure_reason=args.failure_reason,
        feature_dir=feature_dir, repo_output_dir=repo_output_dir, file_path=args.file_path,
        suggested_fix=args.suggested_fix, attempt=args.attempt,
    )
    print(f"\n✅ Patched {args.file_path} "
          f"(from_memory={patch.get('from_memory', False)}).")


if __name__ == "__main__":
    _cli()
