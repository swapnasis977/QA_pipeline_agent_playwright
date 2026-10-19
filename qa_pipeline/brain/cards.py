"""Constructors for knowledge cards.

Every card in the ledger is built here rather than assembled inline by its
caller, for one reason: `id` is a natural key, and a natural key that two
call sites format differently is not a key at all. The Explorer writing
`element:LoginPage/Sign in` while the writeback writes `element:loginpage/sign
in` produces two cards for one element, and the ledger silently stops
deduplicating.

Ids are normalized (case-folded, whitespace-collapsed) so the same element
observed by a crawl, a recording and a passing test lands on ONE card that
gets promoted, instead of three that compete.
"""

from __future__ import annotations

import hashlib
import re

from qa_pipeline.contracts import ELEMENT_VALUE, KNOWLEDGE_CARD, SCREEN_VALUE
from utils.timestamps import utc_iso

from . import trust


def _slug(text: str) -> str:
    """Normalizes one id segment. Keeps spaces (they read better in a file a
    human will open) but collapses runs of them, and case-folds so
    'Sign In' and 'sign in' are the same key."""
    return re.sub(r"\s+", " ", str(text or "").strip()).casefold()


#: Everything the `knowledge_card.id` pattern does NOT allow. Real UI labels
#: are full of it — "Save & Close", "Delete (permanent)", "Continue?", an
#: em dash, a non-Latin script — and `nav_edge` ids are built with a literal
#: "->", which is not in the allowed set either. Without this substitution
#: `_card`'s own `KNOWLEDGE_CARD.check` raised on the way in, so a crawl of
#: any screen with a navigation path, or with one ampersanded button, killed
#: the whole stage rather than recording what it had seen.
_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.\-/ ]+")


def _id_safe(segment: str) -> str:
    """One '/'-delimited segment, reduced to the characters the id pattern
    allows. Applied on WRITE and on lookup alike (both go through `make_id`),
    so two spellings differing only in stripped punctuation land on one card
    rather than silently missing each other.

    A segment that survives as nothing — a label of pure punctuation, or one
    written entirely in a non-Latin script — becomes a stable digest of its
    ORIGINAL text rather than an empty string. Empty would make every such
    element on a screen share the id 'element:login/', quietly merging
    unrelated facts into one card; the digest keeps them distinct and still
    dedupes each against itself.
    """
    raw = str(segment or "")
    cleaned = re.sub(r"[-_ ]{2,}", "-", _ID_UNSAFE.sub("-", raw)).strip("- ")
    if cleaned:
        return cleaned
    if not raw.strip():
        return ""
    return "x-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def make_id(card_type: str, *parts) -> str:
    segments = []
    for part in parts:
        if not str(part or "").strip():
            continue
        # Split on '/' FIRST: callers pass composite keys ("Login/Sign in"),
        # and sanitizing the whole string at once let one unrepresentable
        # half collapse into the delimiter of the other.
        for piece in _slug(part).split("/"):
            safe = _id_safe(piece)
            if safe:
                segments.append(safe)
    if not segments:
        segments = ["x-" + hashlib.sha1(
            "/".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:12]]
    return f"{card_type}:" + "/".join(segments)


def _card(card_type: str, key: str, value, source: str, *,
          confidence: float = None, evidence: str = "", scope: str = "app",
          when: str = None) -> dict:
    when = when or utc_iso()
    card = {
        "id": make_id(card_type, key),
        "type": card_type,
        "key": _slug(key),
        "value": value,
        "source": source,
        "confidence": (trust.base_confidence(source) if confidence is None
                       else max(0.0, min(1.0, float(confidence)))),
        "last_verified": when,
        "scope": scope,
        "evidence": evidence,
        "superseded_by": "",
    }
    KNOWLEDGE_CARD.check(card)
    return card


