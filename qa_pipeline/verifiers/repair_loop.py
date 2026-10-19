"""propose -> verify -> repair -> verify, until it passes or the budget runs out.

This is the control structure the whole architecture rests on, and it is
deliberately tiny: everything interesting lives in the `produce` and `verify`
callables the caller supplies. Keeping it generic means every stage gets the
same recovery behaviour without each one growing its own bespoke retry code —
which is what the old pipeline had, in three different shapes, in three
different agents.

Why a loop rather than one shot plus a decline: the failures the verifiers
catch are overwhelmingly MECHANICAL and repairable — a missing contract
field, a locator that near-misses a real element's name, a file written to
the wrong directory, a compile error. Handing the model a precise diagnosis
and asking again fixes most of them on the first retry. Declining immediately
would send work to a human that no human needed to do.

Why a budget rather than looping until success: the failures that are NOT
mechanical do not improve with repetition, they just cost more. Two repair
attempts is the shape the existing pipeline already converged on
(`MAX_HEAL_ATTEMPTS = 2`), and self-healing's own convergence metrics were
added precisely because unbounded retrying is the failure mode to fear here.

DECLINE SHORT-CIRCUITS. If the producer declines, the loop stops immediately
rather than spending its remaining budget re-asking a question the model has
already refused. `run_pipeline` learned this: on a decline it breaks out of
the attempt loop with the note that retrying "burns another LLM call
re-asking the same question, and is exactly the 'self-healer loops without
converging' pattern this decline signal exists to avoid."
"""

from __future__ import annotations

from .result import combine

#: One initial attempt plus this many repairs. Matches MAX_HEAL_ATTEMPTS.
DEFAULT_MAX_REPAIRS = 2

#: How many individual problems the console names per repair round. A gate
#: batch over forty files can produce hundreds; the point is to make the
#: recurring ones visible, not to reprint the whole result.
_MAX_LOGGED_PROBLEMS = 12


class RepairOutcome:
    """What the loop achieved, and at what cost.

    `attempts` and `declined` are what the orchestrator's metrics read, so
    "the pipeline is generating well" and "the pipeline is repairing its way
    to adequacy" stay distinguishable. Those look identical in a pass rate
    and completely different here.
    """

    __slots__ = ("artifact", "result", "attempts", "declined", "decline_reason", "history")

    def __init__(self, artifact, result, attempts, declined=False,
                 decline_reason="", history=None):
        self.artifact = artifact
        self.result = result
        self.attempts = attempts
        self.declined = declined
        self.decline_reason = decline_reason
        self.history = list(history or [])

    @property
    def ok(self) -> bool:
        return not self.declined and self.result is not None and self.result.ok

    @property
    def verified(self) -> bool:
        return not self.declined and self.result is not None and self.result.verified

    def summary(self) -> str:
        if self.declined:
            return f"declined after {self.attempts} attempt(s): {self.decline_reason}"
        if self.result is None:
            return f"no result after {self.attempts} attempt(s)"
        word = "first try" if self.attempts == 1 else f"{self.attempts} attempts"
        return f"{self.result.summary()} ({word})"


def _is_declined(artifact) -> tuple:
    """Reads the decline protocol off whatever shape the artifact has —
    a single dict, or a list of them. A list counts as declined only when
    EVERY item declined; a partial decline is a normal partial result the
    caller filters, not a reason to abandon the stage."""
    if isinstance(artifact, dict):
        if artifact.get("declined"):
            return True, str(artifact.get("decline_reason") or "(no reason given)")
        return False, ""
    if isinstance(artifact, list) and artifact:
        declined = [a for a in artifact if isinstance(a, dict) and a.get("declined")]
        if len(declined) == len(artifact):
            reasons = "; ".join(str(a.get("decline_reason") or "?") for a in declined[:3])
            return True, reasons
    return False, ""


