"""Consolidates the 27 per-story sheets under runs/<app>/PROJ-68294_FEATURE/1_testcases/ into
ONE sheet sized for an automation suite: every distinct functional area
(Sub-Component) still represented, but near-duplicate cases (same control,
same assertion, reworded) collapsed to a single representative case.

Heuristic, not LLM-judged — cheap and deterministic since the 543 raw cases
were already read once to produce this run's token/time cost:
  1. Normalize each row's Sub-Component into a coarser "feature bucket" by
     stripping the common "Face Comparison Tool - " prefix and merging
     buckets whose remaining words overlap heavily (e.g. "Toolbar",
     "Toolbar Controls", "Image Toolbar" -> one bucket).
  2. Within a bucket, cluster cases by title similarity (difflib ratio) so
     "Zoom In ..." and "Zoom Out ..." stay distinct, but two wordings of the
     exact same assertion collapse to one.
  3. Keep ONE case per cluster: prefer Execution Type == Automated, then the
     case with the most detailed Expected Result (more assertions to
     automate against).
"""
from __future__ import annotations

import glob
from difflib import SequenceMatcher
from pathlib import Path

import openpyxl

# Edit <app> to your profile name. Sheets live in the run folder's 1_testcases/.
SRC_DIR = Path("runs/Example App/PROJ-68294_FEATURE/1_testcases")
OUT_PATH = SRC_DIR / "PROJ-68294_Automation_Suite_Consolidated.xlsx"

_TITLE_SIM_THRESHOLD = 0.72
_BUCKET_OVERLAP_THRESHOLD = 0.5
_MAX_PER_BUCKET = 10


def load_all_rows():
    header = None
    rows = []
    for f in sorted(glob.glob(str(SRC_DIR / "*_TestCases.xlsx"))):
        if "Automation_Suite_Consolidated" in f:
            continue
        wb = openpyxl.load_workbook(f, read_only=True)
        ws = wb.active
        data = list(ws.iter_rows(values_only=True))
        if header is None:
            header = list(data[0])
        rows.extend(data[1:])
    return header, rows


def normalize_bucket_name(sub_component: str) -> str:
    name = (sub_component or "").replace("Face Comparison Tool - ", "").strip()
    return name or "General"


def bucket_key(existing_buckets: list[str], name: str) -> str:
    """Merges `name` into an existing bucket if their word sets overlap
    enough, otherwise returns `name` as a new bucket."""
    words = set(name.lower().split())
    for existing in existing_buckets:
        existing_words = set(existing.lower().split())
        if not words or not existing_words:
            continue
        overlap = len(words & existing_words) / min(len(words), len(existing_words))
        if overlap >= _BUCKET_OVERLAP_THRESHOLD:
            return existing
    return name


def _cluster_once(cases: list[dict], threshold: float) -> list[dict]:
    clusters: list[list[dict]] = []
    for case in cases:
        placed = False
        for cluster in clusters:
            if SequenceMatcher(None, case["title"].lower(), cluster[0]["title"].lower()).ratio() >= threshold:
                cluster.append(case)
                placed = True
                break
        if not placed:
            clusters.append([case])

    reps = []
    for cluster in clusters:
        def score(c):
            is_automated = 1 if (c["row"][idx_execution] or "").strip().lower() == "automated" else 0
            detail = len(c["row"][idx_expected] or "")
            return (is_automated, detail)
        reps.append(max(cluster, key=score))
    return reps


def cluster_and_pick(cases: list[dict]) -> list[dict]:
    """Greedy clustering by title similarity; keeps one representative per
    cluster, biased toward Automated execution type and richer assertions.

    A bucket (e.g. "Image Navigation & Transformation Controls") can hold
    many genuinely distinct controls (zoom, pan, rotate, crop ...) that
    title-similarity correctly keeps apart — dedup alone won't shrink those
    to automation-suite size. For an oversized bucket, the threshold is
    progressively relaxed (coarser clusters = fewer, broader reps) until it
    fits _MAX_PER_BUCKET or hits the floor; a floor overrun is truncated
    rather than looped forever, trading completeness for suite size exactly
    as asked."""
    threshold = _TITLE_SIM_THRESHOLD
    reps = _cluster_once(cases, threshold)
    while len(reps) > _MAX_PER_BUCKET and threshold > 0.35:
        threshold -= 0.1
        reps = _cluster_once(cases, threshold)
    if len(reps) > _MAX_PER_BUCKET:
        reps = sorted(
            reps,
            key=lambda c: (c["row"][idx_execution] or "").strip().lower() != "automated",
        )[:_MAX_PER_BUCKET]
    return reps


def main():
    global idx_execution, idx_expected
    header, rows = load_all_rows()
    idx = {h: i for i, h in enumerate(header)}
    idx_execution = idx["Execution Type"]
    idx_expected = idx["Expected Result"]
    idx_subcomp = idx["Sub-Component"]
    idx_name = idx["Name"]

    print(f"Loaded {len(rows)} raw cases from {SRC_DIR}")

    buckets: dict[str, list[dict]] = {}
    bucket_names: list[str] = []
    for row in rows:
        raw_name = normalize_bucket_name(row[idx_subcomp])
        key = bucket_key(bucket_names, raw_name)
        if key not in buckets:
            buckets[key] = []
            bucket_names.append(key)
        buckets[key].append({"title": row[idx_name] or "", "row": row})

    print(f"Collapsed 68 raw Sub-Components into {len(buckets)} feature bucket(s)")

    final_rows = []
    for _bucket, cases in buckets.items():
        reps = cluster_and_pick(cases)
        final_rows.extend(r["row"] for r in reps)

    print(f"Consolidated {len(rows)} -> {len(final_rows)} case(s) "
          f"({len(final_rows) / len(rows):.0%} of original)")

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Automation Suite"
    ws.append(header)
    # Re-number Test Case ID sequentially within the consolidated sheet so IDs
    # don't collide across the 27 source stories (each had its own TC-001...).
    idx_tcid = idx["Test Case ID"]
    for n, row in enumerate(final_rows, start=1):
        row = list(row)
        row[idx_tcid] = f"PROJ-68294-AUTO-TC-{n:03d}"
        ws.append(row)

    for col_cells in ws.columns:
        max_len = max((len(str(c.value)) for c in col_cells if c.value), default=10)
        ws.column_dimensions[col_cells[0].column_letter].width = min(max_len + 2, 60)

    wb.save(OUT_PATH)
    print(f"-> {OUT_PATH}")


if __name__ == "__main__":
    main()
