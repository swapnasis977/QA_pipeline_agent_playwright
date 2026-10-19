"""
Resolves which target environment (local/central) a run points at, and
normalizes APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD to match — the
single place that knows about the LOCAL_/CENTRAL_ split, so every other
module keeps reading the plain APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD
names it already reads today (utils/dom_inspector.py, agents/coding_agent.py,
agents/automation_planner.py, the workspace's playwright.config.ts and its
generated specs via inherited subprocess env — see agents/executor.py's
`env = {**os.environ, **env_config}`).

Call resolve_app_env() once, as early as possible in any entry point
(the CLI's entry point, or an agent's own standalone _cli()), before
anything reads APP_BASE_URL/APP_TEST_USER/APP_TEST_PASSWORD.
"""
import os

TargetEnv = str  # "local" | "central"


def get_target_env() -> TargetEnv:
    raw = (os.environ.get("TEST_ENV") or "local").strip().lower()
    if raw not in ("local", "central"):
        raise ValueError(f'TEST_ENV must be "local" or "central", got "{raw}"')
    return raw


#: name -> value this module itself wrote into os.environ, so a second call
#: (every agent entry point calls resolve_app_env()) recognises its own work
#: instead of mistaking it for something the shell exported.
_resolved: dict = {}

_APP_NAMES = ("APP_BASE_URL", "APP_TEST_USER", "APP_TEST_PASSWORD")


def _dotenv_candidates() -> list:
    from pathlib import Path

    return [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]


def _file_values(env_files) -> dict:
    """{name: value} from the .env files the CLI loads (never printed)."""
    from utils.dotenv import parse_env_text

    values: dict = {}
    for path in env_files if env_files is not None else _dotenv_candidates():
        try:
            if path.is_file():
                for k, v in parse_env_text(path.read_text(encoding="utf-8-sig")).items():
                    values.setdefault(k, v)
        except OSError:
            continue
    return values


def resolve_app_env(env_files=None) -> TargetEnv:
    """
    Points os.environ["APP_BASE_URL"/"APP_TEST_USER"/"APP_TEST_PASSWORD"] at
    the LOCAL_/CENTRAL_-prefixed value matching TEST_ENV.

    Precedence follows the CLI's "shell over .env" rule: a plain APP_* that
    was EXPLICITLY exported (it is set and is not what the .env file says) is
    never overwritten; the prefixed value only replaces a plain value that
    came from the .env file or from nowhere. When this run has some prefixed
    values but not all, each missing one falls back to the plain variable and
    that is reported BY NAME (never by value), because the plain value may
    belong to the other environment (a central URL with a local password).
    A run that has adopted none of the prefixed variables is untouched and
    silent. Returns the resolved target env ("local" or "central").
    """
    target = get_target_env()
    prefix = "CENTRAL_" if target == "central" else "LOCAL_"
    from_file = _file_values(env_files)
    some_prefixed = any(os.environ.get(prefix + n) for n in _APP_NAMES)
    for name in _APP_NAMES:
        plain = os.environ.get(name)
        if name in _resolved and plain == _resolved[name]:
            continue  # already resolved by an earlier call
        prefixed = os.environ.get(prefix + name)
        explicit = bool(plain) and from_file.get(name) != plain
        if explicit:
            if prefixed and prefixed != plain:
                print(f"      {name}: exported in the environment, so it wins over "
                      f"{prefix}{name} for TEST_ENV={target}")
            elif not prefixed and some_prefixed:
                print(f"      ⚠️  {prefix}{name} is not set for TEST_ENV={target}; using the "
                      f"exported plain {name}, which may belong to the other environment.")
            continue
        if prefixed:
            os.environ[name] = prefixed
            _resolved[name] = prefixed
        elif some_prefixed and plain:
            print(f"      ⚠️  {prefix}{name} is not set for TEST_ENV={target}; falling "
                  f"back to plain {name}, which may belong to the other environment.")
    return target
