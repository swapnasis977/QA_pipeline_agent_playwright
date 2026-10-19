"""Shared, hardened file I/O for every agent's on-disk artifacts.

Three problems this fixes, each of which was repeated in every agent module:

1. ENCODING. `Path.write_text()` / `read_text()` with no `encoding=` use the
   platform's locale codepage — cp1252 on this Windows environment, not
   UTF-8. Every artifact these agents write is LLM output: em dashes, curly
   quotes, "✅"/"⚠️" status markers, and any non-Latin text in a test case.
   Writing those through cp1252 raises UnicodeEncodeError mid-pipeline (or
   silently mangles them), and reading a UTF-8 file back through cp1252
   mojibakes it. Every function here is explicitly UTF-8 in both directions.

2. CORRUPT/PARTIAL JSON. Agents re-read their own artifacts across runs
   (locator memory, the bug registry, the reviewer's prior-files cache).
   A truncated or hand-edited file made `json.loads` raise from inside a
   stage that had nothing to do with the corruption, aborting a run whose
   correct behavior is "treat it as absent and rebuild". `read_json` returns
   the caller's default instead, and says so on stderr.

3. TORN WRITES. Several artifacts are rewritten incrementally while a run is
   in progress (coding_agent's per-chunk unresolved log) specifically so a
   human can inspect partial progress. A plain `write_text` truncates the
   file first, so reading it at the wrong moment yields half a JSON
   document. `write_json`/`write_text_file` write a sibling temp file and
   `os.replace()` it into place, which is atomic on both POSIX and Windows.

Deliberately dependency-free and side-effect-free apart from the I/O itself,
so it stays safe to import from every agent (including the two that must work
with no API key configured at all — bug_reporter and jira_uploader).
"""

import json
import os
import re
from pathlib import Path

#: Written next to the target, then os.replace()d over it. Kept in the same
#: directory on purpose: os.replace() is only atomic within one filesystem,
#: so a temp file in the system temp dir could land on a different volume.
_TMP_SUFFIX = ".tmp"

#: A leading "./" (repeated or not) on an otherwise repo-relative path.
_LEADING_DOT_SLASH_RE = re.compile(r"^(\./)+")


def normalize_target_file(value) -> str:
    """One spelling for a plan's `target_file`: repo-relative, forward
    slashes, no leading "./" or "/".

    Lives here — the lowest shared layer — because BOTH sides of the
    Planner -> Coding Agent handoff must agree on it, and they reach it from
    opposite directions: `qa_pipeline.agents.adapters.normalize_plan` on the
    contract side, `coding_agent._load_planner_plans` on the reader side.
    Defining it in either one would make the other import its consumer.

    The shape is not cosmetic. The Executor hands `target_file` to
    Playwright's CLI, which treats it as a REGEX matched against test file
    paths: a backslashed or absolute path matches nothing and yields "No
    tests found" with zero tests collected, reported as a clean run. And the
    Coding Agent groups cases by this string, so a Windows-style
    backslashed path, "./tests/a.spec.ts" and "tests/a.spec.ts" were three
    different files —
    three independent generation calls writing three versions of one spec,
    the last silently overwriting the rest.
    """
    return _LEADING_DOT_SLASH_RE.sub("", str(value or "").replace("\\", "/")).lstrip("/")


def read_text_file(path, default: str = "") -> str:
    """UTF-8 text read that never raises. Returns `default` for a missing
    path, a directory, or an unreadable file.

    Falls back to a lossy decode rather than failing when the bytes aren't
    valid UTF-8 — that means an artifact written by an OLDER version of this
    pipeline (before this module existed, i.e. through the cp1252 default)
    still loads, with the odd character replaced, instead of taking down a
    run over a legacy encoding.
    """
    p = Path(path) if path else None
    if not p or not p.is_file():
        return default
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"⚠️  '{p}' is not valid UTF-8 (likely written by an older run) — "
              f"decoding with replacement characters.")
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"⚠️  Could not read '{p}' ({exc}) — treating it as empty.")
        return default


def write_text_file(path, text: str) -> Path:
    """Atomic UTF-8 text write. Creates parent directories as needed.

    Two Windows realities are handled here, because this is the one place every
    artifact (including run_manifest.json) is written through:
      * the temp file is unique per writer, so two threads or processes writing
        the same artifact cannot trample one shared `.tmp`;
      * `os.replace` raises PermissionError while an editor, an indexer or an
        antivirus scan holds the destination open. That is momentary, so retry a
        few times before giving up — otherwise a stage whose work was done was
        reported failed because of a file lock.
    """
    import threading
    import time

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.{os.getpid()}-{threading.get_ident()}{_TMP_SUFFIX}")
    try:
        tmp.write_text(text, encoding="utf-8")
        for attempt in range(6):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.2 * (attempt + 1))
    finally:
        # A failed write must not leave a stray .tmp behind that a later
        # glob (e.g. the reviewer CLI's --file-glob) could pick up as real.
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    return p


def read_json(path, default=None):
    """UTF-8 JSON read that never raises — returns `default` for a missing,
    empty, unreadable, or corrupt file.

    Also type-checks against `default`: a file containing valid JSON of the
    WRONG shape (an object where the caller needs a list, e.g. a
    hand-edited locator_memory.json) is rejected the same way corrupt JSON
    is, since handing it back would just move the crash into the caller's
    own iteration.
    """
    raw = read_text_file(path, default="")
    if not raw.strip():
        return default
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"⚠️  '{path}' is not valid JSON ({exc.msg} at line {exc.lineno}) — "
              f"ignoring it and continuing with the default.")
        return default
    if default is not None and not isinstance(parsed, type(default)):
        print(f"⚠️  '{path}' holds {type(parsed).__name__}, expected "
              f"{type(default).__name__} — ignoring it and continuing with the default.")
        return default
    return parsed


def write_json(path, data) -> Path:
    """Atomic, UTF-8, human-readable JSON write.

    `ensure_ascii=False` so a test case containing non-ASCII text stays
    readable in the artifact (these files are reviewed by humans, and
    "\\u2014" instead of an em dash is exactly the kind of noise that makes
    a reviewer distrust the artifact). `default=str` so a stray Path or
    datetime in a payload serializes instead of raising — several callers
    already passed `default=str` for this reason and several forgot to.
    """
    return write_text_file(
        path, json.dumps(data, indent=2, ensure_ascii=False, default=str))


def resolve_within(root, relative_path):
    """Resolves `relative_path` under `root`, returning None if the result
    escapes `root`.

    Every path an LLM produces is untrusted input. `root / "/etc/passwd"`
    DISCARDS root entirely (pathlib absolute-path semantics) and
    `root / "../../x"` climbs out of it — either way a subsequent write
    lands outside the target repo. coding_agent.py already confined its own
    generated-file paths this way; self_healer.py wrote to a path taken
    straight from the Reviewer's `file` field with no confinement at all,
    which is the same untrusted value flowing to the same kind of write.
    """
    root_resolved = Path(root).resolve()
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    if not normalized:
        return None
    candidate = (root_resolved / normalized).resolve()
    if not candidate.is_relative_to(root_resolved):
        return None
    return candidate
