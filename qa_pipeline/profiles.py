"""Profiles — one folder of DATA per application, no code.

This is the separation that makes the pipeline shareable. The test of whether
it holds is concrete and worth stating as a rule:

    Adding a second application must require ZERO changes to qa_pipeline/.
    Only a new folder under profiles/.

If a new app ever needs an engine edit, project knowledge has leaked into the
engine and belongs back here. That is the check to run before accepting any
change to this package.

A profile owns four things:

    project.yaml   configuration — urls, connectors, budgets, gate policy
    input/docs/    the raw documents the brain ingests  (everything YOU put in)
    input/recordings/
                   human `playwright codegen` demonstrations — touchpoint 2
    brain/         the derived ledger (generated, but committed on purpose:
                   it is the accumulated knowledge of every run so far, and
                   throwing it away means re-crawling from nothing)

What the pipeline PRODUCES is not in the profile: per-run artifacts go to
`runs/<app>/<epic>_<story>/` (laid out by utils/run_layout.py) and generated
code to `workspaces/<app>/`. One rule: you edit `input/`, you read `runs/`.
A profile folder from before this layout (`knowledge/`, `recordings/` at its
root) is still read, until `scripts/migrate_layout.py` moves it.

YAML is read through PyYAML when it is installed and through a JSON file
otherwise. PyYAML is not added as a hard requirement because the two agents
that must run with no third-party stack configured (bug_reporter,
jira_uploader) transitively import this module, and a missing optional
formatter should never be what stops a bug report being written.
"""

from __future__ import annotations

import os
from pathlib import Path

from agents.artifact_io import read_json

#: Config filenames tried in order. JSON first is deliberate: it needs no
#: third-party parser, so a profile written as JSON always loads.
CONFIG_NAMES = ("project.json", "project.yaml", "project.yml")

#: Where profiles live relative to the repository root, unless QA_PROFILES_DIR
#: says otherwise.
DEFAULT_PROFILES_DIRNAME = "profiles"

DEFAULTS = {
    "app": {
        "base_url": "",
        "login": {"user_env": "APP_TEST_USER", "password_env": "APP_TEST_PASSWORD"},
    },
    "workspace": "",
    "connectors": {"jira": {}, "zephyr": {}, "figma": {}},
    "gates": {
        "require_human_review": True,
        "strict_coverage": False,
        "require_locator_grounding": True,
        "allow_crawl": True,
        "max_repairs": 2,
    },
    "budgets": {
        "max_run_tokens": 0,
        "max_files_per_run": 40,
        "max_heal_attempts": 2,
        "max_run_seconds": 0,        # heal-stage wall clock; 0 = no limit
        "max_crawls_per_run": 25,
    },
    "execution": {"workers": None, "shard": None, "allure": True},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _load_config(path: Path) -> dict:
    if path.suffix == ".json":
        return read_json(path, default={}) or {}
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            f"{path.name} is YAML but PyYAML is not installed. Either "
            f"`pip install pyyaml`, or rename the file to project.json and "
            f"write it as JSON — both are supported and neither is preferred."
        ) from None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        raise RuntimeError(f"Could not parse {path}: {exc}") from None


