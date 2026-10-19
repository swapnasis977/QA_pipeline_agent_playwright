"""Relevance selection — hands an agent the cards that matter, not the corpus.

This is the module that keeps the brain from becoming the thing it was
supposed to replace. The naive design ("load all the documents into every
agent's prompt") fails three ways at once: token cost multiplies across seven
model stages and every case; contradictions between a stale document and a
live observation get resolved by whichever model happens to be running; and
the signal an agent actually needs is buried under everything it does not.

So retrieval is scored and bounded. An agent asks for what it is about to do
("write code for TC-014, which is about the overlay blend slider on the Face
Comparison screen") and gets back a few dozen cards, rendered as a compact
block with each fact's provenance attached.

Scoring is lexical overlap, not embeddings — see `store.py` for why. The
weights below encode which card types matter to which stage, because the Test
Case Writer needs glossary and rules while the Coding Agent needs elements
and navigation, and giving each of them the other's cards is pure noise.
"""

from __future__ import annotations

import re

from . import trust

#: Per-agent card-type weighting. A weight of 0 excludes the type entirely.
#: These are the "what does this stage actually need" judgments; getting them
#: wrong shows up as an agent with a full prompt and no useful information.
AGENT_WEIGHTS = {
    "testcase_writer": {
        "glossary": 3.0, "screen": 2.5, "rule": 3.0, "env_quirk": 2.5,
        "test_data": 1.5, "nav_edge": 1.0, "element": 0.5,
    },
    "automation_planner": {
        "screen": 3.0, "nav_edge": 3.0, "element": 2.5, "glossary": 2.0,
        # `rule` sat at 0.5 — the floor — on the theory that the Planner
        # routes and the rules are someone else's problem. But the Planner is
        # exactly the stage that decides WHICH element performs which step,
        # and the rules that matter most to that decision are the ones saying
        # one control is another: "after a non-compliant image the same
        # #startCapture button is relabelled to act as Recapture; no separate
        # Recapture button exists". At 0.5 that rule could not outscore the
        # 37 element cards competing for the block, so the Planner grounded
        # the recapture step on a recorded "Next Capture button" that lives on
        # a LATER screen, and the run timed out there. At 2.0 the rule lands
        # and costs two element lines out of thirty-seven.
        "env_quirk": 1.5, "test_data": 1.0, "rule": 2.0,
    },
    "coding_agent": {
        "element": 3.5, "nav_edge": 2.5, "screen": 2.0, "glossary": 1.5,
        # Raised from 0.5 for the same reason as `automation_planner` above:
        # the Coding Agent writes the branch, so a rule saying which control
        # performs a step decides what it writes. With `rule` at the floor,
        # "the same #startCapture button is relabelled to act as Recapture"
        # never reached the prompt, and the generated recapture branch clicked
        # a control that only exists on a later screen.
        "test_data": 1.5, "env_quirk": 1.0, "rule": 2.0,
    },
    "reviewer": {
        "env_quirk": 3.5, "rule": 2.5, "element": 1.5, "screen": 1.0,
        "glossary": 1.0, "nav_edge": 0.5, "test_data": 0.5,
    },
    "self_healer": {
        "element": 3.5, "nav_edge": 2.0, "screen": 1.5, "env_quirk": 1.5,
        "glossary": 1.0, "rule": 0.5, "test_data": 0.5,
    },
    "app_explorer": {
        "screen": 3.0, "nav_edge": 3.0, "glossary": 2.0, "element": 1.0,
        "env_quirk": 1.0, "rule": 0.5, "test_data": 0.5,
    },
}

#: Fallback when an agent has no explicit profile — everything, evenly.
_DEFAULT_WEIGHTS = {t: 1.0 for t in
                    ("glossary", "screen", "nav_edge", "element", "rule",
                     "env_quirk", "test_data")}

#: Hard ceiling on the rendered block. `coding_agent` already bounds
#: POM_SUMMARY.md at 8000 chars before sending it; the brain block sits
#: alongside that in the same prompt, so it gets a comparable budget rather
#: than being allowed to crowd it out.
DEFAULT_CHAR_BUDGET = 6000

_WORD = re.compile(r"[a-z0-9]+")

#: Words that appear in nearly every test case and therefore separate nothing.
_STOP = frozenset(["a", "an", "the", "and", "or", "of", "to", "in", "on", "at", "for", "with", "from", "by", "is", "are", "be", "as", "it", "its", "this", "that", "test", "case", "verify", "validate", "check", "ensure", "user", "should", "when", "then", "given", "click", "open", "select", "enter", "view", "page", "screen", "button", "field", "value", "data"])


def _tokens(text: str) -> set:
    return {w for w in _WORD.findall(str(text or "").casefold())
            if len(w) > 2 and w not in _STOP}


def _card_text(card: dict) -> str:
    """Everything about a card that is worth matching against."""
    value = card.get("value")
    parts = [card.get("key", "")]
    if isinstance(value, dict):
        parts.extend(str(v) for v in value.values() if isinstance(v, (str, int, float)))
        for v in value.values():
            if isinstance(v, list):
                parts.extend(str(x) for x in v)
    elif value is not None:
        parts.append(str(value))
    return " ".join(parts)


