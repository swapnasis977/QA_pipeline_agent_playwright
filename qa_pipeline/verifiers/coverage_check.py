"""Does the generated sheet actually cover the story?

The Test Case Writer's failure mode is not usually a malformed sheet — the
schema gate catches that. It is a sheet that looks entirely reasonable and
quietly omits an acceptance criterion, or covers one criterion five times
under five different titles. Neither shows up in any downstream stage: the
Planner plans what it is given, the Coding Agent codes what it is planned,
everything passes, and the story ships with an untested requirement.

The human review gate is the real defence here, and this verifier exists to
make that gate cheap to pass well. A reviewer told "AC-3 has no test case" can
check one thing in seconds. A reviewer handed forty prose cases and asked
whether anything is missing will approve them, because that is not a task a
person can actually do by reading.

This gate is advisory by default (`strict=False`): a story whose acceptance
criteria were never written down properly should not be unable to run, and
plenty of real JIRA stories have none. It warns, and the human decides.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from .result import VerifyResult

#: Two case titles at or above this similarity are treated as the same case
#: said twice. Matches the threshold used elsewhere in the pipeline for the
#: same "is this the same thing worded differently" judgment, so there is one
#: similarity notion rather than several.
_DUPLICATE_THRESHOLD = 0.85

_AC_SPLIT = re.compile(r"(?:^|\n)\s*(?:AC[-\s]?\d+|[-*•]|\d+[.)])\s+", re.I)


def split_criteria(text: str) -> list:
    """Pulls individual acceptance criteria out of a story's description.

    Deliberately lenient: JIRA acceptance criteria are written as bullets,
    numbered lists, 'AC-1:' prefixes, or plain paragraphs, and a parser that
    only handled one of those would report "no criteria" on most real
    stories — which reads as "nothing to check" rather than "I could not
    tell", and is the more dangerous of the two.
    """
    if not str(text or "").strip():
        return []
    parts = [p.strip() for p in _AC_SPLIT.split(str(text)) if p and p.strip()]
    if len(parts) <= 1:
        parts = [p.strip() for p in str(text).split("\n") if len(p.strip()) > 20]
    return [p for p in parts if len(p) > 12]


#: Words that appear in almost every acceptance criterion and every test case
#: title, and therefore prove nothing about whether one covers the other.
#: Without these, a criterion sharing only the word "works" with an unrelated
#: case was reported as covered — the exact false negative this gate exists to
#: prevent, since an unflagged gap is invisible to the human reviewer.
_GENERIC = frozenset(["the", "and", "for", "that", "this", "with", "from", "into", "when", "then", "shall", "must", "should", "will", "can", "able", "allow", "allows", "user", "users", "system", "application", "app", "page", "screen", "test", "tests", "case", "cases", "verify", "verifies", "validate", "validates", "check", "checks", "ensure", "ensures", "work", "works", "working", "display", "displays", "show", "shows", "correct", "correctly", "successful", "successfully", "valid", "invalid", "data", "value", "values", "field", "fields", "button", "click"])


def _tokens(text: str) -> set:
    """Content words only.

    Length >= 3, not >= 4: the domain's most discriminating tokens are often
    short — "PDF", "API", "SSO", "log". Dropping them made a criterion about
    exporting to PDF indistinguishable from one about anything else, while
    keeping "works", which distinguishes nothing.
    """
    return {w for w in re.findall(r"[a-z0-9]+", str(text or "").casefold())
            if len(w) >= 3 and w not in _GENERIC}


#: Fraction of a criterion's content words that must appear somewhere in the
#: sheet for it to count as covered.
_COVERAGE_THRESHOLD = 0.5


def _lexically_covered(criterion: str, case_blob: str) -> bool:
    """Is this criterion plausibly addressed by some case in the sheet?

    Matching is by SUBSTRING rather than by whole token, which is what lets
    "user can log in" match a case titled "Login works": `log` occurs inside
    `login`. Whole-token matching missed every one of these compound-word
    pairs, and they are extremely common in acceptance criteria — "sign in" /
    "signin", "log out" / "logout", "drop down" / "dropdown".

    Deliberately biased toward FALSE POSITIVES (an extra warning) over false
    negatives (a silently uncovered requirement). The gate is advisory and a
    human reads its output; an unnecessary warning costs them a glance, while
    a missed gap costs an untested requirement in production.
    """
    tokens = _tokens(criterion)
    if not tokens:
        return True  # nothing discriminating to check — do not cry wolf
    hits = sum(1 for token in tokens if token in case_blob)
    return hits / len(tokens) >= _COVERAGE_THRESHOLD


def check(test_cases: list[Any], acceptance_criteria: Any = None, *,
          strict: bool = False) -> VerifyResult:
    cases = [c for c in (test_cases or []) if isinstance(c, dict)]
    errors: list[str] = []
    warnings: list[str] = []
    repairs: list[str] = []

    if not cases:
        return VerifyResult.failed(
            "coverage_check", ["the sheet contains no test cases"],
            repairs=["Produce at least one test case for this story."])

    # -- duplicate ids: a hard failure, always ----------------------------
    # Case ids are the join key for the entire pipeline. Two rows sharing one
    # id means the Planner, the Coding Agent and the Self-Healer each pick a
    # different row and act on it, and nothing downstream can detect that.
    seen: dict[Any, int] = {}
    for i, case in enumerate(cases):
        cid = case.get("test_case_id", "")
        if cid in seen:
            errors.append(f"duplicate test_case_id {cid!r} (rows {seen[cid] + 1} and {i + 1})")
            repairs.append(f"Give every case a unique test_case_id; {cid!r} appears twice.")
        seen[cid] = i

    # -- near-duplicate titles: advisory ----------------------------------
    titles = [(i, str(c.get("title") or "")) for i, c in enumerate(cases)]
    for a in range(len(titles)):
        for b in range(a + 1, len(titles)):
            if not titles[a][1] or not titles[b][1]:
                continue
            ratio = difflib.SequenceMatcher(None, titles[a][1].casefold(),
                                            titles[b][1].casefold()).ratio()
            if ratio >= _DUPLICATE_THRESHOLD:
                warnings.append(
                    f"cases {cases[titles[a][0]].get('test_case_id')} and "
                    f"{cases[titles[b][0]].get('test_case_id')} have nearly identical "
                    f"titles ({ratio:.0%}) — likely the same case written twice.")

    # -- criteria traceability --------------------------------------------
    criteria = (acceptance_criteria if isinstance(acceptance_criteria, list)
                else split_criteria(acceptance_criteria))
    covered_ac = {str(c.get("acceptance_criterion") or "").strip() for c in cases}
    covered_ac.discard("")

    uncovered = []
    if criteria:
        case_text = " ".join(
            f"{c.get('title', '')} {c.get('expected_result', '')} "
            f"{' '.join(c.get('steps') or [])} {c.get('acceptance_criterion', '')}"
            for c in cases)
        case_blob = case_text.casefold()
        for criterion in criteria:
            if any(difflib.SequenceMatcher(None, criterion.casefold(), ac.casefold()).ratio() > 0.6
                   for ac in covered_ac):
                continue
            if _lexically_covered(criterion, case_blob):
                continue
            uncovered.append(criterion)

        for criterion in uncovered:
            message = (f"no test case appears to cover this acceptance criterion: "
                       f"{criterion[:140]}")
            (errors if strict else warnings).append(message)
            repairs.append(
                f"Add a test case covering this acceptance criterion, and set its "
                f"acceptance_criterion field to it: {criterion[:200]}")

    # -- orphan cases: always advisory ------------------------------------
    if criteria:
        for case in cases:
            if not str(case.get("acceptance_criterion") or "").strip():
                warnings.append(
                    f"{case.get('test_case_id')}: no acceptance_criterion recorded — "
                    f"it cannot be traced back to the story.")

    automated = sum(1 for c in cases if c.get("execution_type") == "Automated")
    meta = {"cases": len(cases), "automated": automated,
            "manual": len(cases) - automated, "criteria": len(criteria),
            "uncovered": len(uncovered)}

    if errors:
        return VerifyResult.failed("coverage_check", errors, repairs=repairs,
                                   warnings=warnings, meta=meta)
    return VerifyResult.passed("coverage_check", warnings=warnings, meta=meta)
