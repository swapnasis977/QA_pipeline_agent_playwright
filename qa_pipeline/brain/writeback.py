"""Promotes what actually passed into confirmed ledger knowledge.

This is the stage that makes the pipeline compound instead of decay. Without
it, a locator that a run proved correct is forgotten the moment the run ends,
and the next regeneration re-derives it from scratch — sometimes landing on a
different, worse answer and silently reverting a fix that had already been
found and verified.

`run_pipeline._record_pom_confirmed()` already understood this and appended
confirmed locators to POM_SUMMARY.md as prose. That worked, with two limits
this module removes: the text file is bounded (it is sent to the model in
full on every generation call, so it cannot grow), and prose cannot be
queried — the Coding Agent could only ever read the whole thing or nothing.
Cards can be retrieved selectively and promoted individually.

WHAT COUNTS AS CONFIRMATION. Only a locator that was in a file whose tests
actually PASSED. Not a file that compiled; not a locator the verifier
corroborated against a snapshot; not a patch that was applied. A green run is
the only evidence that survives contact with the real application, which is
why `heal_outcome.verified` is a separate field from `patch_applied`.
"""

from __future__ import annotations

from utils import locator_verifier
from utils.timestamps import utc_iso

from . import cards as card_builders
from . import trust


def _same_locator(stored, passed) -> bool:
    """Whitespace-normalised equality, ignoring a leading `page.`."""
    def norm(value):
        text = "".join(str(value or "").split())
        return text[5:] if text.startswith("page.") else text
    return bool(norm(passed)) and norm(stored) == norm(passed)


def _passing_files(run_result: dict) -> set:
    """Files where every case that ran passed.

    Per-FILE rather than per-case because a locator lives in a file, and a
    file with one failing case may well have failed on the very locator we
    would otherwise be about to promote.
    """
    per_file = {}
    for case in run_result.get("cases", []) or []:
        path = case.get("file") or ""
        if not path:
            continue
        per_file.setdefault(path, []).append(case.get("status"))

    # Fall back to the whole scoped batch when per-case detail is absent —
    # the same condition run_pipeline used: zero unexpected, at least one
    # expected. An empty suite must never count as a pass.
    if not per_file:
        stats = run_result.get("stats") or {}
        if stats.get("unexpected", 0) == 0 and stats.get("expected", 0) > 0:
            return set(run_result.get("test_files") or [])
        return set()

    return {path for path, statuses in per_file.items()
            if statuses and all(s == "passed" for s in statuses)}


def _is_passing(path: str, passing: set) -> bool:
    """Whether `path` is one of the passing files, tolerating path roots.

    Playwright's JSON report names a spec relative to `testDir`
    ("e2e/a.spec.ts"); the generated file is recorded repo-relative
    ("tests/e2e/a.spec.ts"). Compared exactly, nothing ever matched, so no
    locator was ever promoted to `confirmed` and the ledger never learned from a
    green run.
    """
    wanted = path.replace("\\", "/").lstrip("/")
    for candidate in passing:
        c = str(candidate).replace("\\", "/").lstrip("/")
        if c == wanted or wanted.endswith("/" + c) or c.endswith("/" + wanted):
            return True
    return False


def from_run(ledger, run_result: dict, generated_files: list,
             plans: list = None, when: str = None) -> dict:
    """Promotes every locator used by a passing file to `confirmed`.

    Returns a tally for the console. Never raises: a writeback failure must
    not fail a run that otherwise succeeded — the knowledge is an
    optimisation for next time, not part of this run's result.
    """
    when = when or utc_iso()
    tally = {"promoted": 0, "recorded_new": 0, "files_confirmed": 0, "skipped_failing": 0}

    passing = _passing_files(run_result)
    by_case_screen = _screen_by_case(plans or [])

    for gen in generated_files or []:
        path = str(gen.get("path") or "").replace("\\", "/").lstrip("/")
        if not path:
            continue
        if not _is_passing(path, passing):
            tally["skipped_failing"] += 1
            continue
        tally["files_confirmed"] += 1

        screen = ""
        for case_id in gen.get("test_case_ids") or []:
            screen = by_case_screen.get(case_id) or screen

        for locator in _locators_of(gen):
            name = locator.get("name") or locator.get("selector") or ""
            if not name:
                continue
            raw_locator = locator.get("raw", "")
            existing = ledger.resolve_element(name, screen, groundable_only=False)
            # PROMOTE ONLY A CARD THAT ALREADY CARRIES THE LOCATOR THAT PASSED.
            # `promote` changes source/confidence, never the stored locator, so
            # promoting a fuzzy-matched design_doc card (empty or guessed
            # locator) or a disproved one would mint a 1.0 `confirmed` card whose
            # locator is not what the green test used. Anything else gets a new
            # confirmed card carrying the real locator.
            if (existing is not None and not existing.get("disproved")
                    and trust.is_observed(existing.get("source", ""))
                    and _same_locator((existing.get("value") or {}).get("locator"),
                                      raw_locator)):
                if ledger.promote(existing["id"], "confirmed") is not None:
                    tally["promoted"] += 1
                continue
            if not raw_locator:
                continue
            ledger.upsert(card_builders.element(
                screen=screen or "unknown",
                logical_name=name,
                locator=locator.get("raw", ""),
                source="confirmed",
                role=locator.get("role", ""),
                accessible_name=name,
                unique=True,
                evidence=f"passed in {path}",
                when=when,
            ))
            tally["recorded_new"] += 1

    return tally


