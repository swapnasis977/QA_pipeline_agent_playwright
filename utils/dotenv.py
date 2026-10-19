"""
Minimal .env loader for the `run_*_with_env.py` wrapper scripts.

This project has no python-dotenv dependency on purpose (see
USER_GUIDE.md — the pipeline's Python deps are deliberately few), so the
two wrapper scripts each carried their own three-line parser. Both had the
same three problems, and fixing them in two places independently is how they
drifted apart in the first place:

  1. NO STRIPPING. `k, v = line.split("=", 1)` then `os.environ[k] = v` keeps
     every surrounding space, so a perfectly ordinary `.env` line written as
     `APP_BASE_URL = https://host/` set an env var literally named
     `"APP_BASE_URL "` (trailing space) to `" https://host/"` (leading
     space). Nothing downstream reads either — `os.environ.get("APP_BASE_URL")`
     misses the padded name entirely — so DOM inspection reported
     "APP_BASE_URL is not set" against a .env file that plainly set it.
     A trailing space on a URL or an API key is just as invisible and just
     as fatal.

  2. QUOTES KEPT VERBATIM. `.env` files are conventionally quotable, and a
     value containing spaces almost always is (`APP_TEST_PASSWORD="a b c"`).
     The quotes became part of the credential.

  3. A MISSING .env CRASHED with a bare FileNotFoundError traceback rather
     than saying which file it wanted and where.

Deliberately NOT supported: inline comments (`KEY=value  # note`). A `#` is
a legal character in a password, token, or URL fragment, and silently
truncating a credential at one would be far worse than requiring the comment
to go on its own line. Full-line comments work as before.
"""

import os
from pathlib import Path

#: Quote characters stripped from a value when they MATCH at both ends.
_QUOTES = ("'", '"')


def parse_env_text(text: str) -> dict:
    """Parses .env text into {name: value}. Malformed lines are skipped.

    Split out from load_env_file so the parsing rules are testable without
    a real file on disk (see tests/test_dotenv.py).
    """
    env = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        # `export FOO=bar` is valid in a .env meant to also be shell-sourced.
        if line.startswith("export "):
            line = line[len("export "):].lstrip()

        name, _, value = line.partition("=")
        name = name.strip()
        if not name:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in _QUOTES:
            value = value[1:-1]
        env[name] = value
    return env


def load_env_file(path=".env", override: bool = True) -> dict:
    """Loads `path` into os.environ and returns what it set.

    `override=True` (the default) matches what the wrapper scripts have
    always done: the .env file is the run's source of truth, and these
    scripts exist precisely to inject it. Pass override=False to let an
    already-exported shell variable win instead.

    A missing file is a clear, actionable error rather than a traceback —
    it's the single most likely thing to be wrong on a first run.
    """
    env_path = Path(path)
    if not env_path.is_file():
        raise SystemExit(
            f"No '{env_path}' found (looked in {env_path.resolve().parent}).\n"
            f"Copy .env.example to .env and fill in your keys — see USER_GUIDE.md."
        )

    parsed = parse_env_text(env_path.read_text(encoding="utf-8-sig"))
    applied = {}
    for name, value in parsed.items():
        if not override and name in os.environ:
            continue
        os.environ[name] = value
        applied[name] = value
    return applied
