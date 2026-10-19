"""Shaping what one story's Test Case Writer prompt carries: no duplicated acceptance criteria, no unrelated design sections, compact JSON.

Pure functions (no I/O, no model), so each is directly testable.
"""

from __future__ import annotations

import json
import re

_WORD_RE = re.compile(r"[a-z0-9]{4,}")

#: Words common to nearly every story AND every design section; matching on them
#: would keep everything.
_STOPWORDS = frozenset({
    "with", "that", "this", "from", "when", "then", "should", "shall", "have", "will",
    "user", "users", "page", "screen", "verify", "given", "able", "must", "also", "into",
    "their", "there", "which", "where", "while", "after", "before", "each", "being",
})


def _words(text: str) -> set:
    return set(_WORD_RE.findall(text.lower())) - _STOPWORDS


def compact_json(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False)


def story_for_prompt(jira_data: dict) -> dict:
    """The story as the model sees it.

    When acceptance_criteria is just the description repeated (the fallback for
    a story with no AC section) it is omitted from this COPY — the description
    already carries it. `jira_data` itself is untouched: the coverage gate and
    the ledger query still read the full list."""
    story = dict(jira_data)
    criteria = story.get("acceptance_criteria")
    description = (story.get("description") or "").strip()
    if isinstance(criteria, list) and (
            not criteria or (len(criteria) == 1 and str(criteria[0]).strip() == description)):
        story.pop("acceptance_criteria", None)
    return story


def story_with_hierarchy(jira_data: dict, hierarchy: dict, story_key: str) -> dict:
    """`jira_data` plus the shared hierarchy, minus an ancestor that IS this
    story (a --feature-link pointing at the story itself would otherwise list it
    twice: once as the story, once as its own ancestor)."""
    if not hierarchy:
        return jira_data
    own = {story_key, jira_data.get("story_key")} - {None, ""}
    ancestors = [a for a in hierarchy.get("ancestors", []) if a.get("key") not in own]
    return {**jira_data, "hierarchy": {**hierarchy, "ancestors": ancestors}}


def relevant_design_data(figma_data: dict, jira_data: dict) -> dict:
    """The design payload with `sections` narrowed to those that share
    vocabulary with the story's title / acceptance criteria.

    The full payload used to be sent with every story and the model told to
    ignore unrelated sections — input paid for N-1 times over. When NOTHING
    matches (a story phrased differently from the design) the full set is kept:
    dropping every section would be worse than sending too many."""
    sections = figma_data.get("sections") if isinstance(figma_data, dict) else None
    if not isinstance(sections, list) or len(sections) < 2:
        return figma_data
    story_text = " ".join([
        str(jira_data.get("title") or ""),
        " ".join(str(c) for c in (jira_data.get("acceptance_criteria") or [])),
    ])
    story_words = _words(story_text)
    if not story_words:
        return figma_data
    kept = [s for s in sections if story_words & _words(json.dumps(s, default=str))]
    if not kept or len(kept) == len(sections):
        return figma_data
    return {**figma_data, "sections": kept}
