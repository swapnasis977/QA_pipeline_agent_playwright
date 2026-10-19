"""Helpers around a generated file's locators: what a repair is shown, which locators are proven, verifying and repairing the unverified ones.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

import json
from pathlib import Path

from agents.artifact_io import read_json, write_json
from agents.coding_cases import _MIN_REAL_CONTENT_LENGTH, _is_test_spec
from utils import dom_inspector, locator_verifier
from utils.llm_client import call_agent, parse_json_response
from utils.run_layout import artifact_path

#: How much of one previously-generated file to echo back on a repair. A
#: whole spec plus its page objects is the point; a runaway file is not.
_MAX_PRIOR_CHARS = 14000


def _norm_path(path) -> str:
    return str(path or "").replace("\\", "/").lstrip("./").lower()


def _prior_output_block(prior_files: dict, chunk_cases: list, target_file: str,
                        exclude_paths=()) -> str:
    """The files this chunk produced last time, so a repair EDITS them.

    `repair_prompt` has always said "the following problems were found in your
    previous output; fix EXACTLY these" — while the user prompt carried only
    the test cases, the planner doc and the DOM snapshots. The previous output
    itself was never included, so every repair regenerated ~200 lines from
    scratch, blind to what it had just written.

    That is why repairs oscillated rather than converged: 13 -> 5 -> 10 -> 9
    problems on one real case, because each attempt is an independent rewrite
    and a fix in one place reintroduces variance everywhere else. Showing the
    model its own file turns a rewrite into an edit.

    Scoped to this chunk's target file and the supporting files emitted with
    it — a spec and the page object it imports are only valid together, so
    repairing one blind to the other is how a fix in the spec breaks its POM.
    """
    if not prior_files:
        return ""

    # Files the pipeline generates deterministically (page objects, route and
    # slider helpers) are rebuilt every run and any model rewrite of them is
    # discarded, so echoing them back is paid input that cannot change anything.
    excluded = {_norm_path(p) for p in (exclude_paths or ())}
    wanted = {target_file} if target_file else set()
    spec_text = (prior_files.get(target_file) or "") if target_file else ""
    case_ids = [str(c.get("Test Case ID")) for c in chunk_cases or [] if c.get("Test Case ID")]
    supporting = [p for p in prior_files
                  if not _is_test_spec(p) and _norm_path(p) not in excluded]
    # Supporting files (page objects, helpers) travel with their spec — but only
    # the ones THIS chunk's spec imports, or that carry one of its case ids. If
    # nothing can be tied to the chunk, keep the old behaviour (all of them)
    # rather than send a repair blind to the files it must edit.
    related = [p for p in supporting
               if (Path(p).stem and Path(p).stem in spec_text)
               or any(cid in (prior_files.get(p) or "") for cid in case_ids)]
    wanted.update(related or supporting)

    rendered = []
    for path in sorted(wanted):
        content = prior_files.get(path)
        if not content:
            continue
        rendered.append(f"--- {path} ---\n{content[:_MAX_PRIOR_CHARS]}")
    if not rendered:
        return ""

    return ("\n\nYOUR PREVIOUS OUTPUT FOR THESE CASES, exactly as it was written. "
            "EDIT IT — return these same files with ONLY the problems listed above "
            "fixed, and every other line left unchanged. Do not restructure, "
            "rename, re-order or 'improve' anything that was not named. A "
            "from-scratch rewrite reintroduces the defects you already fixed:\n"
            + "\n\n".join(rendered))


def _inline_locators_in_spec(rel_path: str, content: str) -> list:
    """Locator expressions written DIRECTLY in a test spec rather than
    behind a page object.

    Why this matters for self-healing rather than for style: a locator
    breaks because the APP changed, so the fix belongs where the locator is
    DEFINED. When five specs each inline their own copy of the same
    selector, the Self-Healer patches one spec, the verification re-run for
    that one case passes, and the other four fail independently and get
    healed independently — five LLM calls, five Executor re-runs, and five
    chances to diverge, for one root cause. Behind a page object the same
    fix lands once and every test using that element inherits it.

    Reported, not rejected: an inline locator is working code, and throwing
    away a compiling file over a structural preference would cost more than
    it saves. Same posture as the title-prefix convention check.
    """
    if not _is_test_spec(rel_path):
        return []
    return [loc.get("raw", "") for loc in locator_verifier.extract_locators(content)]


#: Cap on confirmed-locator hints sent to the model. This store grows for the
#: life of a feature, and it rides in extra_system on EVERY chunk call.
_MAX_PROVEN_LOCATOR_HINTS = 40


def _proven_locator_hints(feature_dir: Path) -> str:
    """Renders locator_memory.json's confirmed fixes as reuse hints.

    Excludes anything in locator_memory_failed.json — those are pairs the
    Self-Healer applied and the verification re-run then FAILED (see
    self_healer.mark_heal_result), so telling the generator to reuse one
    would re-introduce a fix already proven not to work.
    """
    memory = read_json(artifact_path(feature_dir, "locator_memory.json"), default=[])
    failed = set(read_json(artifact_path(feature_dir, "locator_memory_failed.json"), default=[]))
    if not isinstance(memory, list):
        return ""

    lines = []
    for entry in memory:
        if not isinstance(entry, dict):
            continue
        old, new = entry.get("old_locator"), entry.get("new_locator")
        if not (old and new) or f"{old}::{new}" in failed:
            continue
        lines.append(f"- use {new}  (NOT {old} — that one was confirmed broken)")
        if len(lines) >= _MAX_PROVEN_LOCATOR_HINTS:
            break
    return "\n".join(lines)


def _snapshot_elements_for_cases(chunk_cases: list, dom_snapshots: dict) -> list:
    """The union of DOM-snapshot elements available for this chunk's cases.

    A chunk is one target_file, so its cases share a screen in practice;
    pooling their elements gives locator verification the widest true
    picture rather than failing a locator that belongs to a sibling case's
    snapshot of the same screen.
    """
    elements = []
    for case in chunk_cases or []:
        snapshot = (dom_snapshots or {}).get(case.get("Test Case ID"))
        if isinstance(snapshot, dict):
            elements.extend(snapshot.get("elements") or [])
    if elements:
        # Setup steps in a page object belong to EARLIER screens; see
        # dom_inspector.cached_route_elements.
        elements.extend(dom_inspector.cached_route_elements())
    # The verifier counts MATCHING ELEMENTS to decide a locator is ambiguous, so
    # the same element arriving from two cases (or from the route cache as well)
    # made every unique locator look like it matched twice.
    unique, seen = [], set()
    for el in elements:
        key = (el.get("role"), el.get("name"), el.get("locator"), el.get("id"))
        if key not in seen:
            seen.add(key)
            unique.append(el)
    return unique


def _verify_file_locators(content: str, chunk_cases: list, dom_snapshots: dict) -> dict:
    """Verifies a generated file's locators against whatever live snapshot
    this chunk has. Returns {} when there is no snapshot to check against —
    "no evidence" must not read as "every locator is wrong"."""
    elements = _snapshot_elements_for_cases(chunk_cases, dom_snapshots)
    if not elements:
        return {}
    result = locator_verifier.verify_file(content, elements)
    result["_elements"] = elements
    return result


def _repair_unverified_locators(content: str, verification: dict, feature_dir: Path,
                                 extra_system: str, compile_error: str = "") -> str:
    """One repair call: hand the model back its own file plus the REAL names
    of the elements on that screen, and ask it to correct only the locators
    that don't exist.

    This is the step that actually converts a would-be Executor failure into
    a working file. Without it, verification only produces a warning nobody
    reads until the suite has already run and failed. Scoped tightly — the
    model is told to change nothing but the named locators — because a free
    rewrite here would risk losing correct logic to fix a selector.

    Returns the repaired content, or the original on any failure: a failed
    repair must leave the already-written (compiling) file exactly as it was.
    """
    instructions = locator_verifier.describe_unverified(verification["unverified"])
    retry_note = ("\nYour previous repair of this file did NOT COMPILE: "
                  + compile_error[:600] + "\nFix that - keep the syntax valid.\n"
                  if compile_error else "")
    verified_names = sorted({
        el.get("name") for el in verification.get("_elements", []) if el.get("name")
    })
    prompt = f"""The file below COMPILES, but these locators do not match any element