def element(screen: str, logical_name: str, locator: str, source: str, *,
            role: str = "", accessible_name: str = "", unique: bool = True,
            confidence: float = None, evidence: str = "", when: str = None) -> dict:
    """An interactive element and the locator that reaches it.

    `unique=False` is not a cosmetic flag — `trust.can_ground_locator` refuses
    to let a non-unique element into generated code at all, because
    Playwright's strict mode will refuse to act on it at run time anyway. It
    is far cheaper to know that here than after a 30-second timeout.
    """
    value = ELEMENT_VALUE.coerce({
        "screen": _slug(screen),
        "logical_name": logical_name,
        "locator": locator,
        "role": role,
        "accessible_name": accessible_name,
        "unique": bool(unique),
    })
    ELEMENT_VALUE.check(value)
    return _card("element", f"{screen}/{logical_name}", value, source,
                 confidence=confidence, evidence=evidence, when=when)


def screen(name: str, source: str, *, route: str = "", purpose: str = "",
           reached_by: list = None, confidence: float = None,
           evidence: str = "", when: str = None) -> dict:
    value = SCREEN_VALUE.coerce({
        "name": name,
        "route": route,
        "purpose": purpose,
        "reached_by": list(reached_by or []),
    })
    SCREEN_VALUE.check(value)
    return _card("screen", name, value, source, confidence=confidence,
                 evidence=evidence, when=when)


def nav_edge(from_screen: str, to_screen: str, action: str, source: str, *,
             confidence: float = None, evidence: str = "", when: str = None) -> dict:
    """One edge of the navigation graph.

    The graph is what lets the Explorer reach a screen deterministically on a
    later run instead of re-deriving the click path by keyword matching —
    the guessing that `utils/route_map.py` exists to sidestep, and that left
    DOM inspection stranded on the dashboard when no word from the test case
    appeared anywhere in the real UI.
    """
    value = {"from": _slug(from_screen), "to": _slug(to_screen), "action": action}
    return _card("nav_edge", f"{from_screen}->{to_screen}", value, source,
                 confidence=confidence, evidence=evidence, when=when)


def glossary(tester_word: str, ui_label: str, source: str, *,
             confidence: float = None, evidence: str = "", when: str = None) -> dict:
    """Bridges the vocabulary gap that no amount of prompting fixes on its
    own: the sheet says "tile", the app renders "module button"; the case
    says "Face Comparison Tool admin", the app calls it "Image Editing Tool"."""
    value = {"tester_word": tester_word, "ui_label": ui_label}
    return _card("glossary", tester_word, value, source,
                 confidence=confidence, evidence=evidence, when=when)


def rule(name: str, statement: str, source: str, *, confidence: float = None,
         evidence: str = "", scope: str = "app", when: str = None) -> dict:
    # `name` is carried in the value as well as the key. The key is
    # case-folded so lookups dedupe ("Password policy" and "password policy"
    # are one rule), but the prompt block must show the human's original
    # capitalisation — a retrieved fact rendered as "fingerprint scanner"
    # when the document said "Fingerprint Scanner" reads as machine noise and
    # is harder to match against the app's own wording.
    return _card("rule", name, {"name": name, "statement": statement}, source,
                 confidence=confidence, evidence=evidence, scope=scope, when=when)


def env_quirk(name: str, statement: str, source: str, *, confidence: float = None,
              evidence: str = "", when: str = None) -> dict:
    """A standing fact about THIS environment — a mocked device, a flaky
    third-party redirect, a feature disabled in QA. Read by the Reviewer so
    a known environmental limitation is classified as such instead of being
    written up as a product bug."""
    return _card("env_quirk", name, {"name": name, "statement": statement}, source,
                 confidence=confidence, evidence=evidence, when=when)


def test_data(name: str, payload, source: str, *, confidence: float = None,
              evidence: str = "", scope: str = "app", when: str = None) -> dict:
    return _card("test_data", name, payload, source, confidence=confidence,
                 evidence=evidence, scope=scope, when=when)


#: Dispatch for `brain.ingest`, which receives model-proposed cards as loose
#: dicts and has to turn them into real ones without trusting the model to
#: have formatted an id.
BUILDERS = {
    "element": element,
    "screen": screen,
    "nav_edge": nav_edge,
    "glossary": glossary,
    "rule": rule,
    "env_quirk": env_quirk,
    "test_data": test_data,
}
