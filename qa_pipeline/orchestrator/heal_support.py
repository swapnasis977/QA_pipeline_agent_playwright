"""Self-heal support: judging a re-run, spotting environment failures, recording and rolling back each attempt.

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

import json
import re as _re
from pathlib import Path

from agents.artifact_io import read_text_file, write_text_file
from qa_pipeline.agents import adapters

#: `import { X } from '@pages/Foo';` / '@components/Bar' / a relative path.
_IMPORT_RE = _re.compile(r"""from\s+['"]([^'"]+)['"]""")


def _imported_page_objects(repo_dir, spec_path: str, spec_source: str) -> dict:
    """{repo-relative path: content} for the page objects a spec imports.

    The Self-Healer is handed the spec, but this pipeline's conventions
    forbid a raw locator in a spec — so the locator that failed is almost
    always in one of these files, and without them the healer can only
    describe the fix it is not allowed to make.

    Resolves the scaffold's path aliases (`@pages/*` -> `src/pages/*`, and
    friends) plus ordinary relative imports. Anything that does not land on a
    real file under the workspace is skipped rather than guessed at.
    """
    aliases = {
        "@pages/": "src/pages/", "@components/": "src/components/",
        "@fixtures/": "src/fixtures/", "@config/": "src/config/",
        "@utils/": "src/utils/", "@data/": "src/data/",
    }
    root = Path(repo_dir)
    out = {}
    for specifier in _IMPORT_RE.findall(spec_source or ""):
        relative = None
        for alias, target in aliases.items():
            if specifier.startswith(alias):
                relative = target + specifier[len(alias):]
                break
        if relative is None and specifier.startswith("."):
            relative = str((Path(spec_path).parent / specifier).as_posix())
        if relative is None:
            continue  # a bare package import (@playwright/test, etc.)
        candidate = root / (relative + ".ts")
        if not candidate.is_file():
            candidate = root / relative
        if not candidate.is_file() or candidate.suffix != ".ts":
            continue
        # The fixtures/env modules are scaffold-owned and must not be rewritten
        # by a heal; only offer the files that actually carry locators.
        key = candidate.relative_to(root).as_posix()
        if key.startswith(("src/fixtures/", "src/config/")):
            continue
        out[key] = read_text_file(candidate)
    return out


def heal_succeeded(rerun: dict, case_ids: list) -> bool:
    """Did the heal fix THE CASES IT WAS HEALING?

    The verification re-run executes a whole spec file, because that is the
    unit Playwright takes — but a spec holds several tests, and heals are
    grouped by file (several cases sharing one broken login flow cost one
    heal, not five). Judging by the PROCESS EXIT CODE therefore answered a
    different question than the one asked: any unrelated failing test in the
    same file marked every grouped case still-failing, and — worse —
    `mark_heal_result` then blocklisted a locator fix that had actually
    worked, so the memory learned the opposite of the truth and would refuse
    that fix forever after.

    Falls back to the exit code only when the re-run's cases cannot be
    attributed at all. That happens when the generated titles don't carry
    their case id, which the Coding Agent already warns about separately
    (coding_agent_title_prefix_warnings.json) — in that state the exit code
    is genuinely the best signal available, and it is the previous behaviour.
    """
    wanted = {cid for cid in case_ids if cid}
    matched = [c for c in rerun.get("cases") or [] if c.get("test_case_id") in wanted]
    if not matched:
        return rerun.get("returncode") == 0
    # Every case we asked about must be IN the re-run. `all()` over only the
    # matched ones passed a group whose second member silently did not run.
    if {c.get("test_case_id") for c in matched} < wanted:
        return False
    # Retries are forced to 0 for the verification run (see stage_heal), so a
    # "flaky" pass cannot occur here; a retry-pass elsewhere still counts.
    return all(c.get("status") == "passed" for c in matched)


#: Substrings of an error message that mean "the environment failed before the test
#: could do anything": the name did not resolve, the page never loaded, the identity
#: provider answered with an error, or the shared route helper could not get started.
#: Matched against the error's HEADLINE (its first few lines) only. The rest of a
#: Playwright message is a code frame and the expected/received values, which can
#: quote any of these words legitimately (a test that asserts an "Internal Server
#: Error" page is a PRODUCT check, not an outage).
_ENVIRONMENT_MARKERS = (
    "net::ERR_", "page.goto: Timeout", "Internal Server Error", "Sign-in ended on",
    "Route step not found", "ECONNREFUSED", "ENOTFOUND", "ECONNRESET",
    "502 Bad Gateway", "503 Service Unavailable", "504 Gateway Time-out",
)


#: Matched against the WHOLE text: thrown from the route helper means the test
#: never got past navigation, wherever in the message that location appears.
_ENVIRONMENT_LOCATIONS = ("utils/routes.ts",)


_HEADLINE_LINES = 3


def environment_failure_reason(message: str, route_retries: int = 0) -> str:
    """Why a failure is the ENVIRONMENT's, or "" when the test itself failed."""
    text = str(message or "").replace("\\", "/")
    headline = " | ".join([ln for ln in text.splitlines() if ln.strip()][:_HEADLINE_LINES])
    for marker in _ENVIRONMENT_MARKERS:
        if marker in headline:
            return f"the environment failed before the test began ({marker.strip()})"
    for marker in _ENVIRONMENT_LOCATIONS:
        if marker in text:
            return f"the environment failed before the test began ({marker.strip()})"
    if route_retries >= 3:
        return ("never reached its screen: the route failed every attempt before the test "
                "began - an application/environment stall, not a defect in the test")
    return ""


