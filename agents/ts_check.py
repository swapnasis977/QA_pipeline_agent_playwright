"""TypeScript checking for generated files: syntax and type checks via the workspace's own tsc, and hints for a missing-property error.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

import itertools
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from agents.artifact_io import read_json

#: Outcome of a syntax check, so "the file is fine" and "the check never ran"
#: stop being the same value. _check_syntax() flattens these back to its
#: original string contract (error text on FAIL, "" otherwise) for callers
#: that only care about rejection; _check_syntax_status() exposes the
#: distinction for callers where an unverified file is itself a risk (see
#: self_healer._apply_patch, which overwrites an already-running file).
_SYNTAX_PASS = "pass"


_SYNTAX_FAIL = "fail"


_SYNTAX_SKIPPED = "skipped"


#: Wait between OSError retries below. The retry loop previously re-spawned
#: IMMEDIATELY, which is the one thing that cannot help here: the failure it
#: exists to absorb is an antivirus scanner holding a lock on a file written
#: microseconds earlier, and all three attempts landed inside the same lock
#: window. Short enough not to slow a healthy run (it is only ever paid on an
#: actual failure), long enough to outlast a real-time scan of one small file.
_SPAWN_RETRY_DELAY_SEC = 0.5


def _run_with_retry(cmd: list, timeout: float, retries: int = 2) -> subprocess.CompletedProcess:
    """subprocess.run with retries on OSError — spawning a fresh child
    process (node/tsc) right after writing the file it inspects can
    transiently fail on Windows (antivirus real-time scanning briefly
    locking a just-created file), observed in practice as an intermittent
    OSError with no other symptom, and more often when several spawns
    happen back-to-back. Retries are cheap insurance against that; a
    persistent OSError still propagates to _check_syntax's own except
    clause, which treats it as "skip the check", not a crash."""
    last_exc = None
    for attempt in range(retries + 1):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except OSError as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(_SPAWN_RETRY_DELAY_SEC)
    raise last_exc


#: tsconfig.json compilerOptions -> equivalent tsc CLI flag, for the subset
#: relevant to whether an otherwise-correct import/module statement type-checks.
_TSCONFIG_OPTION_TO_FLAG = {
    "target": "--target",
    "module": "--module",
    "moduleResolution": "--moduleResolution",
    "jsx": "--jsx",
}


#: Counter making each throwaway typecheck project file unique, so two checks
#: that overlap cannot delete each other's config mid-run.
_TYPECHECK_PROJECT_SEQ = itertools.count()


def _typecheck_project(repo_root: Path, file_path: Path):
    """A throwaway tsconfig that type-checks ONE file *in project context*.

    `tsc` cannot be told about `paths` on the command line — only `baseUrl` —
    so the flag-translation path below can never resolve a path alias. The
    scaffold's own tsconfig defines `@pages/*`, `@fixtures/*`, `@config/*`
    and friends, and the generated code is told to use them, so every page
    object importing `@pages/BasePage` failed this check with TS2307 and was
    then DELETED as if it were broken code. Confirmed against a freshly
    scaffolded workspace.

    Extending the real tsconfig fixes that properly: aliases, ambient types
    and `include`d globals all resolve exactly as they do in a normal build.
    `files` plus an empty `include` narrows the check to this file and what it
    imports, so it stays a per-file check rather than a whole-project one.

    Returns the config path, or None when the repo has no tsconfig to extend.
    """
    if not repo_root:
        return None
    tsconfig = repo_root / "tsconfig.json"
    if not tsconfig.is_file():
        return None

    try:
        relative = file_path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return None  # outside the repo; the caller's confinement check owns that

    project = repo_root / f"tsconfig.qa-typecheck-{os.getpid()}-{next(_TYPECHECK_PROJECT_SEQ)}.json"
    try:
        project.write_text(json.dumps({
            "extends": "./tsconfig.json",
            "compilerOptions": {"noEmit": True, "skipLibCheck": True},
            "files": [relative],
            "include": [],
        }, indent=2), encoding="utf-8")
    except OSError:
        return None
    return project


def _tsc_flags_from_tsconfig(repo_root: Path = None) -> list:
    """Translates repo_root/tsconfig.json's compilerOptions into `tsc` CLI
    flags, so the single-file check below agrees with how the project
    itself actually compiles instead of tsc's much older bare defaults.
    Falls back to this repo's known-good flags if no tsconfig is found or
    it fails to parse — never raises."""
    flags = ["--noEmit", "--skipLibCheck"]
    options = {}
    tsconfig = (repo_root / "tsconfig.json") if repo_root else None
    if tsconfig and tsconfig.is_file():
        try:
            options = read_json(tsconfig, default={}).get("compilerOptions", {})
        except AttributeError:
            options = {}
    for option, flag in _TSCONFIG_OPTION_TO_FLAG.items():
        if option in options:
            flags += [flag, str(options[option])]
    if options.get("esModuleInterop"):
        flags.append("--esModuleInterop")
    if "jsx" not in options:
        flags += ["--jsx", "preserve"]  # this repo's tests use JSX-free .ts, but keep tolerant
    return flags


def _check_syntax(file_path: Path, repo_root: Path = None) -> str:
    """Backward-compatible wrapper over _check_syntax_status(): returns the
    error text when the file genuinely fails to compile, and "" for both
    "passed" and "could not be checked".

    Callers that must not treat an UNCHECKED file as a checked one should
    use _check_syntax_status() instead and handle _SYNTAX_SKIPPED.
    """
    status, detail = _check_syntax_status(file_path, repo_root=repo_root)
    return detail if status == _SYNTAX_FAIL else ""


def _check_syntax_status(file_path: Path, repo_root: Path = None) -> tuple:
    """Best-effort syntax check on a just-written generated file, using
    whatever's actually on PATH. Returns (status, detail) where status is
    _SYNTAX_PASS / _SYNTAX_FAIL / _SYNTAX_SKIPPED — never raises; a
    missing/broken toolchain means this check is SKIPPED, not that generated
    code fails closed.

    A syntactically broken file previously only surfaced at Executor time —
    a whole 30-minute test run to discover one typo. This catches that for
    the cost of a local process spawn per file, not another LLM call.

    Deliberately single-file, not project-aware: no tsconfig.json path
    aliases or ambient types are resolved, so a real syntax error is always
    caught, but an import of a path alias the project defines elsewhere can
    ALSO surface as an error here even though the file is fine in context —
    the rejection reason says so explicitly for exactly that reason.
    """
    suffix = file_path.suffix.lower()
    try:
        if suffix in (".js", ".mjs", ".cjs"):
            node = shutil.which("node")
            if not node:
                return _SYNTAX_SKIPPED, "node not found on PATH"
            result = _run_with_retry([node, "--check", str(file_path)], timeout=15)
            if result.returncode == 0:
                return _SYNTAX_PASS, ""
            return _SYNTAX_FAIL, result.stderr.strip()[:500]

        if suffix in (".ts", ".tsx"):
            tsc = shutil.which("tsc")
            cmd = [tsc] if tsc else ([shutil.which("npx"), "tsc"] if shutil.which("npx") else None)
            if not cmd:
                return _SYNTAX_SKIPPED, "neither tsc nor npx found on PATH"

            # Preferred path: check the file inside the real project, so the
            # aliases and ambient types the project defines resolve. See
            # _typecheck_project for the failure this replaces.
            project = _typecheck_project(repo_root, file_path)
            if project is not None:
                try:
                    result = _run_with_retry(cmd + ["-p", str(project)], timeout=60)
                finally:
                    project.unlink(missing_ok=True)
                if result.returncode == 0:
                    return _SYNTAX_PASS, ""
                output = (result.stdout or "").strip()
                # Attribute honestly: a pre-existing error in a file this one
                # merely imports is not a reason to delete THIS file.
                own_name = file_path.name
                if own_name and own_name not in output:
                    return _SYNTAX_SKIPPED, (
                        f"the project does not currently type-check, but no error "
                        f"names {own_name}: {output[:300]}")
                return _SYNTAX_FAIL, output[:500]
            # Mirror the project's own tsconfig.json compilerOptions instead
            # of hardcoded ad-hoc flags: this repo's tsconfig sets
            # esModuleInterop/moduleResolution "node"/module "commonjs" —
            # bare `tsc` with no config falls back to much older defaults,
            # so a perfectly valid `import { test } from '@playwright/test'`
            # or POM import can fail this check even though the file is
            # fine, and the file then gets deleted below as if it were
            # broken. (`tsc --project` can't be combined with an explicit
            # file argument — TS5042 — so the options are translated to
            # flags instead of passed via --project.)
            check_cmd = cmd + _tsc_flags_from_tsconfig(repo_root) + [str(file_path)]
            result = _run_with_retry(check_cmd, timeout=30)
            if result.returncode == 0:
                return _SYNTAX_PASS, ""
            return _SYNTAX_FAIL, (
                "(single-file check — a real syntax error, OR an unresolved "
                "path alias/ambient type this project defines elsewhere; check "
                "manually if uncertain which) " + result.stdout.strip()[:500])
    except subprocess.TimeoutExpired:
        return _SYNTAX_SKIPPED, "the syntax checker timed out"
    except OSError as exc:
        # Observed on Windows when several checks spawn back-to-back; the
        # retries above usually absorb it. Reported as SKIPPED, never as a
        # pass, so a caller that needs a real verdict can tell.
        return _SYNTAX_SKIPPED, f"could not spawn the syntax checker ({exc})"
    # Unrecognized extension (e.g. a POM helper with no test suffix) —
    # nothing to check, which is a pass as far as callers are concerned.
    return _SYNTAX_PASS, ""


#: Path fragments identifying a PAGE OBJECT / helper — the files where a
#: locator belongs. Anything else ending in a test suffix is a spec.
_POM_PATH_MARKERS = ("/pages/", "/poms/", "/fixtures", "/helpers/", "/components/")


#: tsc's "you called a property that isn't there" error. The single most
#: repeated generation failure in this project: a spec imports three recorded
#: page objects and calls a property on the wrong one.
_TS_MISSING_PROPERTY = re.compile(
    r"Property '([^']+)' does not exist on type '([^']+)'")


#: `readonly <name>: Locator;` — how every page object declares one.
_TS_PROPERTY_DECL = "readonly {name}:"


def suggest_property_owner(error_text: str, repo_root) -> str:
    """Turns "property X does not exist on C" into "X lives on D".

    tsc reports only the absence. When the property is real and simply lives
    on a DIFFERENT page object the same spec already imports, that raw message
    sends the repair loop guessing — observed twice in a row on
    VENDOR-TC-001, which called `vendor.connected` when `connected`
    belongs to `RecordedRegisterTc001Page`, imported in the same file as `register`.
    Two repair attempts, no file produced, and a one-identifier fix available
    in the workspace the entire time.

    Returns "" when nothing useful can be said — a property that exists
    nowhere is a genuine invention, and naming the class the error already
    blamed would be noise.
    """
    if not repo_root or not error_text:
        return ""
    misses = _TS_MISSING_PROPERTY.findall(error_text)
    if not misses:
        return ""

    sources = {}
    for path in Path(repo_root).glob("src/pages/**/*.ts"):
        try:
            sources[path.stem] = path.read_text(encoding="utf-8")
        except OSError:
            continue

    hints = []
    for prop, blamed in dict.fromkeys(misses):
        owners = [cls for cls, text in sources.items()
                  if cls != blamed and _TS_PROPERTY_DECL.format(name=prop) in text]
        if owners:
            hints.append(f"'{prop}' is not on {blamed}, but IS declared on "
                         f"{' / '.join(sorted(owners))} — call it on that object "
                         f"instead of inventing it here")
    if not hints:
        return ""
    return ("Property owners (from the workspace): " + "; ".join(hints) + ".")
