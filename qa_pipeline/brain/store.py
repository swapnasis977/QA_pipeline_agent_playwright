"""The ledger — a flat-file store of knowledge cards.

Deliberately NOT a vector database. The original design doc rejected one and
was right to: this corpus is one application's screens, elements and rules —
hundreds of facts, occasionally a few thousand. At that size an exact keyed
lookup beats embedding similarity on every axis that matters here. It is
faster, it is free, it is deterministic across runs, and above all it is
debuggable: when an agent believes something wrong, you open `ledger.json`,
search for the id, and read the `source` and `evidence` fields that put it
there. No embedding store gives you that.

The whole file is loaded into memory on first access and written back
atomically through `artifact_io`. A ledger large enough for that to hurt is
a ledger that has stopped being one application's knowledge, which is a
design problem, not a storage one.

CONFLICTS. `upsert` never silently overwrites. It asks `trust.resolve` which
card wins, and when the incoming card loses it is recorded as superseded
rather than dropped — so a locator that was confirmed, then wrongly
"corrected" by a low-confidence crawl of the wrong screen, leaves a trail.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from agents.artifact_io import read_json, write_json
from utils.timestamps import utc_iso

from . import trust
from .cards import _slug, make_id

#: Filename inside the brain directory. One file, human-readable, diffable —
#: a reviewer should be able to read the ledger in a pull request.
LEDGER_FILENAME = "ledger.json"

#: A superseded card is kept, but only the most recent few per id — otherwise
#: an app under active development grows an unbounded audit trail inside the
#: file every agent loads on every run.
_MAX_SUPERSEDED_PER_ID = 3

#: Fuzzy-match floor for resolving a test case's wording against an element's
#: logical name. Matches the threshold `run_pipeline._cluster_groups_by_reason`
#: already uses for the same kind of "is this the same thing described
#: differently" judgment, so the pipeline has one similarity notion, not two.
_FUZZY_THRESHOLD = 0.7


#: Words too common in UI labels to prove two names mean the same element.
#: Without these, a plan asking for "Cancel button" matched any card whose
#: label merely contained "button".
_GENERIC_LABEL_WORDS = frozenset(["button", "link", "icon", "field", "box", "text", "the", "and", "for", "this", "that", "with", "from", "into", "click", "clicks", "press", "enter", "select", "selected", "page", "screen", "list", "item", "menu", "submenu", "label", "input", "value", "please", "your", "here", "required", "optional"])


#: ARIA roles that describe a passive annotation rather than a control.
#: A card with one of these can be a perfectly accurate observation and still
#: never be the thing a test step means to act on.
#:
#: `resolve_element` scores candidates on NAME SIMILARITY ALONE, which lets a
#: passive card win purely by being better-named. Observed here: the plan
#: asked for the capture device option "4000 (GLB Member R100)" and the ledger
#: answered with `#tooltip164575` — a crawled dashboard TOOLTIP whose text is
#: "GLB Member R100", an almost exact string match — in preference to the
#: recorded `[id="4000"]` option, whose logical name is merely "4000". The
#: tooltip is real; it is just not a control, and clicking it can never select
#: a device.
_PASSIVE_ROLES = frozenset({"tooltip"})


def _is_passive_role(card: dict) -> bool:
    role = ((card.get("value") or {}).get("role") or "").strip().lower()
    return role in _PASSIVE_ROLES


def _content_words(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]+", text or "")
            if len(w) >= 2 and w not in _GENERIC_LABEL_WORDS}


def _contains_whole_word(haystack: str, needle: str) -> bool:
    """`needle` appears in `haystack` bounded by non-alphanumerics.

    The distinction that matters: "register" IS inside "register/update" (bounded
    by "/") but is NOT inside "registration" (the word continues). A plain
    substring test cannot tell those apart, and conflating them let one card
    win against three unrelated elements — see `similarity`.
    """
    if not needle or not haystack:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                     haystack) is not None


def similarity(wanted: str, name: str) -> float:
    """How strongly a requested element name matches a card's stored name.

    Plain `SequenceMatcher` is not enough here, and a real run showed exactly
    why: a crawl stores an element's full ACCESSIBLE NAME, which in a properly
    labelled app is a whole sentence —

        "Start Capture button. Click to capture the Face and Iris of the candidate"

    while a plan sensibly calls that element "Start Capture button". Those are
    the same button by any human reading, but the character-ratio is ~0.40
    because the strings differ so much in LENGTH, so every well-labelled
    element in the ledger failed to resolve and the run reported 61 unresolved
    elements against a ledger that already held every one of them.

    So containment is checked first: when everything distinctive in the
    requested name also appears in the stored name, that is a match regardless
    of how much extra description the label carries. Generic UI words are
    excluded from "distinctive" so "Cancel button" cannot match on "button"
    alone.
    """
    wanted, name = (wanted or "").strip(), (name or "").strip()
    if not wanted or not name:
        return 0.0
    if wanted == name:
        return 1.0

    ratio = difflib.SequenceMatcher(None, wanted, name).ratio()

    wanted_words, name_words = _content_words(wanted), _content_words(name)

    # A ONE-WORD REQUEST IS NOT EVIDENCE AGAINST A LONG LABEL.
    #
    # Containment exists for the case above: a short request naming the same
    # element a verbose label describes. It is not evidence when the request
    # is a single word and the label is a whole region's concatenated text —
    # there, the word is simply one of many things inside, and matching on it
    # identifies nothing.
    #
    # This laundered an invented locator straight through the hard gate. A
    # generated `getByRole('button', { name: /recapture/i })` — carrying its
    # own TODO admitting no inspection had reached that screen — "verified"
    # against a stored card named "Non compliant image. 1 attempts remaining.
    # Recapture Reconnect Reset", because "recapture" appears inside it. Two
    # unrelated elements, two unrelated locator strategies, reported as
    # grounded. The whole promise of the gate is that this cannot happen.
    #
    # A short label stays permissive, so "Register" still matches "Register/Update".
    #
    # MULTI-WORD PHRASES ARE EXEMPT, and that exemption matters. The rule
    # below counts DISTINCTIVE words, so "Submit button" is "one word"
    # ("button" being generic) and was capped at ~0.42 against its own real
    # label, "Submit button to create and submit the registration" — under the
    # 0.7 floor, so the Submit button could not resolve to itself. That gap
    # is half of why a Planner run reached for an unrelated card instead:
    # there was no legitimate candidate left to win. A request of two or more
    # raw words appearing contiguously in the label is a phrase match, not a
    # word-buried-in-a-blob match, so it skips this guard and is scored by
    # the whole-word containment check below. The "recapture" case the guard
    # exists for is a single raw word, so it still hits the guard.
    _raw_word_count = len(re.findall(r"[a-z0-9]+", wanted))
    _phrase_request = _raw_word_count >= 2 and _contains_whole_word(name, wanted)
    _one_word_request = len(wanted_words) <= 1
    _long_label = len(name_words) > 3
    if _one_word_request and _long_label and not _phrase_request:
        overlap = (len(wanted_words & name_words) / len(wanted_words)
                   if wanted_words else 0.0)
        # Scored on character ratio alone, which for a word buried in a
        # sentence is correctly low — the request falls through to
        # `unresolved_reason` and the gate declines instead of inventing.
        return max(ratio, overlap * 0.4)

    # Whole requested phrase appears verbatim inside the stored label —
    # but only as a WHOLE WORD, never as a fragment inside a longer one.
    #
    # A plain `in` test here was a real, observed defect. The card named
    # "register" is a character-substring of a plan asking for "submit button to
    # create and submit the registration" (it sits inside "registration"), so it
    # scored 0.95 against an element it has nothing to do with. In one run
    # that single bug collapsed THREE distinct elements — the Submit button,
    # the success modal, and its Continue button — onto the one "Register" card,
    # and the Planner emitted `getByText('Register')` as the locator for all
    # three. Word-boundary matching keeps the case this rule exists for
    # ("register" still matches "register/update", since "/" is a boundary) while
    # refusing the fragment match that produced that collision.
    if _contains_whole_word(name, wanted) or _contains_whole_word(wanted, name):
        return max(ratio, 0.95)

    if wanted_words and name_words:
        # Every distinctive word of the request is present in the label.
        if wanted_words <= name_words:
            return max(ratio, 0.9)
        overlap = len(wanted_words & name_words) / len(wanted_words)
        if overlap >= 0.75:
            return max(ratio, 0.75 + 0.15 * overlap)
        ratio = max(ratio, overlap * 0.7)

    return ratio


class Ledger:
    """Load-once, mutate-in-memory, save-explicitly.

    Not a context manager on purpose: a stage that crashes mid-way must not
    persist a half-built view of the app. The orchestrator saves only after a
    stage's verifier has passed.
    """

    def __init__(self, brain_dir):
        self.dir = Path(brain_dir)
        self.path = self.dir / LEDGER_FILENAME
        self._cards = None
        self._index = {}
        self._superseded = []
        self._dirty = False

    # -- loading -----------------------------------------------------------

    def load(self) -> Ledger:
        if self._cards is not None:
            return self
        raw = read_json(self.path, default={}) or {}
        # Tolerates the older shape (a bare list) so a ledger written before
        # the superseded trail existed still loads instead of being discarded.
        if isinstance(raw, list):
            cards, superseded = raw, []
        else:
            cards = raw.get("cards", [])
            superseded = raw.get("superseded", [])
        self._cards = [c for c in cards if isinstance(c, dict) and c.get("id")]
        self._superseded = [c for c in superseded if isinstance(c, dict)]
        self._index = {c["id"]: c for c in self._cards}
        self._dirty = False
        return self

    @property
    def cards(self) -> list:
        self.load()
        return self._cards

    def save(self) -> Path:
        self.load()
        if not self._dirty:
            return self.path
        write_json(self.path, {
            "contract_version": 1,
            "updated": utc_iso(),
            "count": len(self._cards),
            "cards": sorted(self._cards, key=lambda c: (c.get("type", ""), c.get("id", ""))),
            "superseded": self._superseded[-(_MAX_SUPERSEDED_PER_ID * 64):],
        })
        self._dirty = False
        return self.path

    # -- writing -----------------------------------------------------------

    def upsert(self, card: dict) -> dict:
        """Adds or updates one card, resolving conflicts by provenance.

        Returns the card that is now authoritative for that id — which may
        be the EXISTING one when the incoming card lost. Callers that assume
        their own card won will otherwise report a locator they did not
        actually install.
        """
        self.load()
        card_id = card.get("id")
        if not card_id:
            raise ValueError("Card has no id — build it through brain.cards, not by hand.")

        existing = self._index.get(card_id)
        winner = trust.resolve(existing, card)

        if existing is not None and winner is not existing:
            loser = dict(existing)
            loser["superseded_by"] = winner.get("source", "")
            loser["superseded_at"] = utc_iso()
            self._superseded.append(loser)
            self._superseded = self._superseded[-(_MAX_SUPERSEDED_PER_ID * 64):]

        if existing is None:
            self._cards.append(winner)
        elif winner is not existing:
            self._cards[self._cards.index(existing)] = winner
        else:
            return existing  # incoming lost; nothing changed, nothing dirty

        self._index[card_id] = winner
        self._dirty = True
        return winner

    def upsert_many(self, cards) -> dict:
        """Bulk upsert. Returns a small tally the caller can print, because
        "142 cards ingested" is far less useful to a human than knowing 9
        were rejected in favour of what was already known."""
        tally = {"added": 0, "updated": 0, "kept_existing": 0}
        for card in cards or []:
            before = self._index.get(card.get("id"))
            winner = self.upsert(card)
            if before is None:
                tally["added"] += 1
            elif winner is before:
                tally["kept_existing"] += 1
            else:
                tally["updated"] += 1
        return tally

    def promote(self, card_id: str, source: str) -> dict:
        """Raises a card's provenance — used by `writeback` when a test that
        depends on this card actually passes."""
        self.load()
        existing = self._index.get(card_id)
        if existing is None:
            return None
        promoted = trust.promote(existing, source, utc_iso())
        if promoted != existing:
            self._cards[self._cards.index(existing)] = promoted
            self._index[card_id] = promoted
            self._dirty = True
        return promoted

    # -- reading -----------------------------------------------------------

    def demote(self, card_id: str, reason: str) -> dict:
        """Marks a card disproved so it stops grounding generated code.

        The counterpart to `promote`. Needed because the only existing
        downgrade path runs through the Self-Healer replacing a locator at run
        time — so a card a HUMAN discovers is wrong (and fixes by hand in the
        page object) stayed at full confidence and kept being handed back out.
        """
        self.load()
        existing = self._index.get(card_id)
        if existing is None:
            return None
        demoted = trust.demote(existing, reason, utc_iso())
        self._cards[self._cards.index(existing)] = demoted
        self._index[card_id] = demoted
        self._dirty = True
        return demoted

    def get(self, card_id: str) -> dict:
        self.load()
        return self._index.get(card_id)

    def by_type(self, card_type: str) -> list:
        return [c for c in self.cards if c.get("type") == card_type]

    def screens(self) -> list:
        return sorted(c["value"].get("name", c["key"]) for c in self.by_type("screen")
                       if isinstance(c.get("value"), dict))

    def screen_matching(self, fingerprint, exclude: str = "",
                        restrict_to=None) -> str:
        """The screen already recorded with this exact element set, if any.

        `explore.crawl` uses it to refuse a snapshot that reached nowhere: in
        a single-page app the URL is identical on every screen, so a repeated
        element set is the only evidence that the inspector never left the
        page it was already on.

        `restrict_to` limits the comparison to screens the caller actually
        DECLARED. Without it, the ledger's screen names are whatever any
        caller ever wrote — and `explore.discover` mints one per targeted
        re-crawl from whatever free text the Planner put in a step's
        `screen` field. This project's own ledger accumulated "capture" and
        "dashboard or registration" that way, each holding a partial snapshot of
        the dashboard. A phantom like that is permanent evidence: the next
        honest crawl of the real Dashboard matched "capture" and was refused
        as "never left that screen", and no amount of re-running could clear
        it. Only a declared screen is a screen.
        """
        if not fingerprint:
            return ""
        allowed = None
        if restrict_to is not None:
            allowed = {str(n).strip().lower() for n in restrict_to if n}
        by_screen = {}
        for card in self.by_type("element"):
            value = card.get("value") or {}
            name = value.get("screen", "")
            if not name or name.strip().lower() == (exclude or "").strip().lower():
                continue
            if allowed is not None and name.strip().lower() not in allowed:
                continue
            by_screen.setdefault(name, set()).add(
                (value.get("logical_name", ""), value.get("locator", "")))
        for name, elements in by_screen.items():
            if elements == set(fingerprint):
                return name
        return ""

    def glossary_map(self) -> dict:
        """tester word -> real UI label, for prompt injection."""
        out = {}
        for c in self.by_type("glossary"):
            v = c.get("value") or {}
            if v.get("tester_word") and v.get("ui_label"):
                out[v["tester_word"]] = v["ui_label"]
        return out

    def find_element(self, screen: str, logical_name: str) -> dict:
        """Exact keyed lookup. The common path once a project has been
        running for a week."""
        return self.get(make_id("element", f"{screen}/{logical_name}"))

    def resolve_element(self, logical_name: str, screen: str = "",
                        groundable_only: bool = True) -> dict:
        """Finds the element card a plan step means.

        Exact first, then fuzzy within the same screen, then fuzzy across all
        screens. `groundable_only` (the default) filters to cards that may
        legally supply a locator — see `trust.can_ground_locator`. A caller
        that wants to know "does the ledger know about this at all, even
        un-groundably?" passes False, which is how the Explorer decides
        whether it is CONFIRMING something or DISCOVERING it.
        """
        self.load()
        candidates = self.by_type("element")
        if groundable_only:
            candidates = [c for c in candidates
                          if trust.can_ground_locator(c)
                          and not _is_passive_role(c)]
        if not candidates:
            return None

        if screen:
            exact = self.find_element(screen, logical_name)
            if exact is not None and (not groundable_only or trust.can_ground_locator(exact)):
                return exact

        wanted = _slug(logical_name)

        def names(cards):
            return {c["id"]: _slug((c.get("value") or {}).get("logical_name", "")) for c in cards}

        scoped = [c for c in candidates
                  if not screen or _slug((c.get("value") or {}).get("screen", "")) == _slug(screen)]
        for pool in (scoped, candidates):
            if not pool:
                continue
            best, best_score = None, 0.0
            for card_id, name in names(pool).items():
                if not name:
                    continue
                score = similarity(wanted, name)
                if score > best_score:
                    best, best_score = self._index.get(card_id), score
            if best is not None and best_score >= _FUZZY_THRESHOLD:
                return best
        return None

    def unresolved_reason(self, logical_name: str, screen: str = "") -> str:
        """Why `resolve_element` returned nothing — phrased for a human and
        for the Explorer's re-crawl request. Distinguishes 'never heard of
        it' from 'heard of it, but from a source that cannot ground code',
        which are completely different problems with completely different
        fixes."""
        known = self.resolve_element(logical_name, screen, groundable_only=False)
        if known is None:
            return (f"no card in the ledger describes an element named "
                    f"{logical_name!r}{f' on {screen}' if screen else ''}")
        return (f"{logical_name!r} is known, but not usable: "
                f"{trust.decay_note(known)}")

    # -- reporting ---------------------------------------------------------

    def stats(self) -> dict:
        self.load()
        by_type, by_source = {}, {}
        groundable = 0
        for c in self._cards:
            by_type[c.get("type", "?")] = by_type.get(c.get("type", "?"), 0) + 1
            by_source[c.get("source", "?")] = by_source.get(c.get("source", "?"), 0) + 1
            if c.get("type") == "element" and trust.can_ground_locator(c):
                groundable += 1
        return {
            "total": len(self._cards),
            "by_type": by_type,
            "by_source": by_source,
            "groundable_elements": groundable,
            "superseded": len(self._superseded),
        }


def open_ledger(brain_dir) -> Ledger:
    return Ledger(brain_dir).load()
