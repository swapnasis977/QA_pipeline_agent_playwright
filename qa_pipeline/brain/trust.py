"""Provenance ranking — the trust hierarchy, as code rather than as prose.

`utils/app_design_doc.as_extra_system()` already got this right in miniature.
It wraps the human-written design doc with an explicit instruction: *"If this
doc and a live DOM snapshot given elsewhere in this prompt disagree about the
CURRENT state of a screen, the live snapshot wins."* That is exactly the
correct rule. Its only weakness is that it is a sentence in a prompt, so it
holds only as well as the model chooses to follow it, and only for the two
sources that one sentence happens to mention.

This module generalizes it to every source and moves the decision out of the
model's hands entirely. When two cards assert different things about the same
key, `resolve()` picks the winner in Python. The model is never asked.

THE RULE THAT MATTERS MOST is `can_ground_locator()`. A locator may not enter
generated code unless something OBSERVED it — a crawl, a human recording, or
a passing test. A design doc may tell you a button is called "Save"; only the
live app can tell you it is actually `getByRole('button', {name: 'Save
changes'})`. `utils/locator_verifier.py`'s docstring records what happens
otherwise: a file that compiled cleanly, ran for 30 seconds, and timed out on
a plausible-but-absent locator.

WHAT `recorded` DOES NOT MEAN. It sits at 0.95 — second only to a passing
test — on the reasoning that a person actually clicked the thing. That is
sound, and it is still not a guarantee of correctness. Observed 2026-09-03:
the committed NSWDCJ recording carries an edit-icon locator for the Surname
field (`#editbasicInfoLastname > .editable > .demographicAdditionalInfo`)
that targets the wrong sub-element — clicking it leaves the input `disabled`
and silently does nothing, while the sibling MIN field's recorded locator
(`... > .editable > .edit-section > .icon.sm.edit`) has the correct shape. A
recording captures what the recorder's tooling THOUGHT was clicked, which is
not always the element that produced the effect.

Nothing here re-checks a recorded card once it is trusted, and that is a real
gap rather than an oversight to fix casually: a DOM snapshot covers ~60
elements of one screen, so failure-to-corroborate is weak evidence and making
it a hard error would block legitimate runs. The demotion path that DOES work
is `writeback.from_heal`, which drops a replaced locator to 0.2 — but it only
fires when the Self-Healer actually supersedes the locator at run time. A
wrong recorded locator that a human fixes by hand instead leaves the bad card
sitting at 0.95 forever. If you correct such a locator manually, correct the
ledger card too.
"""

from __future__ import annotations

from qa_pipeline.contracts import PROVENANCE

#: Rank derives from PROVENANCE's ORDER, so adding a source in the right
#: position there is the only edit needed. Higher wins.
_RANK = {name: i for i, name in enumerate(PROVENANCE)}

#: The confidence a freshly-created card gets from each source. A card can
#: rise above its base through corroboration (see `promote`), but never
#: starts above it — an `inferred` card claiming 0.95 is exactly the
#: overconfidence this table exists to overrule.
BASE_CONFIDENCE = {
    "inferred": 0.10,
    "legacy_testcase": 0.35,
    "figma": 0.45,
    "design_doc": 0.55,
    "crawled": 0.85,
    "recorded": 0.95,
    "confirmed": 1.00,
}

#: Sources that actually observed the running application. Only these may
#: supply a locator to generated code.
OBSERVED = {"crawled", "recorded", "confirmed"}

#: Below this, a card is background knowledge only — usable to phrase a test
#: case or name a screen, never to build an assertion or a click on.
GROUNDING_FLOOR = 0.80


def rank(source: str) -> int:
    """Higher is more trustworthy. An unknown source ranks below everything,
    so a card from a future pipeline version can never outrank a confirmed
    observation just because this build doesn't recognise its name."""
    return _RANK.get(source, -1)


def base_confidence(source: str) -> float:
    return BASE_CONFIDENCE.get(source, 0.0)


def is_observed(source: str) -> bool:
    return source in OBSERVED


def can_ground_locator(card: dict) -> bool:
    """True if this card may supply a locator to generated code.

    Both conditions must hold: the source must have observed the real app,
    AND the observation must have been unambiguous. A locator that matched
    two elements is not a usable locator — Playwright's strict mode refuses
    to act on it at RUN time, which is precisely the failure mode
    `locator_verifier` was extended to catch after one ambiguous
    `getByRole('button', {name: /Identification/i})` caused all three
    Executor failures in a single run.
    """
    if not is_observed(card.get("source", "")):
        return False
    if card.get("confidence", 0.0) < GROUNDING_FLOOR:
        return False
    value = card.get("value")
    # noqa SIM103: ruff would fold this into `return not (...)`. Kept as a
    # guard clause so all three rejection reasons — wrong source, too little
    # confidence, ambiguous match — read as the parallel list the docstring
    # describes, rather than two guards and a negated boolean.
    if isinstance(value, dict) and value.get("unique") is False:  # noqa: SIM103
        return False
    return True


