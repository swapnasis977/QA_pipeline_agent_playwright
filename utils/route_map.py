"""
A declared map from "what a test case is about" to "how to actually get
there in the app" — replacing keyword-guessed navigation.

THE PROBLEM THIS SOLVES. dom_inspector's fallback navigation extracts
4+-letter words from a flow description and fuzzy-clicks them against the
page (up to _MAX_NAV_STEPS, then a word-overlap scoring pass). That cannot
work in the general case, and this project's own comments say why: the test
case says "Face Comparison Tool admin", the app's real tile is named "Image
Editing Tool". No keyword extracted from the case text appears anywhere in
the UI, so the browser never leaves the dashboard — and every "login_page"
/ "not_navigated" grade, every low-confidence re-inspection, and every
locator generated against the wrong screen follows from that one gap.

A route map closes it with information no model can infer and no heuristic
can derive: a human states, once per screen, what it's called and how to
reach it. Inspection then becomes deterministic.

FORMAT — a JSON object of route-name -> route:

    {
      "image-editing": {
        "url": "/tools/image-editing",
        "clicks": ["Image Editing Tool", "Split View"],
        "aliases": ["face comparison", "admin screen", "image compare"]
      }
    }

  - "url"     optional. Appended to APP_BASE_URL and navigated directly —
              by far the most reliable route when the app has addressable
              screens, because it skips clicking entirely.
  - "clicks"  optional. Exact, in-order accessible names to click, in the
              app's OWN vocabulary. Used when a screen isn't addressable
              by URL (a tab, a modal, a wizard step).
  - "expect"  optional. A css selector that must exist on the destination
              screen. A route whose clicks all "succeed" can still land on the
              wrong screen (a duplicate id clicked the wrong button once);
              this is the check that notices.
  - "aliases" optional. The DOMAIN wording your test cases actually use.
              This is the bridge: the case says "Face Comparison Tool admin",
              the alias maps it to the route whose real label is "Image
              Editing Tool".

Matching is deliberately simple and explainable (longest alias wins, then
the route name) — a scoring model here would reintroduce exactly the
guessing this file exists to remove.

WHERE IT LIVES: $APP_ROUTE_MAP, else `routes.json` in the current
directory, else `routes.json` inside the profile's generated workspace.
An optional top-level "_options" object (never a route) carries settings for
the generated route helper; see agents/pom_builder.py `_route_options`.
Entirely optional — with no route map, dom_inspector behaves exactly as it
did before this module existed.
"""

import os
import re
from pathlib import Path

from agents.artifact_io import read_json

#: Searched in order when APP_ROUTE_MAP isn't set. The second entry is a
#: glob rather than a fixed path because the generated workspace is named
#: after the profile (`workspaces/<app>/`), so there is no single directory
#: to hardcode the way the retired `sample-repo/` gave one.
_DEFAULT_LOCATIONS = ("routes.json",)
_WORKSPACE_GLOB = "workspaces/*/routes.json"

_cache = {}

#: Reserved top-level key: settings for how generated code walks the routes
#: (reclick, app_error_regex, cleanup_prefix, step_timeout_ms, arrive_timeout_ms,
#: route_attempts). Any key starting with "_" is reserved and never a route.
OPTIONS_KEY = "_options"


class RouteMap(dict):
    """The routes (name -> route) plus the map's `_options` as `.options`.

    A dict subclass so every existing consumer keeps working unchanged, while
    callers that only hold the loaded map (the route-helper generator) can still
    reach the settings that travel with it."""
    options: dict = {}


def _normalize(text) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip().lower())


def route_map_path() -> Path:
    """The route map file this process will use, whether or not it exists."""
    configured = os.environ.get("APP_ROUTE_MAP", "").strip()
    if configured:
        return Path(configured)
    for candidate in _DEFAULT_LOCATIONS:
        if Path(candidate).is_file():
            return Path(candidate)
    # Exactly one workspace is the ordinary case; with several, an explicit
    # APP_ROUTE_MAP is the only unambiguous answer, so don't guess between them.
    workspaces = sorted(Path().glob(_WORKSPACE_GLOB))
    if len(workspaces) == 1:
        return workspaces[0]
    return Path(_DEFAULT_LOCATIONS[0])