def _same_findings(before, after) -> bool:
    """Two verify results carry the same errors, in any order.

    Compared as a SET of error strings rather than by count: a repair that
    swaps one error for a different one has changed something and may still
    converge, while one that returns the identical set has demonstrably not
    acted on the instruction. Order is ignored because gates aggregate from
    several checks whose order is not guaranteed stable.
    """
    if before is None or after is None:
        return False
    return set(getattr(before, "errors", []) or []) == set(getattr(after, "errors", []) or [])


def _is_empty_generation(result) -> bool:
    return any(isinstance(m, dict) and m.get("empty_generation")
               for m in (getattr(result, "meta", None) or {}).values())


def _score(result) -> tuple:
    """How good an attempt is. Lower is better, so it can be compared directly.

    Ranked (passing < failing < produced-nothing, then fewest errors). Error
    count alone let a generation that produced NO file (one error: "no spec was
    produced") outrank a real attempt with three locator problems, because one
    is fewer than three — and the empty list was then returned as "best".
    """
    if result is None:
        return (3, 0)
    if result.ok:
        return (0, len(result.errors or []))
    if _is_empty_generation(result):
        return (2, len(result.errors or []))
    return (1, len(result.errors or []))


def run(produce, verify, *, max_repairs: int = DEFAULT_MAX_REPAIRS,
        label: str = "stage", verbose: bool = True, should_stop=None) -> RepairOutcome:
    """Drives one stage to a verified artifact.

    `produce(repair_prompt)` is called with None on the first attempt and
    with the verifier's repair text on each retry. `verify(artifact)` returns
    a `VerifyResult` or a list of them.

    An exception from `produce` is NOT caught here. Transient-error retry and
    the human retry prompt are the orchestrator's job (`orchestrator.runner`),
    and catching exceptions here would swallow a rate limit as if it were a
    contract violation — repairing a prompt that never reached the provider.

    `should_stop()` (optional) returns a non-empty reason when no further
    model call should be paid for (a spent token budget). It is consulted
    before each REPAIR attempt only, and ends the loop with the best attempt.
    """
    history = []
    artifact = None
    result = None
    #: The best attempt seen so far, which is NOT necessarily the last one.
    #: A later attempt can come back worse than an earlier one, and returning
    #: it discards a better artifact the loop already paid for — leaving the
    #: stage with less than not retrying at all. Observed: generation attempt
    #: 2 produced an accepted spec, attempt 3 produced nothing, and attempt 3
    #: was returned, so the run failed downstream holding an empty file list.
    #: Same rule `coding_agent._repair_unverified_locators` already applies to
    #: its own inner pass: a repair is kept only where it demonstrably helps.
    best_artifact, best_result, best_score = None, None, None

    for attempt in range(1, max_repairs + 2):
        repair_prompt = None
        if attempt > 1 and result is not None:
            if should_stop is not None:
                why = should_stop()
                if why:
                    if verbose:
                        print(f"      {label}: {why} — not paying for another repair attempt.")
                    break
            # The BEST attempt's diagnosis, not the latest's: the producer
            # edits the best attempt's files (see stage_code), so showing it
            # a regressed attempt's diagnosis next to them described code it
            # was not looking at.
            diagnosed = best_result if best_result is not None else result
            repair_prompt = diagnosed.repair_prompt()
            if not repair_prompt:
                # Nothing actionable to say. Another identical call would
                # produce another identical failure.
                if verbose:
                    print(f"      {label}: failed with no repairable diagnosis — "
                          f"not retrying.")
                break
            if verbose:
                print(f"      {label}: repair attempt {attempt - 1}/{max_repairs} — "
                      f"{len(diagnosed.errors)} problem(s) to fix.")
                # NAME THEM. The count alone made every repair round opaque:
                # a stage that repairs on every single run looks identical in
                # the log to one that repairs occasionally, and neither the
                # console nor any artifact recorded WHAT the gate objected to
                # — so the recurring defect that costs a full extra
                # generation every run could not be found, let alone fixed.
                for problem in diagnosed.errors[:_MAX_LOGGED_PROBLEMS]:
                    print(f"         · {problem}")
                if len(diagnosed.errors) > _MAX_LOGGED_PROBLEMS:
                    print(f"         · ... and {len(diagnosed.errors) - _MAX_LOGGED_PROBLEMS} more "
                          f"(full list in the stage's repair history)")

        artifact = produce(repair_prompt)

        declined, reason = _is_declined(artifact)
        if declined:
            if verbose:
                print(f"      {label}: declined — {reason}")
            if best_artifact is not None:
                # A repair attempt declining must not discard the earlier
                # attempt already paid for: keep it, and note the decline.
                if verbose:
                    print(f"      {label}: keeping the best earlier attempt.")
                return RepairOutcome(best_artifact, best_result, attempt,
                                     decline_reason=f"repair declined: {reason}",
                                     history=history)
            return RepairOutcome(artifact, result, attempt, declined=True,
                                 decline_reason=reason, history=history)

        raw = verify(artifact)
        result = combine(label, raw) if isinstance(raw, list) else raw
        previous = history[-1] if history else None
        history.append(result)

        # Ranked on (did it pass, how many problems) — fewest wins, and an
        # equal score does NOT displace the earlier attempt, since no
        # improvement is no reason to swap.
        score = _score(result)
        if best_score is None or score < best_score:
            best_artifact, best_result, best_score = artifact, result, score

        if result is None or result.ok:
            if verbose and attempt > 1:
                print(f"      {label}: repaired successfully on attempt {attempt}.")
            return RepairOutcome(artifact, result, attempt, history=history)

        if not result.repairable:
            if verbose:
                print(f"      {label}: {result.summary()} — no repair instruction "
                      f"available, giving up.")
            break

        # NO PROGRESS -> STOP. A repair that comes back with exactly the same
        # errors it was asked to fix has told us the instruction is not one
        # this attempt can act on; asking a third time buys a third identical
        # failure at full price. Observed: the Coding Agent spent 77k tokens
        # across three generation cycles on one case, every attempt rejected
        # for the same reason, and wrote no file at all — two of those three
        # cycles were knowably wasted the moment the second one came back
        # unchanged.
        if previous is not None and _same_findings(previous, result):
            if verbose:
                print(f"      {label}: attempt {attempt} reproduced the same "
                      f"{len(result.errors)} problem(s) as the previous one — the "
                      f"repair instruction is not landing, so stopping instead of "
                      f"paying for another identical attempt.")
            break

    # The BEST attempt, not the last. See best_artifact above.
    if best_artifact is not None:
        artifact, result = best_artifact, best_result
    return RepairOutcome(artifact, result, len(history) or 1, history=history)