def resolve(existing: dict, incoming: dict) -> dict:
    """Picks the winner when two cards claim the same id.

    Order of precedence:
      1. Higher provenance rank wins outright. A crawl beats a design doc
         even if the doc is newer, because the doc describes intent and the
         crawl describes reality.
      2. Same rank -> the more recent observation wins. Two crawls of the
         same element mean the app changed; the newer one is the app now.
      3. Same rank and same timestamp -> higher confidence wins.

    Returns the winning card. The loser is not deleted by this function —
    `store.upsert` records it as `superseded_by` so a wrong promotion stays
    auditable instead of vanishing.
    """
    if existing is None:
        return incoming
    if incoming is None:
        return existing

    # A DISPROVED card ranks below everything. `demote` keeps the original
    # source on purpose, so by source alone a demoted `recorded` card would
    # forever beat a fresh `crawled` observation of the same element.
    if existing.get("disproved") and not incoming.get("disproved"):
        return incoming
    if incoming.get("disproved") and not existing.get("disproved"):
        return existing

    r_new, r_old = rank(incoming.get("source", "")), rank(existing.get("source", ""))
    if r_new != r_old:
        return incoming if r_new > r_old else existing

    t_new = incoming.get("last_verified", "")
    t_old = existing.get("last_verified", "")
    if t_new != t_old:
        return incoming if t_new > t_old else existing

    return (incoming if incoming.get("confidence", 0.0) >= existing.get("confidence", 0.0)
            else existing)


def promote(card: dict, new_source: str, when: str) -> dict:
    """Returns a copy of `card` re-sourced to `new_source`, if that is an
    upgrade. Used by `brain.writeback` when a test that used this locator
    actually passes: the card moves to `confirmed` and stops being re-derived
    on every regeneration.

    Never downgrades. A `confirmed` card touched by a later crawl stays
    confirmed — the crawl corroborates it, and corroboration should not cost
    a card its status.
    """
    if rank(new_source) <= rank(card.get("source", "")):
        refreshed = dict(card)
        refreshed["last_verified"] = when
        return refreshed
    promoted = dict(card)
    promoted["source"] = new_source
    promoted["confidence"] = max(card.get("confidence", 0.0), base_confidence(new_source))
    promoted["last_verified"] = when
    return promoted


#: What a disproved card's confidence drops to. Below GROUNDING_FLOOR by a
#: wide margin, so `can_ground_locator` refuses it immediately, but not zero —
#: the card is still a record that this element EXISTS and was once described
#: this way, which is what `unresolved_reason` needs to say something more
#: useful than "never heard of it".
DISPROVED_CONFIDENCE = 0.15


def demote(card: dict, reason: str, when: str) -> dict:
    """Returns a copy of `card` marked as disproved — the mirror of `promote`.

    THE GAP THIS CLOSES. `promote` moves a card up when a passing test
    corroborates it, and `writeback.from_heal` drops a locator the Self-Healer
    replaced. Between them there was no way to say "a human looked at this and
    it is simply wrong", so a bad card sourced from a RECORDING sat at 0.95
    forever and kept being handed to generated code. That is not hypothetical:
    the committed NSWDCJ recording carries a Surname edit-icon locator that
    targets the wrong sub-element, and nothing in the pipeline could retire it.

    Deliberately keeps the ORIGINAL source rather than rewriting history to
    'inferred'. What happened is "a recording said this and the recording was
    wrong", and that is more useful to the next reader than a card that
    pretends it was never observed. The confidence is what stops it being
    used; `disproved` and `disproved_reason` are what explain why.
    """
    demoted = dict(card)
    demoted["confidence"] = min(card.get("confidence", 0.0), DISPROVED_CONFIDENCE)
    demoted["disproved"] = True
    demoted["disproved_reason"] = reason
    demoted["last_verified"] = when
    return demoted


def decay_note(card: dict) -> str:
    """Explains, in one line, why a card is not usable for grounding. Fed
    into the Explorer's re-crawl request and into decline reasons, so a
    human reading a bug list sees 'the design doc said so, nothing has
    confirmed it' rather than a bare confidence number."""
    source = card.get("source", "unknown")
    if not is_observed(source):
        return (f"source is '{source}' — nothing has observed this on the running "
                f"app, so it cannot ground a locator")
    value = card.get("value")
    if isinstance(value, dict) and value.get("unique") is False:
        return ("observed, but matched more than one element — Playwright strict "
                "mode will refuse to act on it")
    return (f"confidence {card.get('confidence', 0.0):.2f} is below the "
            f"{GROUNDING_FLOOR:.2f} grounding floor")