def _locators_of(generated_file: dict) -> list:
    """Prefers the locators the Coding Agent declared; falls back to parsing
    the file content with the existing extractor. The declared list is more
    reliable (it comes from the agent that wrote the code) but the fallback
    matters for files written before that contract field existed."""
    declared = generated_file.get("locators_used") or []
    if declared:
        parsed = []
        for raw in declared:
            found = locator_verifier.extract_locators(str(raw))
            parsed.extend(found or [{"raw": str(raw), "name": "", "kind": "unknown"}])
        return parsed
    return locator_verifier.extract_locators(generated_file.get("content", "") or "")


def _screen_by_case(plans: list) -> dict:
    """Which screen each case's elements live on, taken from the plan. Lets a
    newly recorded element card be filed under a real screen instead of
    'unknown', which is what makes it findable again next run."""
    out = {}
    for plan in plans or []:
        case_id = plan.get("test_case_id")
        if not case_id:
            continue
        for element in plan.get("elements") or []:
            if element.get("screen"):
                out[case_id] = element["screen"]
                break
    return out


def _card_by_locator(ledger, locator: str):
    """The element card that carries this exact locator.

    Keyed on the LOCATOR, which is the only field that can identify it.
    `resolve_element` matches on `logical_name`, so passing a selector to it
    was a category error: it searched element NAMES for a selector string. It
    happened to work when the selector embedded the element's name — true of
    `getByRole('button', { name: 'Save' })`, never true of `#btnContinue` or
    an nth-child path — and silently found nothing otherwise, so a locator a
    heal had just DISPROVED kept its confidence forever.
    """
    if not locator:
        return None
    wanted = " ".join(str(locator).split())
    for card in ledger.by_type("element"):
        current = (card.get("value") or {}).get("locator") or ""
        if " ".join(str(current).split()) == wanted:
            return card
    return None


def from_heal(ledger, heal_outcomes: list, when: str = None) -> dict:
    """Records the locator swaps that self-healing VERIFIED.

    A heal that was applied but never re-run green is deliberately ignored.
    Promoting an unverified patch would teach the ledger a locator that may
    be just as wrong as the one it replaced, and the ledger's whole value is
    that everything in it at `confirmed` has survived a real execution.

    A verified heal proves two things at once, and both are recorded against
    the SAME element: the old locator is wrong (demoted, superseded_by), and
    that element is now reached by the new one (upserted at `confirmed`,
    keeping its real name and screen).

    It used to invent a brand-new card for the fix with
    `logical_name = <the locator string>` and `screen = "unknown"`, at the
    top trust tier — a card no plan could ever ask for, since no plan names
    an element "page.getByRole(...)". That path had never executed here (no
    heal on this project has ever passed its re-run), so the pollution had
    not started yet; it would have begun on the first success.
    """
    when = when or utc_iso()
    tally = {"confirmed": 0, "superseded": 0, "ignored_unverified": 0,
             "unattributed": 0}

    for outcome in heal_outcomes or []:
        if not outcome.get("verified"):
            if outcome.get("patch_applied"):
                tally["ignored_unverified"] += 1
            continue

        old, new = outcome.get("old_locator", ""), outcome.get("new_locator", "")
        if not new:
            continue

        stale = _card_by_locator(ledger, old)
        if stale is None:
            # Nothing in the ledger carried the old locator, so there is no
            # element to attribute the new one to. Learning nothing beats
            # inventing a stub at `confirmed` that will outrank real cards.
            tally["unattributed"] += 1
            continue

        stale_value = stale.get("value") or {}

        # UPDATED IN PLACE, not demoted-and-replaced. A card's id derives from
        # screen+logical_name, so a separate "confirmed" card for the same
        # element computes the same id and silently overwrites the demoted one
        # — there is no way to hold both, and no use for a twin nothing can
        # address. One element, one card: it now carries the locator the
        # re-run proved, at `confirmed`, and the disproved selector is out of
        # circulation. `superseded` keeps the audit trail.
        ledger.upsert(card_builders.element(
            screen=stale_value.get("screen") or "unknown",
            logical_name=stale_value.get("logical_name") or "",
            locator=new,
            source="confirmed",
            accessible_name=stale_value.get("accessible_name")
            or stale_value.get("logical_name") or "",
            evidence=f"self-heal verified for {outcome.get('test_case_id', '?')}",
            when=when,
        ))
        healed = ledger.get(stale.get("id")) if hasattr(ledger, "get") else None
        if healed is not None:
            healed["superseded"] = old
            ledger.upsert(healed)
        tally["superseded"] += 1
        tally["confirmed"] += 1

    return tally


def summarize(tallies: dict) -> str:
    """One console line. Phrased so a human can tell whether the brain is
    actually learning or just being written to."""
    parts = []
    for label, tally in tallies.items():
        if not tally:
            continue
        bits = ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in tally.items() if v)
        if bits:
            parts.append(f"{label}: {bits}")
    return "; ".join(parts) or "nothing new confirmed this run"
