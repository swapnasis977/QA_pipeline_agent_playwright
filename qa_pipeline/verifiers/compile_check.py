"""Does the generated file actually compile?

Delegates to `agents.coding_agent._check_syntax_status`, which already
handles everything hard about this: translating the project's tsconfig into
equivalent `tsc` CLI flags so the single-file check agrees with how the
project really compiles, retrying the subprocess spawn on the Windows
antivirus file-lock OSError, and — most importantly — distinguishing "this
file fails to compile" from "there was no toolchain to check it with".

That last distinction is why this module imports a private function rather
than shelling out itself. `_check_syntax_status` returns a three-state
result for exactly the reason `verifiers.result` has three states, and
reimplementing the check here would mean reimplementing that judgment and
getting it subtly wrong.

Compiling is necessary but nowhere near sufficient, and it is worth being
precise about why: a file can compile perfectly and still be built entirely
on locators that do not exist. That is the dominant first-run failure mode —
"a file that compiled cleanly, ran for 30 seconds, and timed out on a
plausible-but-absent locator". `locator_check` is the gate that catches that
one; this is the cheap gate that catches typos before a browser ever starts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agents.artifact_io import resolve_within

from .result import VerifyResult


def _status_fn() -> tuple[Any, Any, Any]:
    """Imported lazily so this module stays importable when `coding_agent`'s
    own heavier imports (openpyxl, the provider SDKs) are unavailable — the
    same reason `artifact_io` is dependency-free."""
    from agents.coding_agent import _SYNTAX_FAIL, _SYNTAX_PASS, _check_syntax_status
    return _check_syntax_status, _SYNTAX_PASS, _SYNTAX_FAIL


def check_file(generated_file: dict[str, Any], repo_root: str | Path) -> VerifyResult:
    path = str(generated_file.get("path") or "")
    if not path:
        return VerifyResult.failed(
            "compile_check", ["a generated file has no path"],
            repairs=["Every generated file must carry a repo-relative 'path'."])

    # Every path a model produces is untrusted input — `root / '/etc/passwd'`
    # discards root entirely under pathlib semantics. The existing
    # `resolve_within` already encodes this rule; reusing it keeps one
    # definition of "inside the repo" rather than two that can drift.
    target = resolve_within(repo_root, path)
    if target is None:
        return VerifyResult.failed(
            "compile_check",
            [f"{path}: resolves outside the repository root and was not compiled."],
            repairs=[f"The path {path!r} escapes the repository. Use a repo-relative "
                     f"forward-slash path such as 'tests/e2e/my-case.spec.ts'."],
            meta={"file": path})

    if not target.is_file():
        return VerifyResult.skipped(
            "compile_check", f"{path} is not on disk yet", meta={"file": path})

    if target.suffix.lower() not in (".ts", ".tsx", ".js", ".mjs"):
        return VerifyResult.skipped(
            "compile_check", f"{path} is not a TypeScript/JavaScript file",
            meta={"file": path})

    try:
        check_status, PASS_, FAIL_ = _status_fn()
        status, detail = check_status(target, repo_root=Path(repo_root))
    except Exception as exc:
        return VerifyResult.skipped(
            "compile_check", f"{type(exc).__name__}: {exc}", meta={"file": path})

    if status == FAIL_:
        return VerifyResult.failed(
            "compile_check", [f"{path} does not compile:\n{detail}"],
            repairs=[f"The file {path} fails to compile. The compiler says:\n{detail}\n"
                     f"Fix these errors and return the corrected file. Do not change "
                     f"what the test asserts — only what stops it compiling."],
            meta={"file": path})

    if status == PASS_:
        return VerifyResult.passed("compile_check", meta={"file": path})

    return VerifyResult.skipped(
        "compile_check", f"no usable toolchain for {path} ({detail or 'not checked'})",
        meta={"file": path})


def check_files(generated_files: list[dict[str, Any]], repo_root: str | Path) -> list[VerifyResult]:
    return [check_file(f, repo_root) for f in generated_files or []]
