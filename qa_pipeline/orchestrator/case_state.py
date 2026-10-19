"""pipeline_state.yml — where each case stands: locators -> page object -> script.

The run manifest says which STAGES finished. It cannot say that TC-003 has its
locators and a page object but no script, or that a script was built from a
locator the ledger has since demoted. This file says that, per case.

DERIVED, NEVER AUTHORITATIVE. Every status is recomputed from the artifacts on
disk (plan, generated files, the files themselves) each time it is written, so
it cannot disagree with them. The one thing carried over from the previous
write is `built_from`: the input/output hashes a stage was last seen done with.
That is what lets "inputs changed but the output did not" be reported as
STALE instead of silently "done". Written by the orchestrator only — a model
never edits it, and nothing in the pipeline trusts it over the artifacts.

Emit-only YAML (JSON scalars are valid YAML) so no YAML library is needed; the
one machine-read line per case is a single-line flow mapping that is also JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from agents.artifact_io import read_json
from utils.run_layout import artifact_path

STATE_FILE = "pipeline_state.yml"
STAGES = ("locators", "page_object", "script")
_SUPPORT_KINDS = {"page", "component"}


def _hash(obj) -> str:
    raw = json.dumps(obj, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _file_hash(repo_dir, generated: dict) -> str | None:
    """Hash of the file AS IT IS ON DISK (a heal edits it after generation),
    or None when it is not there."""
    path = Path(repo_dir) / str(generated.get("path") or "")
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    except OSError:
        return None


def _locator_stage(case_hash: str, plan: dict | None) -> dict:
    if not plan:
        return {"status": "pending", "reason": "not planned yet"}
    elements = plan.get("elements") or []
    missing = [f"{e.get('screen')}/{e.get('logical_name')}"
               for e in elements if not e.get("locator")]
    pairs = [(e.get("screen"), e.get("logical_name"), e.get("locator") or "")
             for e in elements]
    stage = {"input": _hash([case_hash, [p[:2] for p in pairs]]),
             "output": _hash(pairs), "elements": len(elements)}
    if missing:
        return {**stage, "status": "blocked", "missing": missing}
    if not elements and (plan.get("test_kind") or "").lower() != "api":
        return {**stage, "status": "pending", "reason": "the plan lists no elements"}
    return {**stage, "status": "done"}


def _file_stage(repo_dir, files: list, upstream: dict, upstream_name: str,
                extra_input: str) -> dict:
    if upstream["status"] != "done":
        return {"status": "pending", "reason": f"waiting on {upstream_name}"}
    if not files:
        return {"status": "pending", "reason": "no file generated for this case"}
    hashes = [_file_hash(repo_dir, f) for f in files]
    paths = [f.get("path") for f in files]
    gone = [p for p, h in zip(paths, hashes) if h is None]
    stage = {"input": _hash([upstream["output"], extra_input]),
             "files": sorted(paths)}
    if gone:
        return {**stage, "status": "missing", "missing": sorted(gone)}
    return {**stage, "output": _hash(sorted(zip(paths, hashes))), "status": "done"}


def _settle(stage: dict, previous: dict | None) -> tuple[dict, dict | None]:
    """Applies staleness and returns (stage, what to remember as built_from).

    A stage is STALE when its inputs differ from what it was last built from
    but its output is byte-identical — i.e. nothing rebuilt it. A changed
    output means it was regenerated (or healed), so it is fresh again."""
    if stage["status"] != "done":
        return stage, previous
    built = {"in": stage["input"], "out": stage["output"]}
    if previous and previous.get("out") == built["out"] and previous.get("in") != built["in"]:
        return {**stage, "status": "stale",
                "reason": "its inputs changed after it was built"}, previous
    return stage, built


def derive(feature_dir, repo_dir, previous_state: dict | None = None) -> dict:
    cases = read_json(artifact_path(feature_dir, "test_cases.json"), default=[]) or []
    plans = {p.get("test_case_id"): p for p in
             (read_json(artifact_path(feature_dir, "automation_plan.json"), default=[]) or [])
             if isinstance(p, dict)}
    files = [f for f in (read_json(artifact_path(feature_dir, "generated_files.json"),
                                   default=[]) or []) if isinstance(f, dict)]
    manifest = read_json(artifact_path(feature_dir, "run_manifest.json"), default={}) or {}
    previous_built = (previous_state or {}).get("built_from", {})

    out_cases, built_from = {}, {}
    for case in cases:
        cid = case.get("test_case_id")
        if not cid:
            continue
        mine = [f for f in files if cid in (f.get("test_case_ids") or [])]
        loc = _locator_stage(_hash(case), plans.get(cid))
        pom = _file_stage(repo_dir, [f for f in mine if f.get("kind") in _SUPPORT_KINDS],
                          loc, "locators", "")
        if pom["status"] == "pending" and loc["status"] == "done" and not [
                f for f in mine if f.get("kind") in _SUPPORT_KINDS]:
            # API-only, or served by a shared/recorded page object: nothing of
            # its own to build, so the script waits on locators instead.
            pom = {"status": "n/a", "reason": "no page object generated for this case"}
        upstream, up_name = (loc, "locators") if pom["status"] == "n/a" else (pom, "page_object")
        spec = _file_stage(repo_dir, [f for f in mine if f.get("kind") == "spec"],
                           upstream, up_name, _hash([case, loc.get("output")]))
        # (the locators' output is in the script's input too: a page object that
        # nobody rebuilt keeps its hash, so the change must reach the script directly)
        entry, case_built = {}, {}
        prev = previous_built.get(cid, {})
        for name, stage in zip(STAGES, (loc, pom, spec)):
            settled, remember = _settle(stage, prev.get(name))
            entry[name] = settled
            if remember:
                case_built[name] = remember
        entry["next"] = next((n for n in STAGES
                              if entry[n]["status"] not in ("done", "n/a")), "complete")
        out_cases[cid] = entry
        built_from[cid] = case_built

    totals = {n: {} for n in STAGES}
    for entry in out_cases.values():
        for n in STAGES:
            status = entry[n]["status"]
            totals[n][status] = totals[n].get(status, 0) + 1
    return {
        "note": "Derived from the run's artifacts; edit nothing here. See qa_pipeline/orchestrator/case_state.py.",
        "pipeline_stages_done": sorted((manifest.get("completed") or {}).keys()),
        "totals": totals,
        "cases": out_cases,
        "built_from": built_from,
    }


# -- emit / read -------------------------------------------------------------

def _scalar(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _emit(value, indent: int, lines: list):
    pad = "  " * indent
    for key, item in value.items():
        if isinstance(item, dict) and item:
            lines.append(f"{pad}{_scalar(str(key)) if _needs_quote(key) else key}:")
            _emit(item, indent + 1, lines)
        else:
            lines.append(f"{pad}{_scalar(str(key)) if _needs_quote(key) else key}: {_scalar(item)}")


def _needs_quote(key) -> bool:
    return not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key))


def dump(state: dict) -> str:
    lines = [f"# {state['note']}"]
    for key in ("pipeline_stages_done", "totals"):
        lines.append(f"{key}: {_scalar(state[key])}")
    lines.append("cases:")
    for cid, entry in state["cases"].items():
        lines.append(f"  {_scalar(cid)}:")
        _emit(entry, 2, lines)
        lines.append(f"    built_from: {_scalar(state['built_from'].get(cid, {}))}")
    return "\n".join(lines) + "\n"


def load_built_from(path) -> dict:
    """Reads back ONLY the per-case `built_from` lines this module wrote."""
    result, case = {}, None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return result
    for line in text.splitlines():
        m = re.fullmatch(r'  ("(?:[^"\\]|\\.)*"):', line)
        if m:
            case = json.loads(m.group(1))
        elif case is not None and line.startswith("    built_from: "):
            try:
                result[case] = json.loads(line[len("    built_from: "):])
            except ValueError:
                result[case] = {}
    return result


def refresh(feature_dir, repo_dir) -> Path:
    path = artifact_path(feature_dir, STATE_FILE)
    state = derive(feature_dir, repo_dir, {"built_from": load_built_from(path)})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dump(state), encoding="utf-8")
    return path


def render_table(state: dict) -> str:
    rows = [f"{'case':<28} {'locators':<10} {'page_object':<12} {'script':<10} next"]
    for cid, e in state["cases"].items():
        rows.append(f"{cid:<28} {e['locators']['status']:<10} "
                    f"{e['page_object']['status']:<12} {e['script']['status']:<10} {e['next']}")
        for n in STAGES:
            detail = e[n].get("missing") or e[n].get("reason")
            if detail and e[n]["status"] != "pending":
                rows.append(f"    {n}: {detail}")
    return "\n".join(rows)
