"""`qa-pipeline doctor` — is this machine ready to run the pipeline?

Without it, a missing API key, an absent `npm install` or a wrong base URL
each surfaced late: exit 5 in the middle of `run`, or a Playwright run that
failed on every test. Each check here answers one such question up front, in
one table, with the fix next to the problem.

Rules this module keeps:
  - It prints variable NAMES and yes/no, never a value. `.env` holds real
    credentials, and the output of this command gets pasted into tickets.
  - Offline by default. `--online` adds the checks that touch the network
    (is the application reachable). Nothing here calls a model.
  - Every check is a small function returning (status, detail, fix), so adding
    one is adding a function.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from qa_pipeline.profiles import Profile

OK, WARN, FAIL = "ok", "warn", "fail"

#: One check's answer: (status, what was found, how to fix it).
Check = tuple[str, str, str]
#: One row of the report: (label, status, detail, fix).
Row = tuple[str, str, str, str]

#: Provider -> the variable(s) that must be set for it to work.
_PROVIDER_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "azure_openai": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
    "openrouter": ("OPENROUTER_API_KEY",),
    "ollama": (),
}

#: Profile and workspace names that are safe in a shell, a URL and a CI path.
VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _version(command: list[str]) -> str:
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=15)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except (OSError, subprocess.SubprocessError, IndexError):
        return ""


def check_python() -> Check:
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        return FAIL, f"Python {v.major}.{v.minor} (needs 3.10+)", "Install Python 3.10 or newer."
    return OK, f"Python {v.major}.{v.minor}.{v.micro}", ""


def check_node() -> Check:
    node = shutil.which("node")
    if not node:
        return FAIL, "node not found on PATH", "Install Node.js 18+ (https://nodejs.org)."
    version = _version([node, "--version"])
    match = re.match(r"v(\d+)", version)
    if match and int(match.group(1)) < 18:
        return FAIL, f"Node {version} (needs 18+)", "Upgrade Node.js to 18 or newer."
    return OK, f"Node {version}", ""


def check_npx() -> Check:
    if not shutil.which("npx"):
        return FAIL, "npx not found on PATH", "Reinstall Node.js; npx ships with npm."
    return OK, "npx found", ""


def check_dotenv() -> Check:
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if candidate.is_file():
            return OK, f".env found ({candidate})", ""
    return WARN, "no .env file", "Copy .env.example to .env and fill it in."


def check_model_key(env: Mapping[str, str]) -> Check:
    provider = (env.get("AI_PROVIDER") or "anthropic").strip().lower()
    if provider not in _PROVIDER_KEYS:
        return FAIL, f"AI_PROVIDER={provider!r} is not a known provider", \
            f"Use one of: {', '.join(sorted(_PROVIDER_KEYS))}."
    missing = [name for name in _PROVIDER_KEYS[provider] if not (env.get(name) or "").strip()]
    if missing:
        return FAIL, f"AI_PROVIDER={provider}: {', '.join(missing)} is not set", \
            f"Set {', '.join(missing)} in .env."
    return OK, f"AI_PROVIDER={provider}, credentials present", ""


def check_app_env(env: Mapping[str, str]) -> Check:
    from utils.env_config import get_target_env
    try:
        target = get_target_env()
    except ValueError as exc:
        return FAIL, str(exc), "Set TEST_ENV to local or central."
    prefix = f"{target.upper()}_"
    missing = [n for n in ("APP_BASE_URL", "APP_TEST_USER", "APP_TEST_PASSWORD")
               if not ((env.get(prefix + n) or env.get(n) or "").strip())]
    if missing:
        return WARN, f"TEST_ENV={target}: no value for {', '.join(prefix + m for m in missing)}", \
            f"Set {', '.join(prefix + m for m in missing)} in .env, or the plain names. " \
            f"Needed only to crawl or execute."
    return OK, f"TEST_ENV={target}: application URL and login present", ""


def check_optional_connectors(env: Mapping[str, str]) -> Check:
    present = [n for n in ("JIRA_BASE_URL", "JIRA_API_TOKEN", "FIGMA_API_TOKEN") if (env.get(n) or "").strip()]
    if not present:
        return WARN, "no JIRA/Figma credentials (optional)", \
            "Without them the writer works from documents, a design doc or an import file."
    return OK, f"set: {', '.join(present)}", ""


def check_profile(profile: Profile) -> Check:
    problems, fixes = [], []
    if not VALID_NAME.match(profile.name):
        problems.append(f"name {profile.name!r} has spaces/capitals/symbols")
        fixes.append("Use lowercase letters, digits, - and _ in names (spaces break shell "
                     "commands and CI). To rename: move profiles/<name> and workspaces/<name>, "
                     "then use the new name everywhere.")
    if not profile.base_url:
        problems.append("app.base_url is not set")
        fixes.append("Set app.base_url in project.json.")
    if problems:
        return WARN, "; ".join(problems), " ".join(fixes)
    return OK, f"profile {profile.name!r} loaded", ""


def check_inputs(profile: Profile) -> Check:
    docs = profile.knowledge_dir
    count = sum(1 for p in docs.rglob("*") if p.is_file() and p.name != "README.md") if docs.is_dir() else 0
    ledger = profile.brain_dir / "ledger.json"
    if not count and not ledger.is_file():
        return WARN, f"no documents in {docs} and no ledger yet", \
            "Drop design docs/specs there and run `qa-pipeline ingest`, or give a story/design doc to `run`."
    return OK, f"{count} input document(s); ledger {'present' if ledger.is_file() else 'not built yet'}", ""


def check_workspace(profile: Profile) -> Check:
    ws = profile.workspace
    if not (ws / "package.json").is_file():
        return WARN, f"workspace not scaffolded ({ws})", f"Run `qa-pipeline scaffold --profile {profile.name}`."
    if not (ws / "node_modules").is_dir():
        return FAIL, "workspace dependencies not installed", f"Run `npm install` in {ws}."
    return OK, "workspace scaffolded and installed", ""


def check_browsers() -> Check:
    candidates = [Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or ""),
                  Path(os.environ.get("LOCALAPPDATA") or "") / "ms-playwright",
                  Path.home() / ".cache" / "ms-playwright", Path.home() / "Library" / "Caches" / "ms-playwright"]
    for base in candidates:
        if base.is_dir() and any(p.name.startswith("chromium") for p in base.iterdir()):
            return OK, "Playwright Chromium installed", ""
    return FAIL, "Playwright browsers not installed", "Run `npx playwright install --with-deps chromium`."


def check_reachable(profile: Profile) -> Check:
    import ssl
    import urllib.error
    import urllib.request
    url = profile.base_url
    if not url:
        return WARN, "no base URL to test", "Set app.base_url."
    # Internal QA hosts commonly use a private CA; reachability, not trust, is the question.
    context = ssl.create_default_context()
    context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=10,
                                    context=context) as response:
            return OK, f"{url} answered HTTP {response.status}", ""
    except urllib.error.HTTPError as exc:   # a login wall (401/403) still means "reachable"
        return OK, f"{url} answered HTTP {exc.code}", ""
    except Exception as exc:
        return FAIL, f"{url} unreachable ({type(exc).__name__})", \
            "Check the URL, your VPN/network, and TEST_ENV."


def run_checks(profile: Profile | None = None, *, online: bool = False,
               env: Mapping[str, str] | None = None) -> list[Row]:
    """[(label, status, detail, fix)] in display order."""
    env = os.environ if env is None else env
    plan: list[tuple[str, Callable[[], Check]]] = [("Python", lambda: check_python()), ("Node.js", lambda: check_node()),
            ("npx", lambda: check_npx()), ("Playwright browsers", lambda: check_browsers()),
            (".env file", lambda: check_dotenv()),
            ("Model credentials", lambda: check_model_key(env)),
            ("Application login", lambda: check_app_env(env)),
            ("JIRA / Figma", lambda: check_optional_connectors(env))]
    if profile is not None:
        plan += [("Profile", lambda: check_profile(profile)),
                 ("Input documents", lambda: check_inputs(profile)),
                 ("Test workspace", lambda: check_workspace(profile))]
        if online:
            plan += [("Application reachable", lambda: check_reachable(profile))]
    results: list[Row] = []
    for label, check in plan:
        try:
            status, detail, fix = check()
        except Exception as exc:   # a broken check must not hide the other checks
            status, detail, fix = WARN, f"check failed to run: {type(exc).__name__}: {exc}", ""
        results.append((label, status, detail, fix))
    return results


_ICON = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def render(results: list[Row]) -> str:
    lines = []
    for label, status, detail, fix in results:
        lines.append(f"  {_ICON[status]} {label:22s} {detail}")
        if fix and status != OK:
            lines.append(f"{'':9s}-> {fix}")
    failed = sum(1 for r in results if r[1] == FAIL)
    warned = sum(1 for r in results if r[1] == WARN)
    lines.append("")
    lines.append("  Ready." if not failed and not warned else
                 f"  {failed} problem(s) to fix, {warned} warning(s)." if failed else
                 f"  Ready, with {warned} warning(s).")
    return "\n".join(lines)


def exit_code(results: list[Row]) -> int:
    return 1 if any(r[1] == FAIL for r in results) else 0