on the live screen this test targets:

{instructions}

Every element actually present on that screen (verified, from a live DOM
snapshot taken just now) is listed here — use ONLY these:
{json.dumps(verified_names, indent=2)}

Correct ONLY the locators listed above. Do not restructure the test, rename
anything, change assertions, or touch a locator that wasn't listed. If a
listed locator has no correct equivalent in the verified list, leave it as
it is rather than substituting an unrelated element.

Return JSON: {{"content": "<the corrected full file>"}}
{retry_note}
File:
{content}"""

    try:
        raw = call_agent(agent_name="coding_agent", user_prompt=prompt,
                          feature_dir=feature_dir, max_tokens=16000,
                          extra_system=extra_system)
        result = parse_json_response(raw, feature_dir, "coding_agent")
        repaired = result.get("content") if isinstance(result, dict) else None
        if isinstance(repaired, str) and len(repaired.strip()) >= _MIN_REAL_CONTENT_LENGTH:
            return repaired
    except Exception as exc:
        print(f"      ⚠️  Locator repair pass failed ({exc}) — keeping the original file.")
    return content


#: Diagnostic files this stage writes only when it has something to report.
#: Every one is named in a console message that tells the operator to go read
#: it, so a leftover copy from an earlier run is worse than no file at all.
_PER_RUN_DIAGNOSTICS = (
    "coding_agent_rejected_files.json",
    "coding_agent_title_prefix_warnings.json",
    "coding_agent_inline_locator_warnings.json",
    "coding_agent_overwrite_warnings.json",
    "coding_agent_infra_failures.json",
    "coding_agent_unverified_locators.json",
)


def _clear_stale_diagnostics(feature_dir: Path, produced: dict) -> None:
    """Truncates the warning files this run did NOT produce.

    These are written under `if warnings:`, so a run that fixes a problem
    leaves the previous run's file sitting on disk unchanged. Observed: the
    Coding Agent stopped emitting title-prefix warnings entirely, and
    coding_agent_title_prefix_warnings.json still named a file and a case id
    from two runs earlier — a defect report for something that no longer
    happens, in the exact file the console tells you to open.

    Emptied rather than deleted: `[]` says "this run checked and found
    none", while an absent file cannot be told apart from a stage that never
    reached the check.
    """
    for name in _PER_RUN_DIAGNOSTICS:
        if produced.get(name):
            continue
        path = artifact_path(feature_dir, name)
        if path.exists():
            write_json(path, [])
