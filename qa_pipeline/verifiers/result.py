"""The shared result type every verifier returns.

Three outcomes, not two. `ok` and "not ok" are insufficient because a
verifier can also be UNABLE TO CHECK — no TypeScript toolchain on PATH, no
DOM snapshot for this screen, no acceptance criteria in the story. The
existing `coding_agent._check_syntax_status` already learned this the hard
way and split `_SYNTAX_SKIPPED` out from pass/fail, with the note that
"a missing/broken toolchain means this check is SKIPPED, not that generated
code fails closed."

Collapsing skipped into pass tells you a file was verified when it was not.
Collapsing it into fail stops the pipeline over a missing optional tool.
So it is its own state, and the orchestrator decides per-gate whether an
unverifiable artifact may proceed.

`repairs` is what separates this from a plain assertion. A verifier that can
say WHAT is wrong and HOW to fix it turns a failure into one more model call
with a precise instruction, instead of a decline. `locator_check` is the
best example: when a locator near-misses a real element, the repair carries
the element's true accessible name, so the fix needs no further inspection.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

PASS = "pass"
FAIL = "fail"
SKIPPED = "skipped"


class VerifyResult:
    """Immutable-ish outcome of one verifier over one artifact."""

    __slots__ = ("name", "status", "errors", "warnings", "repairs", "meta")

    def __init__(self, name: str, status: str = PASS, errors: Iterable[str] | None = None,
                 warnings: Iterable[str] | None = None, repairs: Iterable[str] | None = None,
                 meta: dict[str, Any] | None = None) -> None:
        self.name = name
        self.status = status
        self.errors = list(errors or [])
        self.warnings = list(warnings or [])
        self.repairs = list(repairs or [])
        self.meta = dict(meta or {})

    # -- constructors ------------------------------------------------------

    @classmethod
    def passed(cls, name: str, warnings: Iterable[str] | None = None,
               meta: dict[str, Any] | None = None) -> VerifyResult:
        return cls(name, PASS, warnings=warnings, meta=meta)

    @classmethod
    def failed(cls, name: str, errors: Iterable[str], repairs: Iterable[str] | None = None,
               meta: dict[str, Any] | None = None,
               warnings: Iterable[str] | None = None) -> VerifyResult:
        return cls(name, FAIL, errors=errors, repairs=repairs, meta=meta, warnings=warnings)

    @classmethod
    def skipped(cls, name: str, reason: str,
                meta: dict[str, Any] | None = None) -> VerifyResult:
        """`reason` lands in warnings so it always reaches the console. A
        silently skipped gate is indistinguishable from a passing one, which
        is how an unverified artifact reaches production believing it was
        checked."""
        return cls(name, SKIPPED, warnings=[f"{name} could not run: {reason}"], meta=meta)

    # -- predicates --------------------------------------------------------

    @property
    def ok(self) -> bool:
        """SKIPPED counts as ok. A gate that could not run must not block the
        pipeline on its own — the orchestrator escalates that decision via
        `require()` where an unverified artifact is genuinely unacceptable."""
        return self.status in (PASS, SKIPPED)

    @property
    def verified(self) -> bool:
        """Strictly passed. Use this, not `ok`, when the answer to 'was this
        actually checked?' matters."""
        return self.status == PASS

    @property
    def repairable(self) -> bool:
        return self.status == FAIL and bool(self.repairs)

    # -- output ------------------------------------------------------------

    def repair_prompt(self) -> str:
        """The failure phrased as an instruction back to the producing agent."""
        if not self.repairs:
            return ""
        return ("The following problems were found in your previous output by an "
                "automated check. Fix EXACTLY these and return the corrected output "
                "only:\n  - " + "\n  - ".join(self.repairs))

    def summary(self) -> str:
        if self.status == PASS:
            extra = f" ({len(self.warnings)} warning(s))" if self.warnings else ""
            return f"{self.name}: pass{extra}"
        if self.status == SKIPPED:
            return f"{self.name}: skipped — {self.warnings[0] if self.warnings else 'no reason given'}"
        head = self.errors[0] if self.errors else "no detail"
        more = f" (+{len(self.errors) - 1} more)" if len(self.errors) > 1 else ""
        return f"{self.name}: FAIL — {head}{more}"

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "errors": self.errors,
                "warnings": self.warnings, "repairs": self.repairs, "meta": self.meta}

    def __repr__(self) -> str:
        return f"<VerifyResult {self.summary()}>"


def combine(name: str, results: Iterable[VerifyResult | None]) -> VerifyResult:
    """Folds several verifier results into one.

    FAIL dominates; otherwise SKIPPED dominates PASS — because a batch in
    which one gate could not run has not been fully verified, and reporting
    it as a clean pass is the exact lie this module exists to prevent.
    """
    kept = [r for r in results if r is not None]
    if not kept:
        return VerifyResult.passed(name)

    errors: list[str] = []
    warnings: list[str] = []
    repairs: list[str] = []
    meta: dict[str, Any] = {}
    for i, r in enumerate(kept):
        errors.extend(r.errors)
        warnings.extend(r.warnings)
        repairs.extend(r.repairs)
        # Keyed by name AND position. Combining a per-file or per-case batch
        # means every result carries the SAME name ("gate_code" forty times),
        # so keying by name alone kept only the last one — and with it, only
        # the last file's locator counts. That is what made
        # `metrics.absorb_verify` and `note_crawls` report zero locators and
        # zero crawls on every run, which in turn left `budgets.max_crawls_
        # _per_run` unenforceable.
        key = r.name if r.name not in meta else f"{r.name}[{i}]"
        meta[key] = r.meta or {}

    if any(r.status == FAIL for r in kept):
        status = FAIL
    elif any(r.status == SKIPPED for r in kept):
        status = SKIPPED
    else:
        status = PASS

    return VerifyResult(name, status, errors=errors, warnings=warnings,
                        repairs=repairs, meta=meta)
