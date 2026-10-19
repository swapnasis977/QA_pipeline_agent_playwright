"""Code-stage support: repair bookkeeping, restoring the best attempt, merging split specs, and reporting missing test data.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

from agents.artifact_io import read_text_file, write_json
from qa_pipeline import verifiers

#: How many rejection reasons to quote back to the Coding Agent on a retry.
#: Enough to see the pattern, not so many that the real instruction drowns.
_MAX_REJECTIONS_IN_REPAIR = 6


def empty_generation_result(planned_ids: list, rejected: list):
    """The gate verdict for a generation that produced no spec at all.

    `combine("gate_code", [])` returns PASS — with zero files there is nothing
    to verify, so an empty generation read as a clean one. The repair loop
    never engaged, `stage_code` returned `files: []` as a verified artifact,
    and the failure surfaced a stage later at the Executor's "planned but no
    spec" guard, where the only recourse left is failing the whole run.

    Observed: the Coding Agent tried to add a property to a recorded page
    object, the read-only guard rejected that write, the spec still referenced
    the property, failed `tsc`, and was rolled back with its chunk. Two
    rejections, zero files, gate passed, no repair attempted — while every
    reason was already written to coding_agent_rejected_files.json and read by
    nobody. Feeding those reasons back is what makes the retry different from
    the call that just failed.
    """
    ids = ", ".join(str(i) for i in planned_ids[:8]) or "the planned case(s)"
    errors = [f"No spec file was produced for {ids}."]
    repairs = [
        f"Your previous response produced NO usable spec file for {ids}. "
        f"Return a complete, compiling spec for every one of those cases."
    ]
    for entry in (rejected or [])[:_MAX_REJECTIONS_IN_REPAIR]:
        reason = str(entry.get("reason") or "").strip()
        path = entry.get("path") or "(unnamed file)"
        if not reason:
            continue
        errors.append(f"{path}: {reason}")
        repairs.append(f"'{path}' was rejected — {reason}")
    return verifiers.VerifyResult.failed("gate_code", errors, repairs=repairs,
                                         meta={"empty_generation": True})


def _merge_repair(best_files, failing, partial):
    """Combines a targeted repair's output with what was already accepted.

    Returns (files, paths_to_delete, files_to_restore).

    A rejected file is replaced only when a regenerated file covers all of its
    cases. When the model call failed (quota, outage) or skipped a case, the
    rejected file STAYS — an imperfect spec is worth more than none, and a repair
    must never leave the suite smaller than it found it. An old file whose path a
    new file reuses is not deleted: the new one was just written there.
    """
    regenerated = {cid for g in partial for cid in (g.get("test_case_ids") or [])}
    new_paths = {g.get("path") for g in partial}
    merged = {g["path"]: g for g in best_files if g.get("path")}
    delete_paths, restore = [], []
    for generated in failing:
        if set(generated.get("test_case_ids") or []) <= regenerated:
            merged.pop(generated.get("path"), None)
            if generated.get("path") not in new_paths:
                delete_paths.append(generated.get("path"))
        else:
            restore.append(generated)
    merged.update({g["path"]: g for g in partial if g.get("path")})
    return list(merged.values()), delete_paths, restore


def _failing_scope(files, results):
    """Which generated files a gate rejected, and the cases they serve.

    Returns (failing_files, case_ids), or None when a targeted repair is not
    possible: no per-file verdicts, a failing file that is not a spec (a shared
    helper cannot be mapped to cases), or a failing spec with no case ids.
    The caller then regenerates everything, as it always did.

    The repair loop regenerated EVERY case when ANY file failed its gate, so one
    bad spec out of fifteen cost a second full pass of 16 model calls. Targeting
    just the failing files turns that into a call or two.
    """
    if not files or not results or len(results) != len(files):
        return None
    failing = [f for f, r in zip(files, results, strict=True) if r is not None and not r.ok]
    if not failing:
        return None
    ids = set()
    for generated in failing:
        if generated.get("kind", "spec") != "spec" or not generated.get("test_case_ids"):
            return None
        ids.update(generated["test_case_ids"])
    return failing, sorted(ids)


def _merge_split_specs(ctx, files):
    """Puts a feature's split spec files back together (see agents/spec_merge.py)."""
    if not ctx.profile.get("gates.merge_split_specs", True):
        return files
    from agents import spec_merge
    merged_files, report = spec_merge.apply(files, ctx.repo_dir)
    if report["merged"] or report["kept_split"]:
        write_json(ctx.artifact("spec_merge.json"), report)
    for item in report["merged"]:
        print(f"      Merged {len(item['from'])} split spec files into {item['file']}.")
    for item in report["kept_split"]:
        print(f"      Kept {item['file']} split: {item['reason']}")
    return merged_files