def _stalled_in_navigation(report: dict) -> dict:
    """{case id: why} for cases that never got as far as their own steps.

    A test whose route failed every attempt, whose page never loaded, or whose
    sign-in was refused has told us nothing about its own steps. Patching the spec
    cannot fix that — and each heal costs a model call plus a re-run that itself
    waits out the timeout. Observed: one run with 21 of 25 cases "failing" was a
    brief DNS/application outage; none of them was a test defect.
    """
    stalled = {}

    def walk(suites):
        for suite in suites or []:
            for spec in suite.get("specs") or []:
                case_id = adapters._case_id_from_title(spec.get("title") or "")
                for test in spec.get("tests") or []:
                    results = test.get("results") or []
                    if not results or not case_id:
                        continue
                    last = results[-1]
                    if last.get("status") == "passed":
                        continue
                    # Playwright puts annotations on the TEST, and on the result
                    # in some reporter versions; read both.
                    retries = [a for a in (last.get("annotations") or [])
                               + (test.get("annotations") or [])
                               if a.get("type") == "route-retry"]
                    retries = list({a.get("description"): a for a in retries}.values())
                    message = str((last.get("error") or {}).get("message") or "")
                    reason = environment_failure_reason(message, len(retries))
                    if reason:
                        stalled[case_id] = reason
            walk(suite.get("suites"))

    walk(report.get("suites"))
    return stalled


def _raw_error_for(ctx, case_id: str) -> str:
    """Playwright's own error text for one case, from the executor's results."""
    for case in (ctx.state.get("executor") or {}).get("cases") or []:
        if case.get("test_case_id") == case_id:
            return str(case.get("error_message") or "")
    return ""


def _dependent_specs(files, spec_path: str, shared_path: str) -> list:
    """Other generated specs that import the shared file a heal just patched."""
    stem = Path(shared_path).stem
    found = []
    pattern = _re.compile(r"""from\s+['"][^'"]*\b""" + _re.escape(stem) + r"""['"]""")
    for generated in files:
        other = generated.get("path") or ""
        if generated.get("kind") != "spec" or other == spec_path or other in found:
            continue
        if pattern.search(generated.get("content") or ""):
            found.append(other)
    return found


def _regressions(rerun: dict, case_ids: list, baseline: dict) -> list:
    """Cases OTHER than the healed ones that passed in the first run and fail now."""
    was_passing = {c.get("test_case_id") for c in baseline.get("cases") or []
                   if c.get("status") == "passed" and c.get("test_case_id")}
    wanted = set(case_ids)
    return sorted(c.get("test_case_id") for c in rerun.get("cases") or []
                  if c.get("test_case_id") in was_passing
                  and c.get("test_case_id") not in wanted
                  and c.get("status") not in ("passed", "skipped"))


def _heal_diff(repo_dir, originals: dict) -> str:
    """Unified diff of every touched file, original -> what is on disk now."""
    import difflib
    chunks = []
    for rel, before in originals.items():
        after = read_text_file(Path(repo_dir) / rel, default=before)
        if after != before:
            chunks.append("".join(difflib.unified_diff(
                before.splitlines(keepends=True), after.splitlines(keepends=True),
                fromfile=f"a/{rel}", tofile=f"b/{rel}")))
    return "\n".join(chunks)


def _restore_originals(ctx, originals: dict, files: list, content_by_path: dict) -> None:
    """Puts every touched file back as it was before the heal began."""
    for rel, before in originals.items():
        target = Path(ctx.repo_dir) / rel
        if target.is_file() and read_text_file(target) != before:
            write_text_file(target, before)
        content_by_path[rel] = before
        for generated in files:
            if generated.get("path") == rel:
                generated["content"] = before


def _write_heal_record(ctx, case_id: str, attempt: int, status: str, body: str,
                       model_claim: dict = None) -> None:
    """One file per heal attempt under 3_results/heals/: what changed (a diff), the
    verdict, and what the model CLAIMED it changed. heal_outcomes.json alone held only
    the model's own words, so nobody could audit what a heal actually did to a test."""
    safe = _re.sub(r"[^\w.-]+", "_", str(case_id))[:60]
    header = [f"# case: {case_id}", f"# attempt: {attempt}", f"# result: {status}"]
    if model_claim:
        header.append(f"# model claimed: {json.dumps(model_claim, ensure_ascii=False)[:400]}")
    write_text_file(ctx.artifact(f"heals/{safe}.attempt{attempt}.{status}.diff"),
                    "\n".join(header) + "\n\n" + (body or "(no change on disk)") + "\n")
