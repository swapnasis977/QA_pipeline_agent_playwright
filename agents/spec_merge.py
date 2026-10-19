"""Merge the spec files a feature was split into back into one file per feature.

WHY THEY ARE SPLIT. The Coding Agent generates a few cases per model call (a whole
file's worth of code per case overflows the output cap), so one planned file such as
`image-editing-adjustments-panel.spec.ts` came back as SIX: `..._tc003-019`,
`..._tc020-021`, `..._tc023-051` and so on. That is a generation detail, and it is
the wrong shape for the person who owns the suite afterwards: they open "the
adjustments panel tests" and find six files named after case numbers, each with its
own copy of the same helpers.

WHAT THIS DOES. Deterministic and conservative. It only merges when it can show the
result means the same thing as the parts; otherwise it leaves the split files alone
and says why. No model call.

It refuses (keeps the files split) when:
  - two parts declare the same top-level name differently (a helper re-invented per
    chunk with a different body - merging would silently pick one);
  - a part has file-level hooks/`test.use`/`test.setTimeout`/`test.describe.configure`
    that the others lack: at file level they apply to EVERY test in the file, so merging
    would change what the other parts' tests run under;
  - two tests share a title;
  - a part uses a construct it cannot parse (an unbalanced block).

The caller compiles the merged file and falls back to the parts if it does not compile.
Pure text in, text out - no file I/O here, so it is easy to test.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

#: `_tc003-019`, `_tc14`, `_part2` immediately before `.spec.ts` / `.test.ts`.
_SPLIT_SUFFIX = re.compile(r"(?:_tc\d+(?:-\d+)*|_part\d+)(?=\.(?:spec|test)\.[jt]sx?$)", re.I)
_FIRST_NUMBER = re.compile(r"_(?:tc|part)(\d+)", re.I)

_FILE_LEVEL_SETTINGS = re.compile(
    r"^test\.(?:beforeEach|afterEach|beforeAll|afterAll|use|setTimeout|slow|fixme|skip|"
    r"describe\.configure|describe\.parallel\.configure)\b")
_DECL = re.compile(
    r"^(?:export\s+)?(?:(?:async\s+)?function\s*\*?\s*(\w+)|(?:const|let|var)\s+(\w+)\b|"
    r"(?:class|enum|interface|type)\s+(\w+)\b)")
_TEST_TITLE = re.compile(r"""\btest(?:\.(?:only|skip|fixme|fail|slow))?\s*\(\s*(['"`])((?:\\.|(?!\1).)*)\1""")
_SINGLE_BINDING = re.compile(
    r"""^import\s+(?:(\w+)|\*\s+as\s+(\w+))\s+from\s*['"][^'"]+['"]\s*;?\s*$""")
_NAMED_IMPORT = re.compile(r"^import\s+(type\s+)?\{([^}]*)\}\s*from\s*(['\"])([^'\"]+)\3\s*;?\s*$", re.S)


def base_path(path: str) -> str:
    """The path with any split marker removed: the file the feature should live in."""
    return _SPLIT_SUFFIX.sub("", str(path or "").replace("\\", "/"))


def is_split_part(path: str) -> bool:
    return bool(_SPLIT_SUFFIX.search(str(path or "").replace("\\", "/")))


def _order_key(path: str) -> tuple[int, int, str]:
    """The unsplit base file first, then parts by their first case number."""
    match = _FIRST_NUMBER.search(path)
    return (0, 0, path) if not is_split_part(path) else (1, int(match.group(1)) if match else 0, path)


def _split_header(source: str) -> tuple[list[str], list[str], str] | None:
    """(leading comments, [import statements], body) for one file."""
    lines = source.replace("\r\n", "\n").split("\n")
    comments: list[str] = []
    imports: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped.startswith("import ") or stripped.startswith("import{"):
            statement = [lines[i]]
            while not re.search(r"""from\s*['"][^'"]+['"]\s*;?\s*$|^import\s*['"][^'"]+['"]\s*;?\s*$""",
                                statement[-1].strip()) and i + 1 < len(lines):
                i += 1
                statement.append(lines[i])
                if len(statement) > 60:
                    return None
            imports.append("\n".join(statement).strip())
        elif not stripped or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
            if not imports:
                comments.append(lines[i])
        else:
            break
        i += 1
    return comments, imports, "\n".join(lines[i:])


def _blocks(body: str) -> list[str] | None:
    """Top-level statements: a block starts at a column-0 line that is not a closer.

    Relies on formatted code (every generated file is), and the caller's compile check
    is the safety net for anything this misreads. Returns None on unbalanced braces.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in body.split("\n"):
        starts_new = bool(line) and not line[0].isspace() and line[0] not in "})]" \
            and not line.startswith(("//", "/*", "*"))
        if starts_new and current and any(part.strip() for part in current):
            blocks.append("\n".join(current).rstrip())
            current = []
        current.append(line)
    if any(part.strip() for part in current):
        blocks.append("\n".join(current).rstrip())
    joined = "\n".join(blocks)
    if joined.count("{") != joined.count("}") or joined.count("(") != joined.count(")"):
        return None
    return [b for b in blocks if b.strip()]


def _merge_imports(import_lists: list[list[str]]) -> list[str]:
    """Union of imports; `{ a }` + `{ b }` from one module becomes `{ a, b }`."""
    named: dict[tuple[bool, str], list[str]] = {}
    order: list[tuple[bool, str]] = []
    other: list[str] = []
    bound: dict[str, str] = {}
    for statements in import_lists:
        for statement in statements:
            m = _NAMED_IMPORT.match(statement)
            if not m:
                # `import path from 'path'` and `import * as path from 'path'` bind the
                # SAME local name; keeping both is "Duplicate identifier". First wins.
                single = _SINGLE_BINDING.match(statement)
                if single:
                    local = single.group(1) or single.group(2)
                    if local in bound:
                        continue
                    bound[local] = statement
                if statement not in other:
                    other.append(statement)
                continue
            key = (bool(m.group(1)), m.group(4))
            if key not in named:
                named[key] = []
                order.append(key)
            for name in (n.strip() for n in m.group(2).split(",")):
                if name and name not in named[key]:
                    named[key].append(name)
    merged = [f"import {'type ' if is_type else ''}{{ {', '.join(names)} }} from '{module}';"
              for (is_type, module), names in ((k, named[k]) for k in order)]
    return other + merged


def merge(parts: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    """Merge `parts` ([{path, content}]) into one source.

    Returns (merged_source, None) or (None, reason). `parts` must already be the files of
    ONE feature; ordering is applied here.
    """
    ordered = sorted(parts, key=lambda p: _order_key(p["path"]))
    headers, block_lists = [], []
    for part in ordered:
        header = _split_header(part.get("content") or "")
        if header is None:
            return None, f"could not read the imports of {part['path']}"
        blocks = _blocks(header[2])
        if blocks is None:
            return None, f"{part['path']} has unbalanced brackets, so its statements cannot be split safely"
        headers.append(header)
        block_lists.append(blocks)

    # File-level settings must be identical in every part (or absent from all).
    settings = [{b for b in blocks if _FILE_LEVEL_SETTINGS.match(b)} for blocks in block_lists]
    if any(s != settings[0] for s in settings[1:]):
        return None, ("the parts set up file-level hooks/options differently; at file level "
                      "they apply to every test, so merging would change what some tests run under")

    declared: dict[str, str] = {}
    seen_blocks: set[str] = set()
    merged_blocks: list[str] = []
    for blocks in block_lists:
        for block in blocks:
            if block in seen_blocks:
                continue
            decl = _DECL.match(block)
            if decl:
                name = next(g for g in decl.groups() if g)
                if name in declared and declared[name] != block:
                    return None, f"`{name}` is declared differently in two parts"
                declared[name] = block
            seen_blocks.add(block)
            merged_blocks.append(block)

    titles = [m.group(2) for block in merged_blocks for m in _TEST_TITLE.finditer(block)]
    duplicates = sorted({t for t in titles if titles.count(t) > 1})
    if duplicates:
        return None, f"two tests share the title {duplicates[0]!r}"

    out = []
    comments = [c for c in headers[0][0] if c.strip()]
    if comments:
        out.append("\n".join(comments))
    imports = _merge_imports([h[1] for h in headers])
    if imports:
        out.append("\n".join(imports))
    out.extend(merged_blocks)
    return "\n\n".join(out).rstrip() + "\n", None


def plan(files: list[dict[str, Any]]) -> list[tuple[str, list[dict[str, Any]]]]:
    """Groups of generated files that are parts of one feature and should be merged.

    Returns [(base_path, [file dicts])] for each spec base with MORE THAN ONE file
    (the unsplit base itself counts as one).
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for generated in files or []:
        path = str(generated.get("path") or "").replace("\\", "/")
        if generated.get("kind", "spec") != "spec" or not re.search(r"\.(?:spec|test)\.[jt]sx?$", path):
            continue
        groups.setdefault(base_path(path), []).append(generated)
    return [(base, group) for base, group in groups.items() if len(group) > 1
            and any(is_split_part(str(g.get("path"))) for g in group)]


def apply(files: list[dict[str, Any]], repo_root: str | Path, *,
          check: Callable[[Path], tuple[bool, Any]] | None = None,
          ) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """Merge every mergeable split group ON DISK and in the returned file list.

    `check(path) -> (ok, detail)` compiles one file; defaults to the Coding Agent's own
    TypeScript syntax check. A merge that does not compile is undone completely - the
    parts are left exactly as they were - so this can only ever leave the workspace as
    good as it found it.

    Returns (new_files, report) with report = {"merged": [...], "kept_split": [...]}.
    """
    from agents.artifact_io import resolve_within, write_text_file

    if check is None:
        from agents.ts_check import _SYNTAX_FAIL, _check_syntax_status

        def default_check(target: Path) -> tuple[bool, Any]:
            status, detail = _check_syntax_status(target, repo_root=Path(repo_root))
            return status != _SYNTAX_FAIL, detail

        check = default_check

    report: dict[str, list[dict[str, Any]]] = {"merged": [], "kept_split": []}
    result = list(files)
    for base, group in plan(files):
        merged, reason = merge(group)
        if merged is None:
            report["kept_split"].append({"file": base, "reason": reason})
            continue
        target = resolve_within(repo_root, base)
        if target is None:
            report["kept_split"].append({"file": base, "reason": "path escapes the workspace"})
            continue
        prior = target.read_text(encoding="utf-8") if target.is_file() else None
        write_text_file(target, merged)
        ok, detail = check(target)
        if not ok:
            if prior is None:
                target.unlink(missing_ok=True)
            else:
                write_text_file(target, prior)
            report["kept_split"].append(
                {"file": base, "reason": f"the merged file did not compile: {str(detail)[:200]}"})
            continue

        ordered = sorted(group, key=lambda g: _order_key(str(g.get("path"))))
        for part in ordered:
            part_path = str(part.get("path") or "").replace("\\", "/")
            if part_path != base:
                stale = resolve_within(repo_root, part_path)
                if stale is not None:
                    stale.unlink(missing_ok=True)
        ids = []
        for part in ordered:
            for case_id in part.get("test_case_ids") or []:
                if case_id not in ids:
                    ids.append(case_id)
        entry = {**ordered[0], "path": base, "content": merged, "test_case_ids": ids}
        dropped = {id(g) for g in group}
        result = [f for f in result if id(f) not in dropped] + [entry]
        report["merged"].append({"file": base, "from": [str(g.get("path")) for g in ordered]})
    return result, report
