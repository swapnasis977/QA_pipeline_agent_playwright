"""Settling "needs a demonstration" on a re-run, without re-paying for planning.

THE FAILURE THIS EXISTS TO PREVENT. `stage_code` raised DemonstrationRequired
for every element the plan could not ground, and the message told the operator
to record a `playwright codegen` session and "re-run the exact same command".
That could never work: on resume the ingest stage was skipped (so the new
recording was never absorbed into the ledger) and the planner stage was skipped
(so its stale `needs_demonstration.json` was reloaded verbatim). The only
escape was `--force` on two model stages, i.e. paying for everything again.

This re-checks, deterministically and with no model call, the elements that
were unresolved: absorb any recordings now on disk, look each element up again,
write what resolved back onto the plan, and return only what is STILL missing.
"""

from __future__ import annotations

from agents.artifact_io import read_json, write_json
from qa_pipeline.contracts import write_validated
from qa_pipeline.verifiers import locator_check
from utils.run_layout import artifact_path


def _patch_plans(plans, item: dict, card: dict) -> int:
    """Applies `card` to the matching still-ungrounded element of `plans`."""
    patched = 0
    for plan in plans or []:
        if not isinstance(plan, dict) or plan.get("test_case_id") != item.get("test_case_id"):
            continue
        for element in plan.get("elements") or []:
            if (isinstance(element, dict) and not element.get("locator")
                    and element.get("logical_name") == item.get("logical_name")
                    and element.get("screen") == item.get("screen")):
                locator_check.apply_resolved_card(element, card)
                patched += 1
    return patched


def settle_demonstrations(ctx, plan_state: dict, absorb_recordings) -> list:
    """Returns the demonstration items that are still unresolved."""
    unresolved = list(plan_state.get("unresolved") or [])
    needs_file = ctx.artifact("needs_demonstration.json")
    if not unresolved:
        return []

    absorb_recordings(ctx)

    still, resolved = [], 0
    planner_path = artifact_path(ctx.feature_dir, "planner_doc.json")
    planner_doc = read_json(planner_path, default=None)
    plans = plan_state.get("plans") or []
    for item in unresolved:
        card = ctx.ledger.resolve_element(
            item.get("logical_name", ""), item.get("screen", ""), groundable_only=True)
        if card is None:
            still.append({**item, "reason": ctx.ledger.unresolved_reason(
                item.get("logical_name", ""), item.get("screen", ""))})
            continue
        resolved += _patch_plans(plans, item, card) or 1
        if isinstance(planner_doc, list):
            _patch_plans(planner_doc, item, card)

    if resolved:
        ctx.ledger.save()
        write_validated(ctx.artifact("automation_plan.json"), "automation_plan",
                        plans, many=True)
        if isinstance(planner_doc, list):
            write_json(planner_path, planner_doc)
        print(f"      {resolved} previously unresolved element(s) are now grounded "
              f"by the recording(s) — no re-planning needed.")

    plan_state["unresolved"] = still
    if still:
        write_json(needs_file, still)
    elif needs_file.exists():
        needs_file.unlink()
    return still