def score(card: dict, query_tokens: set, weights: dict) -> float:
    """Lexical overlap, weighted by card type and nudged by trust.

    The trust nudge is small on purpose. It breaks ties toward observed facts
    without letting a high-confidence but irrelevant card outrank a low-
    confidence but exactly-on-topic one — the ledger's job here is to be
    relevant first and trustworthy second, since `can_ground_locator` is what
    actually enforces trust downstream.
    """
    type_weight = weights.get(card.get("type", ""), 0.0)
    if type_weight <= 0:
        return 0.0
    overlap = query_tokens & _tokens(_card_text(card))
    if not overlap:
        return 0.0
    return type_weight * len(overlap) * (1.0 + 0.15 * trust.rank(card.get("source", "")) / 6.0)


def select(ledger, query: str, agent: str = "", limit: int = 40,
           screen: str = "", always_include_types=("env_quirk",)) -> list:
    """The cards this agent should see for this task, best first.

    `always_include_types` is not a convenience — an environment quirk ("the
    fingerprint scanner is mocked in QA") is exactly the fact that shares no
    vocabulary with the test case it invalidates, so lexical scoring will
    never surface it. Those types are appended regardless of overlap. Getting
    this wrong means the Reviewer writes up a mocked device as a product bug.
    """
    weights = AGENT_WEIGHTS.get(agent, _DEFAULT_WEIGHTS)
    query_tokens = _tokens(query)
    if screen:
        query_tokens |= _tokens(screen)

    scored = []
    for card in ledger.cards:
        s = score(card, query_tokens, weights)
        if s > 0:
            scored.append((s, card))
    scored.sort(key=lambda pair: (-pair[0], pair[1].get("id", "")))
    chosen = [c for _, c in scored[:limit]]

    chosen_ids = {c.get("id") for c in chosen}
    for card_type in always_include_types or ():
        if weights.get(card_type, 0) <= 0:
            continue
        for card in ledger.by_type(card_type):
            if card.get("id") not in chosen_ids:
                chosen.append(card)
                chosen_ids.add(card.get("id"))
    return chosen


def render(cards: list, char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """Formats cards for a prompt, grouped by type, with provenance inline.

    Provenance is rendered on every line and is not decoration: it is what
    lets the model know that an element line is a fact it must use verbatim
    while a design-doc line is background it may be overruled on. The same
    distinction `app_design_doc.as_extra_system` makes in prose, made
    per-fact.
    """
    if not cards:
        return ""

    order = ["glossary", "screen", "nav_edge", "element", "rule", "env_quirk", "test_data"]
    grouped = {}
    for card in cards:
        grouped.setdefault(card.get("type", "other"), []).append(card)

    headings = {
        "glossary": "VOCABULARY (what testers call it -> what the UI actually says)",
        "screen": "SCREENS",
        "nav_edge": "NAVIGATION (how to reach a screen)",
        "element": "ELEMENTS (verified locators — use these VERBATIM, do not invent variants)",
        "rule": "BUSINESS RULES",
        "env_quirk": "ENVIRONMENT QUIRKS (true of this test environment right now)",
        "test_data": "TEST DATA",
    }

    out, used = [], 0
    for card_type in order + [t for t in grouped if t not in order]:
        bucket = grouped.get(card_type)
        if not bucket:
            continue
        section = [f"\n## {headings.get(card_type, card_type.upper())}"]
        for card in bucket:
            line = f"  - {_render_one(card)}"
            if used + len(line) > char_budget:
                section.append(f"  - ...{len(bucket)} more omitted for length.")
                break
            section.append(line)
            used += len(line)
        out.append("\n".join(section))
        if used >= char_budget:
            break

    return ("APPLICATION KNOWLEDGE (from the ledger; each fact carries its source. "
            "A fact sourced 'confirmed', 'recorded' or 'crawled' was OBSERVED on the "
            "real application and must be used exactly as written. A fact sourced "
            "'design_doc', 'figma' or 'legacy_testcase' is background that a live "
            "observation overrules.)\n" + "\n".join(out))


def _render_one(card: dict) -> str:
    value = card.get("value") or {}
    src = card.get("source", "?")
    t = card.get("type")

    if t == "element" and isinstance(value, dict):
        bits = [f"{value.get('screen', '?')} / {value.get('logical_name', '?')}",
                f"-> {value.get('locator', '?')}"]
        if value.get("unique") is False:
            bits.append("[AMBIGUOUS — matched >1 element; do NOT use]")
        return " ".join(bits) + f"  ({src})"
    if t == "screen" and isinstance(value, dict):
        route = f" route={value['route']}" if value.get("route") else ""
        reach = (" reached by: " + " > ".join(value["reached_by"])) if value.get("reached_by") else ""
        return f"{value.get('name', card.get('key'))}{route}{reach}  ({src})"
    if t == "nav_edge" and isinstance(value, dict):
        return (f"{value.get('from', '?')} --[{value.get('action', '?')}]--> "
                f"{value.get('to', '?')}  ({src})")
    if t == "glossary" and isinstance(value, dict):
        return (f"\"{value.get('tester_word', '?')}\" = \"{value.get('ui_label', '?')}\" "
                f"in the UI  ({src})")
    if t in ("rule", "env_quirk") and isinstance(value, dict):
        # `value["name"]` preserves the human's original capitalisation;
        # `key` is case-folded for deduplication and reads as machine noise.
        name = value.get("name") or card.get("key", "?")
        return f"{name}: {value.get('statement', '')}  ({src})"
    return f"{card.get('key', '?')}: {value}  ({src})"


def for_agent(ledger, agent: str, query: str, screen: str = "",
              limit: int = 40, char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """The one call a stage makes: query in, prompt block out."""
    return render(select(ledger, query, agent=agent, limit=limit, screen=screen),
                  char_budget=char_budget)
