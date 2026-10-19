"""Page objects built from the Planner's grounded locators, not by a model.

THE PROBLEM. Specs are generated in independent chunks, and every chunk used to
write (or rewrite) the SHARED page objects itself. Chunk A defined
`brightnessSlider`, chunk B rewrote the same file without it, and chunk C's
spec called `clickRegistrationTile`, which nobody wrote — `tsc` rejected a third
of the files, the gate asked for a regeneration of the whole stage, and the
regeneration made the same mistake. Separately, the gate refuses a page object
whose locator is not the one the Planner grounded to a verified element, so a
model-written page object was rejected for being creative.

THE FIX. Page objects are data here, not prose. Every element the Planner
grounded (screen, logical name, verified locator expression) becomes one
`readonly` property of that screen's class. The model writes SPECS only, against
a member list it is handed, and cannot disagree with the page object because it
does not write it.

This module is pure: plans in, file dicts out. No I/O, no ledger, no model.
"""

from __future__ import annotations

import re
from typing import Any

_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_MAX_WORDS_IN_NAME = 4
_MAX_NAME_LENGTH = 40

#: Words that make a member name long without making it say more. Accessible names
#: carry instructions ("Cancel button to cancel the operation", "Zoom In, Press
#: Enter to activate"), and the whole phrase became `cancelButtonToCancelThe`.
_FILLER = frozenset({
    "the", "a", "an", "to", "of", "for", "and", "with", "this", "that", "is", "are",
    "press", "enter", "activate", "click", "select", "button",
})
# NOT filler: "in", "on", "out", "off", "up", "down" - they ARE the label ("Zoom In",
# "Turn On", "Scroll Down"), and dropping them merges distinct controls into one name.


def _pascal(text: str) -> str:
    words = _WORD_RE.findall(text or "")
    name = "".join(w[:1].upper() + w[1:] for w in words) or "Screen"
    return name if not name[0].isdigit() else f"Screen{name}"


def _camel(text: str) -> str:
    """A property name from an element's logical name.

    Logical names are accessible names, so they can be a paragraph
    ("Contextual Information\\nTransaction Type\\nSelect\\nContinue") or carry
    keyboard hints; only the first line, first few words are kept.
    """
    first_line = (text or "").strip().split("\n")[0]
    first_line = first_line.split(",")[0]
    all_words = _WORD_RE.findall(first_line)
    # Filler is dropped only when something meaningful remains: "Select" or "Button"
    # alone is the whole name of some elements and must not vanish.
    meaningful = [w for w in all_words if w.lower() not in _FILLER]
    words = (meaningful or all_words)[:_MAX_WORDS_IN_NAME]
    if not words:
        return "element"
    name = words[0].lower() + "".join(w[:1].upper() + w[1:] for w in words[1:])
    name = name[:_MAX_NAME_LENGTH]
    return name if not name[0].isdigit() else f"el{name[:1].upper()}{name[1:]}"


def _unique(name: str, taken: set) -> str:
    candidate, n = name, 2
    while candidate in taken or candidate in {"path", "ready", "page", "goto"}:
        candidate = f"{name}{n}"
        n += 1
    taken.add(candidate)
    return candidate


def _is_usable(locator: str) -> bool:
    return isinstance(locator, str) and locator.startswith("page.") and "\n" not in locator


def _ready_index(entries: list) -> int:
    """The element whose presence best says 'this screen has loaded': an id-based
    locator beats a positional css chain, which beats everything else."""
    for i, (_p, _n, locator) in enumerate(entries):
        if re.search(r"""locator\((["'])#[\w-]+\1\)""", locator):
            return i
    return 0


#: `    this.zoomIn = page.getByRole('button', ...);   // Zoom In` -> (prop, locator, comment)
_ASSIGNMENT_RE = re.compile(r"^\s+this\.(\w+)\s*=\s*(.+?);\s*(?://\s*(.*))?$", re.M)
_RESERVED_MEMBERS = {"ready", "path"}


def existing_members(content: str) -> list:
    """[(property, locator, comment)] already declared by a generated page object."""
    return [(m.group(1), m.group(2).strip(), (m.group(3) or "").strip())
            for m in _ASSIGNMENT_RE.finditer(content or "")
            if m.group(1) not in _RESERVED_MEMBERS and _is_usable(m.group(2).strip())]