class Profile:
    """One application's configuration and directories."""

    def __init__(self, name: str, root: Path, config: dict):
        self.name = name
        self.root = Path(root)
        self.config = _deep_merge(DEFAULTS, config or {})

    # -- directories -------------------------------------------------------

    @property
    def knowledge_dir(self) -> Path:
        """The folder of source documents the Brain ingests.

        Defaults to the profile's own `knowledge/`, which stays the norm: the
        profile is the unit of "one application's data", and keeping the
        documents inside it is what makes a run reproducible from the profile
        alone.

        `TESTCASE_WRITER_KNOWLEDGE_DIR` overrides it for teams whose
        requirement documents live somewhere they cannot be copied out of — a
        synced SharePoint/OneDrive folder, or a shared network drive that is
        the single source of truth. That is a real constraint, and the
        alternative people reach for otherwise is copying files by hand before
        every run, which goes stale silently.
        THE TRADE-OFF, stated plainly because the override hides it: two runs
        of the same profile on different machines can now be grounded in
        different documents, and the ledger accumulates across runs, so a card
        can outlive the document that produced it. Prefer the profile folder
        unless you actually need this.

        A path that does not exist is NOT silently ignored — `ingest`'s own
        "no knowledge directory" message would read as "you have no documents"
        when the truth is "your override points somewhere wrong", so it is
        reported here where the cause is known.
        """
        override = (os.environ.get("TESTCASE_WRITER_KNOWLEDGE_DIR") or "").strip()
        if not override:
            return self._input_dir("docs", "knowledge")
        path = Path(override).expanduser()
        if not path.is_dir():
            fallback = self._input_dir("docs", "knowledge")
            print(f"      ⚠️  TESTCASE_WRITER_KNOWLEDGE_DIR points at {path}, which is "
                  f"not a directory — falling back to {fallback}.")
            return fallback
        return path

    def _input_dir(self, name: str, legacy: str) -> Path:
        """`input/<name>`, or the pre-restructure `<legacy>/` while that is the
        only one that exists. New wins as soon as it is there, so a half-migrated
        profile reads the new folder and never silently mixes the two."""
        new = self.root / "input" / name
        old = self.root / legacy
        if not new.is_dir() and old.is_dir():
            return old
        return new

    @property
    def brain_dir(self) -> Path:
        return self.root / "brain"

    @property
    def recordings_dir(self) -> Path:
        return self._input_dir("recordings", "recordings")

    @property
    def conventions_file(self) -> Path:
        return self.root / "conventions.md"

    @property
    def workspace(self) -> Path:
        """Where generated code lands.

        Kept OUTSIDE the profile on purpose: the generated framework is meant
        to be its own git repository that a team owns and edits by hand, not
        an artifact buried inside configuration.
        """
        configured = self.config.get("workspace") or ""
        if configured:
            path = Path(configured)
            return path if path.is_absolute() else (self.root.parent.parent / configured)
        return self.root.parent.parent / "workspaces" / self.name

    @property
    def context_root(self) -> Path:
        """Per-run artifacts: `runs/<app>/`, one folder per `<epic>_<story>`.

        Per APP, not shared: the old flat `context/` held every application's
        runs side by side, so two apps using the same epic key collided. What
        goes inside a run folder is decided by `utils/run_layout.py`.
        """
        return self.root.parent.parent / "runs" / self.name

    # -- configuration -----------------------------------------------------

    def get(self, dotted: str, default=None):
        """`profile.get("gates.max_repairs")`."""
        node = self.config
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    @property
    def base_url(self) -> str:
        return self.get("app.base_url", "") or os.environ.get("APP_BASE_URL", "")

    def apply_environment(self):
        """Exports the profile's app settings into the process environment.

        Necessary because the reused stages read the PLAIN names: DOM
        inspection, the Coding Agent's low-confidence re-inspection, and the
        Executor's `npx playwright test` subprocess (which inherits this
        process's environment) all look for APP_BASE_URL directly. An
        already-set variable wins, so an explicit `set APP_BASE_URL=...` at
        the shell still overrides the profile — which is what someone
        debugging against a one-off environment expects.
        """
        if self.base_url and not os.environ.get("APP_BASE_URL"):
            os.environ["APP_BASE_URL"] = self.base_url
        for key, value in (self.get("app.env") or {}).items():
            if value is not None and not os.environ.get(key):
                os.environ[key] = str(value)
        # THIS profile's route map. `route_map` can only guess a workspace
        # when exactly one has a routes.json; with two applications it guessed
        # none, so every route silently stopped applying.
        route_file = self.workspace / "routes.json"
        if route_file.is_file() and not os.environ.get("APP_ROUTE_MAP"):
            os.environ["APP_ROUTE_MAP"] = str(route_file)
        # Profile data the stage-logic layer (agents/, which cannot import this
        # package) reads from the environment: slider-name words and an optional
        # directory of template overrides (e.g. input/templates/sliders.ts).
        words = self.get("generation.slider_words")
        if words and not os.environ.get("QA_SLIDER_WORDS"):
            os.environ["QA_SLIDER_WORDS"] = ",".join(str(w) for w in words)
        templates = self.root / "input" / "templates"
        if templates.is_dir() and not os.environ.get("QA_TEMPLATES_DIR"):
            os.environ["QA_TEMPLATES_DIR"] = str(templates)

    def ensure_dirs(self):
        for path in (self.knowledge_dir, self.brain_dir, self.recordings_dir,
                     self.workspace, self.context_root):
            path.mkdir(parents=True, exist_ok=True)

    def describe(self) -> str:
        return (f"profile '{self.name}'\n"
                f"  config     {self.root}\n"
                f"  knowledge  {self.knowledge_dir}\n"
                f"  brain      {self.brain_dir}\n"
                f"  recordings {self.recordings_dir}\n"
                f"  workspace  {self.workspace}\n"
                f"  base_url   {self.base_url or '(unset)'}")

    def __repr__(self):
        return f"<Profile {self.name!r} at {self.root}>"