def _report_missing_test_data(ctx, files) -> None:
    """One list of every src/data file the specs need and the workspace lacks."""
    from qa_pipeline.verifiers import data_files
    gaps = data_files.missing(files, ctx.repo_dir)
    if not gaps:
        return
    write_json(ctx.artifact("missing_test_data.json"), gaps)
    print(f"      ⚠️  {len(gaps)} test-data file(s) are loaded by the specs but missing from "
          f"src/data/: {', '.join(sorted(gaps))}. Those tests fail until you add them. "
          f"See missing_test_data.json.")


def _restore_best_attempt(ctx, best_files, last_produced) -> int:
    """Puts the BEST attempt's files back on disk.

    Every generation attempt writes its files as it goes, and `repair_loop.run`
    returns the best attempt — not the last. When the last one regressed, the
    workspace kept the regression while generated_files.json described the
    better code, so the Executor ran something the run's own record disowned.
    Returns how many files were rewritten.
    """
    if best_files is last_produced or not best_files:
        return 0
    from agents.artifact_io import resolve_within
    rewritten = 0
    for generated in best_files:
        content = generated.get("content")
        target = resolve_within(ctx.repo_dir, generated.get("path"))
        if target is None or not isinstance(content, str) or not content.strip():
            continue
        if target.is_file() and read_text_file(target) == content:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        rewritten += 1
    return rewritten


def _write_repair_history(ctx, stage: str, histories: dict) -> None:
    """Persists what each gate objected to, per attempt, per item.

    A stage that repairs on every run and a stage that repairs once a month
    produce the same manifest entry ("2 file(s) written") and the same
    metrics row (repair_rate 1.0) — neither says WHAT was wrong. Without
    this, the recurring first-attempt defect that costs a full extra
    generation on every single run is invisible: the failing artifact is
    overwritten by the repaired one before anything reads it.

    `histories` is {item key: [VerifyResult, ...]} in attempt order. Written
    as a plain artifact rather than through a contract because it is
    diagnostic output, read by people, not an input to any later stage.
    """
    payload = []
    for key, results in sorted(histories.items()):
        for i, result in enumerate(results, start=1):
            if result is None:
                continue
            payload.append({
                "item": key,
                "attempt": i,
                "status": result.status,
                # The final attempt is recorded too, passing, so the file
                # shows the whole arc rather than only the bad half.
                "errors": list(result.errors or []),
                "repairs": list(result.repairs or []),
                "warnings": list(result.warnings or []),
            })
    if not payload:
        return
    write_json(ctx.artifact(f"{stage}_repair_history.json"), payload)


def memoised_verify(verify):
    """Wraps a `verify(files)` so the same generated files are gated once.

    Each code attempt was gated twice - once in `produce` to score it, then
    again by `repair_loop.run` - and the gate shells out to tsc per file. Keyed
    on a hash of every file's path, kind, case ids and content.
    """
    import hashlib
    import json
    cache = {}

    def wrapper(files):
        key = hashlib.sha256(json.dumps(
            [[f.get("path"), f.get("kind"), f.get("test_case_ids"), f.get("content"),
              f.get("declined")] for f in files or []],
            sort_keys=True, default=str).encode("utf-8")).hexdigest()
        if key not in cache:
            cache[key] = verify(files)
        return cache[key]

    return wrapper


def report_gate_outcome(outcome, *, say=print) -> list:
    """Says out loud what the code gate did NOT verify. Returns the warnings.

    A SKIPPED gate counts as ok (so a missing optional tool cannot block a
    run), which means a broken toolchain approves every file silently. That
    stays as is; this only makes it visible, along with specs that were kept
    although they still fail a gate after the repairs ran out.
    """
    result = getattr(outcome, "result", None)
    notes = []
    if result is None:
        return notes
    skipped = [w for w in result.warnings if "could not run:" in w]
    if skipped:
        shown = "; ".join(dict.fromkeys(skipped[:3]))
        notes.append(f"{len(skipped)} gate check(s) were SKIPPED, so those files are "
                     f"NOT verified by them: {shown}")
    if not outcome.ok and not outcome.declined:
        locator_failures = [e for e in result.errors if "ledger" in e or "locator" in e
                            or "accessible name" in e or "not confirmed" in e]
        notes.append(f"{len(result.errors)} gate problem(s) remain after the repairs ran "
                     f"out; the affected spec(s) were kept but FAILED the gate"
                     + (f" ({len(locator_failures)} on locators)" if locator_failures else "")
                     + f": {result.errors[0]}")
    for note in notes:
        say(f"      ⚠️  {note}")
    return notes