def build_page_objects(plans: list[dict[str, Any]],
                       existing: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """One generated-file dict per screen that has at least one grounded element.

    Returned entries use the generated_file contract's fields, plus
    `class_name` and `members` ([(property, logical name)]) for the prompt.

    `existing` is {repo-relative path: the file's current content}. A page object is
    REBUILT on every run, and a rebuild from only THIS run's plans used to overwrite
    the file with this run's elements alone - a second story on the same screen
    deleted every member the first story's specs used, and those specs stopped
    compiling. Existing members are now carried over verbatim, and an element whose
    locator is already a member keeps that member's NAME, so a spec written last week
    still means the same thing this week.
    """
    by_screen: dict[str, dict[str, str]] = {}
    for plan in plans or []:
        for element in plan.get("elements") or []:
            screen = str(element.get("screen") or "").strip()
            locator = element.get("locator")
            if not screen or not _is_usable(locator):
                continue
            by_screen.setdefault(screen, {}).setdefault(
                locator, str(element.get("logical_name") or ""))

    pages = []
    for screen in sorted(by_screen):
        class_name = f"{_pascal(screen)}Page"
        taken, entries = set(), []
        path = f"src/pages/{class_name}.ts"
        carried = existing_members((existing or {}).get(path, ""))
        by_locator = {}
        for prop, locator, comment in carried:
            taken.add(prop)
            by_locator[locator] = prop
            entries.append((prop, comment, locator))
        for locator, logical in by_screen[screen].items():
            if locator in by_locator:
                # Same element as before: keep its name, refresh nothing.
                continue
            entries.append((_unique(_camel(logical), taken), logical, locator))
        ready_at = _ready_index(entries)
        ready_prop, _ready_logical, ready_locator = entries[ready_at]

        declarations = "\n".join(f"  readonly {prop}: Locator;" for prop, _n, _l in entries)
        assignments = "\n".join(
            f"    this.{prop} = {locator};   // {' '.join((logical or prop).split())[:80].replace('*/', '')}"
            for prop, logical, locator in entries)
        content = (
            "import { Locator, Page } from '@playwright/test';\n"
            "import { BasePage } from '@pages/BasePage';\n\n"
            f"/**\n * {screen} — GENERATED from the Planner's grounded locators.\n"
            " * Do not edit by hand: it is rebuilt on every Coding Agent run. Add\n"
            " * behaviour in the spec, or ground a new element so it appears here.\n */\n"
            f"export class {class_name} extends BasePage {{\n"
            "  readonly path = '/';\n"
            "  readonly ready: Locator;\n"
            f"{declarations}\n\n"
            "  constructor(page: Page) {\n"
            "    super(page);\n"
            f"    this.ready = {ready_locator};\n"
            f"{assignments}\n"
            "  }\n"
            "}\n")
        pages.append({
            "path": f"src/pages/{class_name}.ts",
            "content": content,
            "kind": "page",
            "test_case_ids": [],
            "locators_used": [locator for _p, _n, locator in entries],
            "class_name": class_name,
            "screen": screen,
            "members": [(prop, logical) for prop, logical, _l in entries],
        })
    return pages


def describe(pages: list, max_members_per_page: int = 80, screens=None) -> str:
    """The member list handed to the model, so specs use only what exists.

    `screens` (optional iterable of screen names, matched case-insensitively
    against each page's `screen`; None = every page, the previous behaviour)
    keeps the list to the screens the current chunk of cases actually uses,
    instead of sending every screen's members with every chunk."""
    if screens is not None:
        wanted = {str(x).strip().lower() for x in screens}
        pages = [p for p in pages or [] if str(p.get("screen") or "").strip().lower() in wanted]
    if not pages:
        return ""
    lines = [
        "Page objects for this feature are ALREADY WRITTEN (generated from verified "
        "locators). Do NOT emit these files. Import them as "
        "`import { X } from '@pages/X';` and use ONLY the members listed — a member "
        "that is not listed does not exist and fails `tsc`. Perform multi-step flows "
        "in the spec by composing these members (click/fill/expect on them) and "
        "BasePage's helpers. Each class takes `page` in its constructor. Do NOT write any other "
        "page object, subclass or wrapper — one that is not listed here is discarded, and a "
        "spec that imports it fails to compile.",
    ]
    for page in pages:
        lines.append(f"\n{page['class_name']}  ({page['path']}) — screen '{page['screen']}'")
        for prop, logical in page["members"][:max_members_per_page]:
            label = " ".join((logical or "").split())[:60]
            lines.append(f"  - {prop}" + (f"  ({label})" if label else ""))
        if len(page["members"]) > max_members_per_page:
            lines.append(f"  - ... {len(page['members']) - max_members_per_page} more")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Route helper — reach any declared screen the way the crawler did
# ---------------------------------------------------------------------------

_ROUTES_TS_HEAD = """import * as fs from 'fs';
import * as path from 'path';
import { Page, Locator, test } from '@playwright/test';

/**
 * GENERATED from the profile's routes.json — do not edit by hand.
 * `followRoute(page, name)` performs exactly the steps the crawler used to
 * reach that screen, so a spec never re-invents how to get there.
 */
interface Route { url?: string; clicks: string[]; expect?: string; }

/* Tunables below come from the `_options` object of the profile's routes.json. */
/** How long one step may wait for its target. In a single-page app the next control
 *  is often not painted right after a navigation or a click. */
const STEP_TIMEOUT = @STEP_TIMEOUT@;
const ARRIVE_TIMEOUT = @ARRIVE_TIMEOUT@;
const ROUTE_ATTEMPTS = @ROUTE_ATTEMPTS@;
/** A click can land and still open nothing; repeating it can help. When the next step's
 *  control has not appeared after RECLICK_AFTER ms, the previous navigation click is
 *  repeated (at most RECLICKS times). RECLICKS = 0 disables this. */
const RECLICK_AFTER = @RECLICK_AFTER@;
const RECLICKS = @RECLICKS@;
/** Routes whose name starts with this are cleanup routes (see cleanupRoute). */
const CLEANUP_PREFIX = @CLEANUP_PREFIX@;
/** Text/regex of an application error dialog worth recording on a retry; null = none. */
const APP_ERROR: RegExp | null = @APP_ERROR@;

export const ROUTES: Record<string, Route> = """

_ROUTES_TS_TAIL = r""";

const ROLES = ['link', 'button', 'tab', 'menuitem'] as const;

/** The first VISIBLE match: ids are not always unique on a page. */
function firstVisible(page: Page, selector: string): Locator {
  return page.locator(selector).filter({ visible: true }).first();
}

async function settle(page: Page): Promise<void> {
  await page.waitForLoadState('domcontentloaded').catch(() => undefined);
  await page.waitForTimeout(500);
}

function pickImage(source: string): string {
  if (!fs.statSync(source).isDirectory()) return source;
  const images = fs.readdirSync(source)
    .filter((f) => /\.(jpe?g|png)$/i.test(f))
    .map((f) => path.join(source, f))
    .filter((f) => fs.statSync(f).size <= 5 * 1024 * 1024)
    // Smallest first: the app may process the upload before it moves on.
    .sort((a, b) => fs.statSync(a).size - fs.statSync(b).size || a.localeCompare(b));
  if (images.length === 0) throw new Error(`No jpg/png under 5 MB in ${source}`);
  return images[0];
}

/** The locator a route step addresses (an `upload=` step has none). */
function stepTarget(page: Page, step: string): Locator {
  if (step.startsWith('css=')) return firstVisible(page, step.slice(4));
  const candidates: Locator[] = [
    ...ROLES.map((role) => page.getByRole(role, { name: step, exact: true })),
    page.getByLabel(step, { exact: true }),
    page.getByTitle(step, { exact: true }),
    page.getByText(step, { exact: true }),
  ];
  return candidates.reduce((all, one) => all.or(one)).filter({ visible: true }).first();
}

/**
 * A step is already behind us when its control is disabled or gone but the NEXT step's
 * control is already on screen. Seen when the app opens the next dialog on its own after the
 * previous click: the button this step would press is then disabled behind that dialog, and
 * waiting for it to become clickable only timed the whole walk out while the dropdown it
 * needed sat open and untouched.
 */
async function alreadyPast(page: Page, step: string, next?: string): Promise<boolean> {
  if (!next || step.startsWith('upload=') || next.startsWith('upload=')) return false;
  const here = stepTarget(page, step);
  const stuck = (await here.count()) === 0 || (await here.isDisabled().catch(() => false));
  if (!stuck) return false;
  // Give the next control a moment to finish appearing (a dialog animates in).
  return stepTarget(page, next).waitFor({ state: 'visible', timeout: 4_000 }).then(() => true, () => false);
}

async function runStep(page: Page, step: string, timeout: number = STEP_TIMEOUT): Promise<void> {
  if (step.startsWith('upload=')) {
    const [selector, source] = step.slice('upload='.length).split('|');
    const file = pickImage(source);
    const target = page.locator(selector).first();
    await target.waitFor({ state: 'attached', timeout: STEP_TIMEOUT });
    const tag = await target.evaluate((el) => el.tagName);
    if (tag === 'INPUT') {
      await target.setInputFiles(file);
    } else {
      const [chooser] = await Promise.all([page.waitForEvent('filechooser'), target.click()]);
      await chooser.setFiles(file);
    }
    // NOT waitForLoadState('networkidle'): a polling app never goes idle. The
    // route's own `expect` marker is the real signal that the screen has arrived.
    return;
  }
  if (step.startsWith('css=')) {
    const target = firstVisible(page, step.slice(4));
    await target.waitFor({ state: 'visible', timeout });
    await target.click();
    await settle(page);
    return;
  }
  const candidates: Locator[] = [
    ...ROLES.map((role) => page.getByRole(role, { name: step, exact: true })),
    page.getByLabel(step, { exact: true }),
    page.getByTitle(step, { exact: true }),
    page.getByText(step, { exact: true }),
  ];
  // Whichever kind of match appears first, WAITING for it rather than testing once.
  const target = candidates.reduce((all, one) => all.or(one)).filter({ visible: true }).first();
  try {
    await target.waitFor({ state: 'visible', timeout });
  } catch {
    throw new Error(`Route step not found on the page: ${step}`);
  }
  await target.click();
  await settle(page);
}

/**
 * Ends whatever a test left open (the steps of every route named with CLEANUP_PREFIX,
 * run from wherever the test finished). Best effort and quick: a test that never
 * opened anything has nothing to cancel, so each step gets only a few seconds.
 */
export async function cleanupRoute(page: Page): Promise<void> {
  if (page.isClosed()) return;
  // Every cleanup route is an independent way to end something a test left open
  // (a wizard transaction, a dialog); a step that is not there only abandons its own route.
  for (const [name, route] of Object.entries(ROUTES)) {
    if (!name.startsWith(@CLEANUP_PREFIX@)) continue;
    let completed = true;
    for (const step of route.clicks) {
      try {
        await runStep(page, step, 5_000);
      } catch {
        completed = false;
        break;
      }
    }
    // One route that ran to its end has done the job; trying the others only waits for
    // controls that are no longer there.
    if (completed) {
      try {
        test.info().annotations.push({ type: 'route-cleanup', description: `ended via ${name}` });
      } catch { /* not inside a test */ }
      return;
    }
  }
}

/** One walk of the route from the app's home screen. */
async function walkRoute(page: Page, name: string): Promise<void> {
  const started = Date.now();
  const timings: string[] = [];
  const route = ROUTES[name];
  if (!route) throw new Error(`Unknown route '${name}'. Known: ${Object.keys(ROUTES).join(', ')}`);
  const home = ROUTES['app-dashboard'];
  const start = route.url ?? (name !== 'app-dashboard' ? home?.url : undefined);
  if (start) await page.goto('/' + start.replace(/^\//, ''));
  await settle(page);
  for (let i = 0; i < route.clicks.length; i++) {
    const step = route.clicks[i];
    const previous = i > 0 ? route.clicks[i - 1] : undefined;
    // Only a plain navigation click is safe to repeat; an upload is not.
    const repeatable = previous !== undefined && !previous.startsWith('upload=');
    if (await alreadyPast(page, step, route.clicks[i + 1])) {
      timings.push(`skipped ${step.slice(0, 40)} (next step already on screen)`);
      continue;
    }
    const at = Date.now();
    let reclicks = 0;
    for (;;) {
      try {
        await runStep(page, step, repeatable && reclicks < RECLICKS ? RECLICK_AFTER : STEP_TIMEOUT);
        break;
      } catch (error) {
        if (!repeatable || reclicks >= RECLICKS) throw error;
        reclicks += 1;
        // Best effort: if the previous control is gone the page moved on, and the loop
        // simply waits for this step again.
        await runStep(page, previous as string, RECLICK_AFTER).catch(() => undefined);
      }
    }
    timings.push(`${Date.now() - at}ms ${step.slice(0, 40)}` + (reclicks ? ` (re-clicked previous step x${reclicks})` : ''));
  }
  // Every click can "succeed" and still land on the wrong screen, or on the right
  // one before it has finished rendering: wait for the screen's own marker.
  if (route.expect) {
    const at = Date.now();
    await page.locator(route.expect).first().waitFor({ state: 'attached', timeout: ARRIVE_TIMEOUT });
    timings.push(`${Date.now() - at}ms expect ${route.expect}`);
  }
  try {
    test.info().annotations.push({
      type: 'route-timing',
      description: `${name}: ${Date.now() - started}ms total | ` + timings.join(' | '),
    });
  } catch { /* not inside a test */ }
}

/**
 * Walks the declared route, retrying the WHOLE walk when it stalls.
 *
 * A click that visibly lands can still open nothing (the page's handlers were not
 * attached yet), and an application error dialog can appear intermittently. Neither
 * is a defect in the test, and a fresh walk from the home screen (which also discards
 * any open dialog) can clear both. Each retry is recorded as a test annotation so
 * flakiness stays visible in the report.
 */
export async function followRoute(page: Page, name: string): Promise<void> {
  let lastError: unknown;
  for (let attempt = 1; attempt <= ROUTE_ATTEMPTS; attempt++) {
    try {
      await walkRoute(page, name);
      return;
    } catch (error) {
      lastError = error;
      // A failed attempt can leave a transaction or dialog open on the server. Retries
      // that walked away from them piled up hundreds of in-progress records, which made
      // the next page slower and the next attempt more likely to fail. End them first.
      await cleanupRoute(page).catch(() => undefined);
      const appError = APP_ERROR
        ? await page.getByText(APP_ERROR).first().isVisible().catch(() => false)
        : false;
      try {
        test.info().annotations.push({
          type: 'route-retry',
          description: `${name}: attempt ${attempt}/${ROUTE_ATTEMPTS} failed`
            + (appError ? ` (app error dialog: ${APP_ERROR})` : '')
            + ` - ${String(error).split('\n')[0].slice(0, 160)}`,
        });
      } catch { /* not inside a test */ }
    }
  }
  throw lastError;
}
"""


_ROUTE_OPTION_DEFAULTS = {
    "step_timeout_ms": 15_000, "arrive_timeout_ms": 30_000, "route_attempts": 3,
    "reclick_after_ms": 6_000, "reclicks": 0, "cleanup_prefix": "zz-cleanup-",
    "app_error_regex": None,
}


def _positive_int(value, default: int, minimum: int = 1) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def _ts_string(text: str) -> str:
    """A single-quoted TypeScript string literal."""
    return "'" + str(text).replace("\\", "\\\\").replace("'", "\\'") + "'"


def _route_options(raw) -> dict:
    """Normalised route-helper settings from a routes.json `_options` object.

    `reclick`: false/absent = off (default); true = after 6 s, at most 2 times;
    {"after_ms": N, "max": M} = custom. Bad values fall back to the defaults."""
    raw = raw if isinstance(raw, dict) else {}
    options = dict(_ROUTE_OPTION_DEFAULTS)
    options["step_timeout_ms"] = _positive_int(raw.get("step_timeout_ms"), options["step_timeout_ms"])
    options["arrive_timeout_ms"] = _positive_int(raw.get("arrive_timeout_ms"), options["arrive_timeout_ms"])
    options["route_attempts"] = _positive_int(raw.get("route_attempts"), options["route_attempts"])
    prefix = raw.get("cleanup_prefix")
    if isinstance(prefix, str) and prefix:
        options["cleanup_prefix"] = prefix
    regex = raw.get("app_error_regex")
    if isinstance(regex, str) and regex:
        options["app_error_regex"] = regex
    reclick = raw.get("reclick")
    if reclick is True:
        options["reclicks"] = 2
    elif isinstance(reclick, dict):
        options["reclick_after_ms"] = _positive_int(
            reclick.get("after_ms"), options["reclick_after_ms"])
        options["reclicks"] = _positive_int(reclick.get("max"), 2, minimum=0)
    return options


def _fill_route_options(template: str, options: dict) -> str:
    import json
    regex = options["app_error_regex"]
    values = {
        "@STEP_TIMEOUT@": str(options["step_timeout_ms"]),
        "@ARRIVE_TIMEOUT@": str(options["arrive_timeout_ms"]),
        "@ROUTE_ATTEMPTS@": str(options["route_attempts"]),
        "@RECLICK_AFTER@": str(options["reclick_after_ms"]),
        "@RECLICKS@": str(options["reclicks"]),
        "@CLEANUP_PREFIX@": _ts_string(options["cleanup_prefix"]),
        "@APP_ERROR@": f"new RegExp({json.dumps(regex)}, 'i')" if regex else "null",
    }
    for token, value in values.items():
        template = template.replace(token, value)
    return template


def build_route_helper(routes: dict) -> dict:
    """`src/utils/routes.ts`: the route map as a typed helper. {} when there are
    no routes, so a profile without a route map is unaffected.

    Settings come from the map's `_options` (see utils/route_map.py): the loaded
    `RouteMap.options`, or a literal `"_options"` key in a plain dict. Without
    them every behaviour that is specific to one application is OFF."""
    import json
    routes = routes or {}
    options = _route_options(getattr(routes, "options", None) or routes.get("_options"))
    routes = {n: r for n, r in routes.items() if not str(n).startswith("_")}
    usable = {name: {"url": r.get("url") or None, "clicks": list(r.get("clicks") or []),
                     "expect": r.get("expect") or None}
              for name, r in (routes or {}).items() if r.get("url") or r.get("clicks")}
    if not usable:
        return {}
    table = json.dumps(
        {n: {k: v for k, v in r.items() if v} | {"clicks": r["clicks"]} for n, r in usable.items()},
        indent=2)
    return {
        "path": "src/utils/routes.ts",
        "content": (_fill_route_options(_ROUTES_TS_HEAD, options) + table
                    + _fill_route_options(_ROUTES_TS_TAIL, options)),
        "kind": "util",
        "test_case_ids": [],
        "locators_used": [],
        "route_names": sorted(usable),
    }


def describe_routes(helper: dict) -> str:
    if not helper:
        return ""
    names = ", ".join(f"'{n}'" for n in helper["route_names"])
    return (
        "Reaching a screen is ALREADY WRITTEN: `import { followRoute } from '@utils/routes';` "
        "then `await followRoute(page, '<route>')` performs the full navigation from the app home "
        f"(login is already handled by the auth setup). Routes: {names}. Do NOT hand-write "
        "the clicks to get to a screen, and do NOT emit src/utils/routes.ts.")

# ---------------------------------------------------------------------------
# Slider helper — range inputs move by STEP, not by percent
# ---------------------------------------------------------------------------

_SLIDERS_TS = """import { Locator, expect } from '@playwright/test';

/**
 * Range sliders, driven by each input's OWN min/max/step (read from the DOM, never
 * assumed). A slider spanning -1..1 in steps of 0.05 shows -100%..100%; one spanning
 * -180..180 in steps of 1 is shown in degrees.
 *
 * Two facts that matter to a test:
 *  - the input's value is in native units ("0.05" for 5%), not in percent;
 *  - an application may record ONE history entry per change EVENT. Each arrow-key press
 *    is its own change, so pressing a key four times may need four Undos, while one
 *    input+change event (what releasing a drag produces) is undone by a single Undo.
 *
 * setSliderPercent therefore makes a single change, like a drag release, snapped to the
 * slider's step. A target that is not a multiple of the step (22% on a 5% step) lands on
 * the nearest step, as a person dragging would get.
 */

interface RangeState {
  min: number;
  max: number;
  step: number;
  value: number;
}

export async function sliderState(slider: Locator): Promise<RangeState> {
  return slider.evaluate((el: HTMLInputElement) => ({
    min: Number(el.min),
    max: Number(el.max),
    step: Number(el.step) || 1,
    value: Number(el.value),
  }));
}

/** The slider's own value for a displayed percentage (or degrees, for rotation). */
function nativeFor(state: RangeState, percent: number): number {
  // A slider spanning more than 2 units (an angle, -180..180) is taken to be in native
  // units already; a narrower one is shown as a percentage of 1.
  const span = state.max - state.min;
  const native = span > 2 ? percent : percent / 100;
  return Math.min(state.max, Math.max(state.min, native));
}

function snap(state: RangeState, native: number): number {
  const steps = Math.round((native - state.min) / state.step);
  const decimals = (String(state.step).split('.')[1] || '').length;
  return Number((state.min + steps * state.step).toFixed(decimals));
}

/** Moves the slider to the displayed `percent` with a single change (one history entry). */
export async function setSliderPercent(slider: Locator, percent: number): Promise<void> {
  const state = await sliderState(slider);
  const target = snap(state, nativeFor(state, percent));
  await slider.focus();
  await slider.evaluate((el: HTMLInputElement, value: number) => {
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    if (setter) setter.call(el, String(value)); else el.value = String(value);
    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));
  }, target);
  await expectSliderPercent(slider, percent);
}

/** Asserts the slider shows `percent` (within half a step, retrying like any expect). */
export async function expectSliderPercent(slider: Locator, percent: number): Promise<void> {
  await expect
    .poll(async () => {
      const state = await sliderState(slider);
      return Math.abs(state.value - nativeFor(state, percent)) <= state.step / 2 + 1e-9;
    }, { message: `slider should show ${percent}` })
    .toBe(true);
}
"""


#: Names that mark a planned element as a range slider when the plan carries no
#: min/max/step state. A profile replaces them (it does not extend them) with
#: project.json `generation.slider_words`, exported as QA_SLIDER_WORDS
#: (comma-separated) by the profile loader.
DEFAULT_SLIDER_WORDS = ('slider', 'range', 'brightness', 'contrast', 'sharpen', 'opacity')


def slider_words(words=None) -> tuple:
    """The words that mark a slider: the argument, else $QA_SLIDER_WORDS, else the defaults."""
    import os
    if words is None:
        words = os.environ.get('QA_SLIDER_WORDS', '')
    if isinstance(words, str):
        words = words.split(',')
    cleaned = tuple(w.strip().lower() for w in (words or ()) if str(w).strip())
    return cleaned or DEFAULT_SLIDER_WORDS


def _mentions_sliders(plans: list, words=None) -> bool:
    wanted = slider_words(words)
    for plan in plans or []:
        for element in plan.get('elements') or []:
            state = element.get('state') or {}
            name = str(element.get('logical_name') or '').lower()
            if (state.get('max') is not None and state.get('step')) or any(
                    word in name for word in wanted):
                return True
    return False


def _slider_template(template=None) -> str:
    """The profile's own sliders.ts when one exists (`<templates dir>/sliders.ts`,
    $QA_TEMPLATES_DIR = the profile's input/templates), else the built-in one."""
    import os
    from pathlib import Path
    if template is None:
        directory = os.environ.get('QA_TEMPLATES_DIR', '').strip()
        candidate = Path(directory) / 'sliders.ts' if directory else None
        if candidate is not None and candidate.is_file():
            return candidate.read_text(encoding='utf-8')
        return _SLIDERS_TS
    return template


def build_slider_helper(plans: list, words=None, template=None) -> dict:
    """`src/utils/sliders.ts`, when any planned element looks like a range slider.

    A test that presses an arrow key N times and expects the input's value to be
    'N' fails on a slider whose real range is -1..1 with step 0.05, where one press
    is 5% and the value is '0.05'. The helper reads each slider's own min/max/step,
    so a spec asks for a percentage and gets the right keystrokes and assertion.

    `words` (optional) overrides the slider-name words, `template` the TypeScript;
    both default to the profile's settings (see `slider_words`, `_slider_template`).
    """
    if not _mentions_sliders(plans, words):
        return {}
    return {'path': 'src/utils/sliders.ts', 'content': _slider_template(template),
            'kind': 'util', 'test_case_ids': [], 'locators_used': []}


def describe_sliders(helper: dict) -> str:
    if not helper:
        return ''
    return (
        "SLIDERS: `import { setSliderPercent, expectSliderPercent } from '@utils/sliders';` - "
        'ALREADY WRITTEN. A range input moves one STEP per arrow key and holds a native value '
        "(e.g. a slider running -1..1 in steps of 0.05, so 5% is ONE step and the value is '0.05', "
        "NOT '5'; and every key press is its OWN undo entry). Never loop key presses against toHaveValue('N'), and never fill() a slider. "
        'Ask for a percentage: `await setSliderPercent(slider, 20)` and '
        '`await expectSliderPercent(slider, 20)`. Elements may carry a `state` object (min, max, '
        'step, value, checked, options, disabled) read from the live screen - use it instead of '
        'guessing a range or a default. Do not emit src/utils/sliders.ts.')
