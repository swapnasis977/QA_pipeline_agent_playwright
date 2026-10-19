"""Contract validation as a verifier.

Thin by design — `contracts` already does the work. What this adds is the
`VerifyResult` shape (so the orchestrator treats every gate identically) and,
more importantly, the translation from validation errors into REPAIR
instructions the producing agent can act on.

That translation is the difference between a pipeline that fails and one that
recovers. "automation_plan[3].target_file: required field is missing" is a
correct error and a useless instruction. Paired with the contract's own
`describe()` output it becomes a prompt that reliably gets fixed on the next
attempt, which is what `repair_loop` then drives.
"""

from __future__ import annotations

from qa_pipeline.contracts import resolve, validate_many

from .result import VerifyResult


def check(schema, data, *, many: bool = False, label: str = "") -> VerifyResult:
    s = resolve(schema)
    errors = validate_many(s, data) if many else s.validate(data, s.name)
    label = label or s.name

    if not errors:
        count = len(data) if many and isinstance(data, list) else 1
        return VerifyResult.passed("schema_check", meta={"contract": s.name, "items": count})

    return VerifyResult.failed(
        "schema_check",
        [f"{label}: {e}" for e in errors],
        repairs=[_repair_prompt(s, errors)],
        meta={"contract": s.name, "violations": len(errors)},
    )


def _repair_prompt(schema, errors: list) -> str:
    """Pairs the specific violations with the contract itself.

    Sending the shape alongside the errors matters: a model told only "field
    X is missing" often invents a plausible neighbouring field instead, while
    a model shown the full contract fixes the actual field. Both halves come
    from the SAME `Schema` object, so the shape the model is asked for and the
    shape that is enforced can never drift apart.
    """
    return (
        f"Your output did not satisfy the '{schema.name}' contract. "
        f"Fix EXACTLY these problems and return corrected JSON only:\n  - "
        + "\n  - ".join(errors)
        + f"\n\nThe full required shape is:\n{schema.describe()}"
    )


def check_declines(items: list, *, label: str = "output") -> VerifyResult:
    """Enforces the decline protocol: `declined: true` requires a reason.

    A decline with no reason is worse than no decline at all. It stops the
    case without telling anyone what to supply, so it cannot be escalated to
    a crawl, cannot be turned into a demonstration request, and lands in the
    bug list as a bare case id — exactly the outcome
    `run_pipeline._merge_self_heal_declined` was added to prevent.
    """
    errors, repairs = [], []
    declined = 0
    for i, item in enumerate(items or []):
        if not isinstance(item, dict) or not item.get("declined"):
            continue
        declined += 1
        if not str(item.get("decline_reason") or "").strip():
            ident = item.get("test_case_id") or item.get("path") or f"index {i}"
            errors.append(f"{label}[{i}] ({ident}): declined with no decline_reason.")
            repairs.append(
                f"You marked {ident} as declined but gave no decline_reason. "
                f"State exactly what is missing — which element, on which screen, "
                f"or which piece of information a human would have to supply — so "
                f"it can be crawled for or demonstrated.")

    if errors:
        return VerifyResult.failed("schema_check", errors, repairs=repairs,
                                   meta={"declined": declined})
    return VerifyResult.passed("schema_check", meta={"declined": declined})
