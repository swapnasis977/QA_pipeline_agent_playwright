"""How much effort did automating this story save? Measured facts, plus one stated assumption.

MEASURED, read from the run: automated cases, spec lines produced, tokens, the model's wall-clock time,
the share of generated files that needed repair, and how many cases passed.

ASSUMED, and printed as an assumption: what a person would have spent writing a script by hand and
what they spend reviewing a generated one. Defaults are round numbers, not a benchmark; set
`EFFORT_MANUAL_MINUTES_PER_CASE` and `EFFORT_REVIEW_MINUTES_PER_CASE` to your team's own figures so the
saving is yours. The pipeline cannot measure a counterfactual, and this report does not pretend to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

DEFAULT_MANUAL_MIN = 60
DEFAULT_REVIEW_MIN = 10


def _minutes(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, default)))
    except ValueError:
        return default


def _json_list(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def measure(run_dir: Path, spec_files: list[Path], matrix_totals: dict) -> dict:
    tokens = _json_list(run_dir / "_internal" / "token_log.json")
    metrics = _json_list(run_dir / "_internal" / "run_metrics.json")
    generated = sum(int(m.get("files_generated", 0)) for m in metrics)
    repaired = sum(int(m.get("repaired", 0)) for m in metrics)
    lines = sum(len(p.read_text(encoding="utf-8", errors="replace").splitlines()) for p in spec_files)
    return {
        "automated_cases": matrix_totals.get("with_script", 0),
        "passed": matrix_totals.get("passed", 0),
        "failed": matrix_totals.get("failed", 0),
        "spec_files": len(spec_files),
        "spec_lines": lines,
        "model_calls": len(tokens),
        "model_minutes": round(sum(float(t.get("elapsed_sec", 0)) for t in tokens) / 60, 1),
        "input_tokens": sum(int(t.get("input_tokens", 0)) for t in tokens),
        "output_tokens": sum(int(t.get("output_tokens", 0)) for t in tokens),
        "files_generated": generated,
        "files_repaired": repaired,
    }


def render(run_name: str, m: dict) -> str | None:
    cases = m["automated_cases"]
    if not cases:
        return None
    manual_min = _minutes("EFFORT_MANUAL_MINUTES_PER_CASE", DEFAULT_MANUAL_MIN)
    review_min = _minutes("EFFORT_REVIEW_MINUTES_PER_CASE", DEFAULT_REVIEW_MIN)
    manual_h = cases * manual_min / 60
    review_h = cases * review_min / 60
    saving = (1 - review_h / manual_h) * 100 if manual_h else 0
    repair = (f"{100 * m['files_repaired'] / m['files_generated']:.0f}% of generated files needed a repair pass"
              if m["files_generated"] else "no generation record in this run folder")
    ran = f"{m['passed']} passed, {m['failed']} failed" if (m["passed"] or m["failed"]) else "not executed yet"
    return f"""# Effort: {run_name}

## Measured

| | |
|---|---|
| Cases with an automation script | {cases} ({ran}) |
| Spec files / lines of code | {m['spec_files']} / {m['spec_lines']} |
| Model calls / wall-clock | {m['model_calls']} / {m['model_minutes']} min (unattended) |
| Tokens in / out | {m['input_tokens']:,} / {m['output_tokens']:,} |
| Generation quality | {repair} |

## Estimated human effort (assumption, not measured)

| | Hours |
|---|---|
| Writing {cases} scripts by hand at {manual_min} min each | {manual_h:.1f} |
| Reviewing generated scripts at {review_min} min each | {review_h:.1f} |
| **Estimated saving** | **{manual_h - review_h:.1f} h ({saving:.0f}%)** |

The per-case minutes are defaults. Set `EFFORT_MANUAL_MINUTES_PER_CASE` and
`EFFORT_REVIEW_MINUTES_PER_CASE` in `.env` to your team's real numbers to make this your figure.
Time spent fixing failing generated scripts is not included: add it if the failed count above is not zero.
"""