def run_per_item(items, produce_one, verify_one, *,
                 max_repairs: int = DEFAULT_MAX_REPAIRS, label: str = "item",
                 key=None, verbose: bool = True, should_stop=None) -> dict:
    """Applies the loop to each item independently.

    Per-item rather than per-batch on purpose: one malformed case out of
    forty should cost one repair call for that case, not a regeneration of
    the whole sheet. Batch repair also tends to perturb the items that were
    already correct, which is how a repair pass makes things worse.

    Returns `{key: RepairOutcome}` plus an "_summary" tally.
    """
    outcomes, tally = {}, {"ok": 0, "failed": 0, "declined": 0, "repaired": 0}

    for item in items or []:
        item_key = key(item) if callable(key) else str(
            (item.get("test_case_id") or item.get("path") or id(item))
            if isinstance(item, dict) else item)

        outcome = run(
            lambda repair, _i=item: produce_one(_i, repair),
            lambda artifact, _i=item: verify_one(_i, artifact),
            max_repairs=max_repairs, label=f"{label} {item_key}", verbose=verbose,
            should_stop=should_stop)

        outcomes[item_key] = outcome
        if outcome.declined:
            tally["declined"] += 1
        elif outcome.ok:
            tally["ok"] += 1
            if outcome.attempts > 1:
                tally["repaired"] += 1
        else:
            tally["failed"] += 1

    outcomes["_summary"] = tally
    return outcomes
