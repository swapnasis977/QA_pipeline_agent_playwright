"""
Shared memory: case_index.json — the canonical test_case_id -> {name,
target_file} map for one feature, written once after the Automation Planner
runs and read by every agent downstream of it (Reviewer, Self-Healer,
Bug Reporter).

Why this exists: before this file, each downstream agent re-derived its own
join between the sheet's "Test Case ID" and the Planner's "target_file" — the
old orchestrator rebuilt it in memory every run, and self_healer's
file-resolution fell back through three tiers (planner target_file ->
reviewer's own "file" field -> a basename match) precisely because there was
no single place agents could trust. Persisting the join once, right after the
Planner produces it, means every later agent reads the SAME mapping instead of
re-deriving a possibly-different one from whichever inputs it happens to have
on hand.

The authoritative join is now the `generated_file` contract's `test_case_ids`,
declared by the agent that wrote each file — this index remains the
Planner-side view of the same relationship (id -> name -> intended
target_file), which is what the Reviewer and Bug Reporter read for a case's
human-readable name.
"""

import re
from pathlib import Path

# The same hardened I/O every other agent artifact goes through (explicit
# UTF-8 both ways, atomic replace, corrupt file treated as absent). This file
# used bare write_text/read_text, which on Windows means the cp1252 locale
# codepage — so a case "Name" carrying an em dash or any non-Latin character
# read back mojibaked, and a run interrupted mid-write left a half-written
# index that then raised inside whichever later agent read it.
from agents.artifact_io import read_json, write_json
from utils.run_layout import artifact_path


def build(test_cases: list, planner_doc: list) -> list:
    """Pairs each sheet row's real 'Test Case ID' with its title and the
    Planner's target_file. Lives here, rather than inside the orchestrator,
    so it is a shared, importable, testable step instead of a module-private
    helper."""
    target_by_id = {
        p.get("test_case_id"): p.get("target_file")
        for p in planner_doc
        if isinstance(p, dict) and p.get("test_case_id")
    }
    index = []
    for row in test_cases:
        case_id = row.get("Test Case ID")
        if not case_id:
            continue
        index.append({
            "test_case_id": case_id,
            "name": row.get("Name", ""),
            "target_file": target_by_id.get(case_id, ""),
        })
    return index


def write(feature_dir: Path, index: list) -> Path:
    return write_json(artifact_path(feature_dir, "case_index.json"), index)


def read(feature_dir: Path) -> list:
    return read_json(artifact_path(feature_dir, "case_index.json"), default=[])


#: Case ids are embedded in test titles by the Coding Agent's skill. A title
#: with no id yields "" rather than a guess — an invented id silently drops
#: the case, which is the exact failure `skills/reviewer.skill.md` warns about.
#:
#: ALTERNATIVES ARE ORDERED LONGEST-FIRST, and that ordering is the whole
#: point: Python's alternation takes the first branch that matches at the
#: earliest position, not the longest one. The previous pattern covered
#: `PROJ-1234-TC-007` (project key, story number, case number) and a bare
#: `TC-007`, but had no branch for `<PREFIX>-TC-<n>` — which is this
#: project's entire scheme. So "VENDOR-TC-001 ..." fell through to the
#: bare-`TC-\d+` branch and came back as "TC-001": the story half silently
#: dropped. Every consumer keys on this — `heal_succeeded` matching a heal to
#: its case, the Bug Reporter matching screenshots, `writeback` recording
#: outcomes — so a truncated id joins to nothing and each of them quietly
#: falls back to a weaker signal. Confirmed on a real run.
ID_IN_TITLE = re.compile(
    r"\b("
    r"[A-Z][A-Z0-9]*-\d+-[A-Z][A-Z0-9]*-TC-\d+"  # PROJ-68294-AUTO-TC-019
    r"|[A-Z][A-Z0-9]*-\d+-TC-\d+"   # PROJ-68308-TC-012
    r"|[A-Z][A-Z0-9]*-TC-\d+"      # VENDOR-TC-001, REG-TC-001
    r"|[A-Z][A-Z0-9]*-\d+"         # PROJ-1234, and a bare TC-007
    r")\b"
)


def case_id_from_title(title: str) -> str:
    match = ID_IN_TITLE.search(str(title or ""))
    return match.group(1) if match else ""