def profiles_dir(start: Path = None) -> Path:
    override = os.environ.get("QA_PROFILES_DIR")
    if override:
        return Path(override)
    base = Path(start) if start else Path(__file__).resolve().parent.parent
    return base / DEFAULT_PROFILES_DIRNAME


def load(name: str, start: Path = None) -> Profile:
    """Loads a profile by name, or by path if `name` points at a directory."""
    candidate = Path(name)
    root = candidate if candidate.is_dir() else profiles_dir(start) / name

    if not root.is_dir():
        available = list_profiles(start)
        raise FileNotFoundError(
            f"No profile {name!r} at {root}. "
            + (f"Available: {', '.join(available)}." if available else
               f"Create one with: qa-pipeline init-profile {name}"))

    for filename in CONFIG_NAMES:
        config_path = root / filename
        if config_path.is_file():
            return Profile(root.name, root, _load_config(config_path))

    raise FileNotFoundError(
        f"Profile directory {root} has no config file. Expected one of: "
        f"{', '.join(CONFIG_NAMES)}.")


def list_profiles(start: Path = None) -> list:
    directory = profiles_dir(start)
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir()
                  if p.is_dir() and any((p / n).is_file() for n in CONFIG_NAMES))


#: Written by `qa-pipeline init-profile`. Deliberately minimal — a profile
#: that starts with forty commented-out options gets skimmed rather than read,
#: and every value here has a working default in DEFAULTS.
TEMPLATE = """\
{
  "app": {
    "base_url": "https://your-app.example.com"
  },
  "connectors": {
    "jira": { "project": "", "issue_type": "Test" }
  },
  "gates": {
    "require_human_review": true,
    "strict_coverage": false,
    "allow_crawl": true,
    "max_repairs": 2
  },
  "budgets": {
    "max_run_tokens": 0,
    "max_files_per_run": 40,
    "max_crawls_per_run": 25
  },
  "execution": {
    "workers": null,
    "allure": true
  }
}
"""


def init(name: str, start: Path = None) -> Profile:
    """Creates an empty profile skeleton and returns it."""
    root = profiles_dir(start) / name
    if root.exists():
        raise FileExistsError(f"{root} already exists.")
    root.mkdir(parents=True)
    (root / "project.json").write_text(TEMPLATE, encoding="utf-8")
    (root / "input" / "docs").mkdir(parents=True)
    (root / "input" / "recordings").mkdir()
    (root / "brain").mkdir()
    (root / "conventions.md").write_text(
        f"# Coding conventions for {name}\n\n"
        f"House style for generated automation code. Read by the Coding Agent.\n"
        f"Leave empty to use the pipeline defaults.\n", encoding="utf-8")
    (root / "input" / "docs" / "README.md").write_text(
        "# Knowledge inputs\n\n"
        "Drop design docs, Figma exports, screenshots and legacy test sheets here.\n"
        "`qa-pipeline ingest` reads everything in this folder and distils it into\n"
        "typed facts in `../../brain/ledger.json`.\n\n"
        "Supported: .md .txt .csv .json .yaml .docx .pdf .png .jpg\n\n"
        "Re-ingesting is safe and idempotent — cards are merged by natural key,\n"
        "so an unchanged document changes nothing and an edited one updates only\n"
        "what actually changed.\n", encoding="utf-8")
    return load(name, start)
