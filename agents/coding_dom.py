"""Deciding which cases need a live DOM re-inspection before code is written, and doing it.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

import os
import re
from pathlib import Path

from agents.automation_planner import _confidence
from agents.coding_cases import _load_planner_plans
from utils import dom_inspector

#: Below this, the Planner's own DOM inspection didn't confidently reach the
#: case's real target screen (see automation_planner.py's dom_inspection_
#: confidence) — Coding Agent re-inspects live using the Planner's
#: navigation_keywords plus the CASE's own Test Step/Expected Result text
#: (richer, more specific keywords than the Planner's short flow_summary)
#: rather than generating code from a guess.
_DOM_CONFIDENCE_THRESHOLD = 0.5


#: Planner-assigned dom_inspection_quality values meaning its snapshot is NOT
#: the case's real target screen (see automation_planner._grade_inspection).
#: Any of these forces a re-inspection here regardless of the model's
#: self-reported confidence — the grade is an observation of what the browser
#: actually returned, which always outranks a pre-browser self-assessment.
_UNUSABLE_DOM_QUALITY = {"login_page", "empty", "unavailable"}


#: Mirrors automation_planner._MIN_USEFUL_ELEMENTS. Used only as a fallback
#: for planner docs written BEFORE dom_inspection_quality existed, so an
#: older context/ directory still gets re-inspected rather than silently
#: generating code from a 0-element snapshot.
_MIN_USEFUL_ELEMENTS = 3


def _dom_snapshot_is_unusable(plan: dict) -> bool:
    """Whether the Planner's own inspection for this plan failed to produce a
    usable picture of the target screen.

    Trusts the Planner's dom_inspection_quality grade when present; falls
    back to inspecting the raw result for older planner docs. A plan that
    was never flagged for inspection at all is not "unusable" — there's
    simply nothing to re-check (its flow is covered by an existing POM)."""
    quality = plan.get("dom_inspection_quality")
    if quality:
        return quality in _UNUSABLE_DOM_QUALITY
    result = plan.get("dom_inspection_result")
    if not isinstance(result, dict):
        # Flagged for inspection but no result recorded at all — inspection
        # never ran (no APP_BASE_URL at plan time, or it crashed).
        return bool(plan.get("needs_dom_inspection"))
    if result.get("error"):
        return True
    elements = result.get("elements") or []
    return len(elements) < _MIN_USEFUL_ELEMENTS or not result.get("logged_in")


def _cases_needing_reinspection(planner_doc_path: Path) -> set:
    """Test Case IDs whose locators must be re-read from the live DOM here,
    because the Planner's snapshot can't be trusted as the target screen.

    Two independent triggers, either of which is sufficient:
      1. The model's own low confidence in its inspection decision
         (< _DOM_CONFIDENCE_THRESHOLD) — the original trigger.
      2. The snapshot came back UNUSABLE (login page, near-empty, or
         inspection unavailable). This case previously slipped through
         entirely: a plan graded "login_page" but self-reported at
         confidence 0.9 was never re-inspected, and the Coding Agent then
         wrote selectors for the target screen from a snapshot of the login
         form — the exact "guess a locator" failure the pipeline's
         never-fabricate rule exists to prevent. Confidence describes the
         model's reuse judgment; it cannot know the browser got stuck.
    """
    needing = set()
    for plan in _load_planner_plans(planner_doc_path):
        case_id = plan.get("test_case_id")
        if not case_id or not plan.get("needs_dom_inspection"):
            continue
        # A MISSING confidence means "never graded", which the Planner itself
        # reads as 1.0 (_confidence's default) — not 0. Treating it as 0
        # re-inspected every flagged plan live; an unusable snapshot is still
        # caught by _dom_snapshot_is_unusable below.
        low_confidence = _confidence(plan.get("dom_inspection_confidence")) < _DOM_CONFIDENCE_THRESHOLD
        if low_confidence or _dom_snapshot_is_unusable(plan):
            needing.add(case_id)
    return needing


#: Pulls literal element name strings out of a POM_SUMMARY.md's code
#: fences — e.g. `name: 'Image Editing Tool. Click to access...'` or
#: `name: /Split View/i` — so a KNOWN real navigation target (documented
#: from a past confirmed run) can be prioritized as an inspect() keyword,
#: instead of relying on word-frequency luck across the whole markdown
#: doc's prose crowding it out of the keyword cap.
_POM_NAME_RE = re.compile(r"""name:\s*['"/]([^'"/]{4,40})['"/]""")


def _pom_navigation_hints(existing_pom_summary: str, max_hints: int = 5) -> str:
    if not existing_pom_summary:
        return ""
    hints = _POM_NAME_RE.findall(existing_pom_summary)[:max_hints]
    return " ".join(hints)


def _plan_navigation_keywords(plan: dict) -> str:
    """The Planner's own click path in the app's real UI labels (see
    skills/automation_planner.skill.md's navigation_keywords rule), as a
    space-joined string for dom_inspector's keyword extraction."""
    if not plan:
        return ""
    keywords = plan.get("navigation_keywords") or []
    if isinstance(keywords, str):
        keywords = [keywords]
    return " ".join(str(k) for k in keywords if k)


def _reinspect_cases(cases: list, reinspect_ids: set,
                     existing_pom_summary: str = "",
                     plans_by_id: dict = None) -> dict:
    """Best-effort, sequential (dom_inspector's browser session is a shared
    singleton — concurrent inspect() calls from multiple chunk threads would
    race on it) re-inspection for the cases _cases_needing_reinspection()
    identified — the Planner either wasn't confident about its inspection or
    its snapshot didn't actually show the target screen.
    Returns {} entirely if APP_BASE_URL isn't configured — same "no live
    environment" posture as the rest of the pipeline, not an error."""
    if not reinspect_ids or not os.environ.get("APP_BASE_URL"):
        return {}
    plans_by_id = plans_by_id or {}
    snapshots = {}
    for case in cases:
        cid = case.get("Test Case ID")
        if cid not in reinspect_ids:
            continue
        # Known real navigation labels (e.g. POM_SUMMARY.md documenting that
        # the app's own tile is named "Image Editing Tool", not whatever
        # domain name the test case text uses — "Face Comparison Tool admin")
        # are fed to inspect()'s keyword extraction alongside the case text,
        # which on its own can't bridge that naming gap: it never mentions
        # the app's actual UI label at all.
        #
        # Keyword ORDER matters: dom_inspector.inspect() keeps only the first
        # _MAX_KEYWORDS 4+-letter words, so anything later is never even
        # tried against the page. Highest-signal navigation labels first:
        #   1. the Planner's navigation_keywords — the app's real UI labels
        #      for the click path, chosen deliberately for this case
        #   2. POM_SUMMARY.md's recorded element names — real labels
        #      confirmed by a past run
        #   3. the case's own Test Step / Expected Result prose — richest in
        #      detail but written in DOMAIN wording that often never appears
        #      in the UI, and full of filler words that burn the cap
        nav_keywords = _plan_navigation_keywords(plans_by_id.get(cid))
        pom_hints = _pom_navigation_hints(existing_pom_summary)
        context_text = (f"{nav_keywords} {pom_hints} "
                        f"{case.get('Test Step') or ''} {case.get('Expected Result') or ''}")
        reason = (plans_by_id.get(cid, {}).get("dom_inspection_quality")
                  or "low-confidence")
        print(f"      Re-inspecting live DOM for {cid} "
              f"(Planner's inspection: {reason})...")
        try:
            snapshots[cid] = dom_inspector.inspect(
                context_text, cache_ttl=float(os.environ.get("QA_ROUTE_SNAPSHOT_TTL", "600")))
        except Exception as exc:
            snapshots[cid] = {"error": str(exc)}
    return snapshots