def load(force: bool = False) -> dict:
    """Loads and validates the route map. Returns {} when absent or
    unusable — a malformed map must degrade to the old keyword behavior,
    not break inspection for every case in the run."""
    path = route_map_path()
    key = str(path)
    if not force and key in _cache:
        return _cache[key]

    raw = read_json(path, default={})
    if not isinstance(raw, dict):
        # A list (or any other shape) used to raise AttributeError on `.items()`,
        # against this function's own promise that a malformed map degrades to
        # keyword navigation.
        print(f"⚠️  Route map {path} must be a JSON object of route-name -> route; "
              f"ignoring it.")
        raw = {}
    routes = RouteMap()
    declared_options = raw.get(OPTIONS_KEY)
    if declared_options is not None and not isinstance(declared_options, dict):
        print(f"⚠️  '{OPTIONS_KEY}' in {path} must be an object — ignoring it.")
        declared_options = None
    routes.options = dict(declared_options or {})
    for name, route in (raw or {}).items():
        if str(name).startswith("_"):
            continue  # reserved keys (e.g. "_options") are settings, never routes
        if not isinstance(route, dict):
            print(f"⚠️  Route '{name}' in {path} is not an object — ignoring it.")
            continue
        clicks = route.get("clicks") or []
        if isinstance(clicks, str):
            clicks = [clicks]
        aliases = route.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        if not (route.get("url") or clicks):
            print(f"⚠️  Route '{name}' in {path} has neither 'url' nor 'clicks' — "
                  f"there is no way to reach it, ignoring.")
            continue
        routes[str(name)] = {
            "name": str(name),
            "url": route.get("url") or "",
            "clicks": [str(c) for c in clicks if c],
            "aliases": [str(a) for a in aliases if a],
            # Optional css selector that must exist once the route has been
            # followed — proof the route LANDED, not merely that every click ran.
            "expect": str(route.get("expect") or ""),
        }

    _cache[key] = routes
    return routes


def options(force: bool = False) -> dict:
    """The route map's reserved `_options` object ({} when absent)."""
    return dict(load(force=force).options)


def match(context_text: str, routes: dict = None) -> dict:
    """The route whose name or alias appears in `context_text`, or None.

    Ranked by, in order:

      1. DEPTH — the route with the longest click path wins. One case
         routinely mentions both an area and a screen inside it ("in the
         face comparison tool, verify split view"), and the deeper route is
         the one the test is actually about. Ranking by alias length alone
         picked the BROADER route there, leaving inspection one screen
         short of the target — the same class of near-miss this module
         exists to eliminate, just with a shorter journey. Depth is also
         self-consistent: a deeper route's clicks pass through the
         shallower one on the way.
      2. Longest matching alias — a specific alias ("face comparison tool
         adm") beats a generic one ("comparison").
      3. Route name — purely so two runs against the same case always pick
         the same route.
    """
    routes = load() if routes is None else routes
    if not routes:
        return None

    haystack = _normalize(context_text)
    if not haystack:
        return None

    best_by_route = {}
    for route in routes.values():
        for phrase in [route["name"], *route["aliases"]]:
            needle = _normalize(phrase)
            # Word-boundary match: alias "adm" must not hit inside "html2".
            if needle and re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])",
                                    haystack):
                previous = best_by_route.get(route["name"], (0, route))[0]
                best_by_route[route["name"]] = (max(previous, len(needle)), route)

    if not best_by_route:
        return None
    ranked = sorted(
        best_by_route.items(),
        key=lambda item: (-len(item[1][1]["clicks"]), -item[1][0], item[0]),
    )
    return ranked[0][1][1]


def describe(route: dict) -> str:
    """One-line human summary, for the console and for the snapshot's own
    record of how it got where it got."""
    if not route:
        return "(no declared route matched)"
    parts = [f"route '{route['name']}'"]
    if route.get("url"):
        parts.append(f"url={route['url']}")
    if route.get("clicks"):
        parts.append("clicks=" + " > ".join(route["clicks"]))
    return ", ".join(parts)
