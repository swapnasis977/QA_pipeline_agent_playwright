"""
Mechanical (no-LLM) extraction of real locators from human-recorded
`npx playwright codegen` output — the same principle the design doc calls
for elsewhere in this pipeline: an LLM never invents a locator; it only
calls something a human already proved works. dom_inspector.py covers the
DOM-inspection half of "get a real locator without guessing" (automatic,
but has to guess which element to click/navigate to); this module covers
the other half — a human recorded the exact flow once, so every locator
here is ground truth, not a guess at all.

Workflow this expects:
  1. A human runs `npx playwright codegen $APP_BASE_URL`, performs the
     flow described by one test case, and saves the generated script to
     recordings/<TestCaseID>.ts (or maps an arbitrarily-named file to a
     Test Case ID via recordings/manifest.json — same escape hatch the
     other Playwright framework in this org uses for codegen's own
     ticket-style suggested filenames).
  2. `python scripts/generate_recorded_pages.py --profile <app>  # was: --recordings-dir ./recordings
     --repo-dir ./workspaces/<app>` (or the `doc_ingestor` stage, which
     calls the same function) parses each recording and mechanically writes
     ONE Page Object class per recording into
     <workspace>/src/pages/recorded/<Name>.ts — readonly Locator properties
     only, matching the scaffold's own page-object convention rather than a
     click*()-method convention.
  3. The same run rewrites an AUTO-GENERATED, clearly delimited section of
     <workspace>/POM_SUMMARY.md listing every new class/property — this is
     the ONLY channel coding_agent.py actually reads for "what already
     exists" (existing_pom_summary), so writing new Page Objects with
     nothing pointing Coding Agent at them would be invisible and useless.

Deliberately NOT attempted in this version (documented instead of silently
half-done): merging/deduping locators recorded across MULTIPLE files that
happen to hit the same real screen into one shared class — each recording
gets its own class. A future enhancement could group by the recorded
`page.goto()`/navigation URL the way the other framework's
generate-page-objects.ts does, but that needs tracking navigation across
clicks (not just the initial goto), which this simpler parser doesn't do.
"""

import re
from pathlib import Path

from agents.artifact_io import read_json

#: Matches one `getByX(...)` / `.locator(...)` call with NO nested
#: parentheses in its argument list — covers the vast majority of real
#: codegen output (object-literal options like `{ name: 'X' }` use curly
#: braces, not parens, so they don't break this). A locator chain with a
#: genuinely nested-parens argument (rare in codegen output) won't match
#: and that step is simply skipped — logged, not silently dropped.
#:
#: Parentheses INSIDE a quoted string are fine (`name: 'Save (draft)'`): the
#: argument pattern consumes whole quoted strings before looking at bare
#: parens. `[^()]*` made such a step fail to match and vanish silently.
_ARGS = r"""(?:[^()'"]|'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*")*"""

_LOCATOR_METHOD_RE = re.compile(
    r"getBy(Role|Label|Placeholder|Text|TestId|Title|AltText)\((" + _ARGS + r")\)"
)

#: A raw `.locator('<selector>')` call. `playwright codegen` emits these
#: constantly — for every control it cannot address by role or accessible
#: name — and this parser used to match only the getByX family, so each one
#: produced `role is None` and the step was dropped. Not "logged, not
#: silently dropped" as the note above claims: dropped, silently. In a real
#: recording of this project's registration flow that cost the Start Capture
#: button, both category dropdowns and the annotation controls — i.e. most of
#: what the demonstration existed to prove — out of the highest-confidence
#: source the pipeline has.
_RAW_LOCATOR_RE = re.compile(r"""\.locator\(\s*(?:'([^']+)'|"([^"]+)")""")

#: Either kind of call, so `_extract_display_name` can take the LAST one in a
#: chain whichever family it belongs to.
_ANY_LOCATOR_RE = re.compile(
    _LOCATOR_METHOD_RE.pattern + "|" + _RAW_LOCATOR_RE.pattern
)


_HAS_TEXT_RE = re.compile(r"""hasText:\s*['"]([^'"]+)['"]""")


def _name_from_selector(selector: str) -> str:
    """A human-readable name for a CSS selector, for property naming and for
    the ledger's fuzzy lookup: `#startCapture` -> "startCapture",
    `[id="4000"]` -> "4000", `.icon.camera` -> "icon camera"."""
    attr = re.match(r"""\[\s*[\w-]+\s*=\s*['"]?([^'"\]]+)""", selector or "")
    if attr:
        return attr.group(1).strip()
    words = re.findall(r"[A-Za-z0-9_-]+", selector or "")
    return " ".join(w.replace("-", " ") for w in words) or (selector or "")

#: One full "await <locator chain>.<action>(<arg>)" statement.
_ACTION_RE = re.compile(
    r"await\s+(page(?:\.[\w$]+\(" + _ARGS + r"\))+)\s*\.\s*"
    r"(fill|click|check|uncheck|selectOption|press|hover|dblclick)\s*"
    r"\(\s*(?:['\"]([^'\"]*)['\"])?[^)]*\)"
)

#: `await expect(<locator chain>).<assertion>(...)`
_ASSERT_RE = re.compile(
    r"await\s+expect\(\s*(page(?:\.[\w$]+\(" + _ARGS + r"\))+)\s*\)\s*\.\s*"
    r"(toBeVisible|toBeChecked|toBeEnabled|toBeAttached)\s*\("
)

_GOTO_RE = re.compile(r"page\.goto\(\s*['\"]([^'\"]+)['\"]\s*\)")

_NAME_OPT_RE = re.compile(r"name:\s*(?:['\"]([^'\"]*)['\"]|/([^/]*)/[a-z]*)")

#: A fill/click target whose visible/accessible text looks like a login
#: field or the button that submits one — these are SKIPPED entirely, not
#: turned into a property, because src/fixtures/index.ts's authenticatedPage
#: fixture (backed by src/pages/LoginPage.ts) already handles login.
#: Generating a second, parallel login implementation per recording is
#: exactly the duplication bug already found once in this pipeline's
#: LLM-generated specs — this parser must not reintroduce it mechanically.
_LOGIN_FIELD_RE = re.compile(r"user|email|login|password|passwd", re.I)
_LOGIN_SUBMIT_RE = re.compile(r"^(next|continue|sign ?in|log ?in|submit)$", re.I)

_ROLE_SUFFIX = {
    "button": "Button", "link": "Link", "tab": "Tab", "menuitem": "MenuItem",
    "checkbox": "Checkbox", "radio": "Radio", "combobox": "Dropdown",
    "textbox": "Input", "option": "Option", "heading": "Heading",
    "dialog": "Dialog", "listitem": "ListItem", "row": "Row",
}


def _extract_display_name(locator_chain: str) -> tuple:
    """Returns (roleOrKind, displayName) from the LAST getByX(...) call in
    a locator chain — the one actually being acted on, e.g. for
    `page.locator('.card').getByRole('button', { name: 'Save' })` this
    returns ('button', 'Save'), not something derived from '.card'."""
    matches = list(_ANY_LOCATOR_RE.finditer(locator_chain))
    if not matches:
        return None, None
    last = matches[-1]
    selector = last.group(3) or last.group(4)
    if selector is not None:           # a raw .locator('<selector>') call
        name = _name_from_selector(selector)
        # `page.locator('a').filter({ hasText: 'Member' })` — the filter text
        # is the only thing distinguishing it from every other anchor on the
        # page, so it belongs in the name the ledger is searched by.
        narrowing = _HAS_TEXT_RE.search(locator_chain)
        if narrowing:
            name = f"{name} {narrowing.group(1)}".strip()
        return "locator", name
    kind, args = last.group(1), last.group(2)
    if kind == "Role":
        role_match = re.match(r"\s*['\"]([^'\"]+)['\"]", args)
        role = role_match.group(1) if role_match else "element"
        name_match = _NAME_OPT_RE.search(args)
        name = None
        if name_match:
            name = name_match.group(1) if name_match.group(1) is not None else name_match.group(2)
        return role, name
    # getByLabel/getByPlaceholder/getByText/getByTestId/getByTitle/getByAltText:
    # the first string argument IS the display name.
    name_match = re.match(r"\s*['\"]([^'\"]+)['\"]", args)
    return kind.lower(), (name_match.group(1) if name_match else None)


def _to_property_name(role: str, name: str) -> str:
    """Best-effort camelCase property name, e.g. ('button', 'Start Capture')
    -> 'startCaptureButton'. Falls back to a role-only name when there's no
    usable display text (e.g. an icon-only button)."""
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if not words:
        words = [role or "element"]
    base = words[0].lower() + "".join(w.capitalize() for w in words[1:])
    if base[:1].isdigit():
        # A selector like `[id="4000"]` yields a name that is a valid English
        # word and an invalid TypeScript identifier — `readonly 4000: Locator;`
        # does not compile, and one such property made the whole recorded page
        # object unusable, which is a silent failure: nothing reads the file
        # until tsc does.
        base = "element" + base
    suffix = _ROLE_SUFFIX.get((role or "").lower(), "")
    if suffix and not base.lower().endswith(suffix.lower()):
        base += suffix
    return base


#: Longest `getByText(...)` string still plausibly ONE control's own label.
#:
#: Above this, codegen has matched a CONTAINER and stringified everything
#: inside it. Such a locator resolves only while every one of those children
#: is rendered, so it silently stops matching the moment a sibling hides -
#: and `getByText` reports that as "not visible" rather than as an error.
#: Observed cost: a recorded
#: `getByText('Non compliant image. 1 attempts remaining. Recapture Reconnect Reset')`
#: became the `if` guarding a generated spec's recapture branch. The live
#: banner reads only "Non compliant image. 1 attempts remaining." because the
#: Recapture/Reconnect/Reset links are hidden at that moment, so the guard was
#: false, the retry never ran, and the test waited out its timeout on a
#: result the app was never going to reach. It looked like a product failure.
_MAX_TEXT_LABEL_CHARS = 60


def _is_container_text(kind: str, name: str) -> bool:
    """Whether this is a region's concatenated text rather than a control's
    own label. See `_MAX_TEXT_LABEL_CHARS`."""
    if (kind or "").lower() != "text":
        return False
    return len(name or "") > _MAX_TEXT_LABEL_CHARS


#: A section header a human wrote into their recording to say which screen
#: the steps under it belong to, e.g.
#:     // --- Capture device (R100) ----------------------------------------
#: Everything up to the next header is attributed to that section.
#:
#: This is the ONLY screen information a recording carries. Dropping it, as
#: this module used to, flattens a whole multi-screen journey into one page
#: object whose members all look equally available at every point in the
#: flow — and downstream that is not a cosmetic loss. The Planner grounded a
#: recapture step, which happens on the capture screen, on the recorded
#: "Next Capture button" that the demonstration only ever touched on the
#: identification RESULT screen, two screens later; the generated test then
#: waited fifteen seconds for a control that could not be there. The header
#: the human had already written said exactly that, and nothing read it.
_SECTION_RE = re.compile(r"^[ \t]*//[ \t]*-{2,}[ \t]*(.+?)[ \t]*-{2,}[ \t]*$", re.M)


def _section_index(text: str) -> list:
    """[(offset, section_name), ...] in file order, for `_section_at`."""
    return [(m.start(), m.group(1).strip()) for m in _SECTION_RE.finditer(text or "")]


def _section_at(index: list, pos: int) -> str:
    """The section a match at `pos` falls under — the last header before it."""
    name = ""
    for start, label in index:
        if start > pos:
            break
        name = label
    return name


def parse_recording(text: str) -> dict:
    """Extracts (locator_chain, action, arg, role, name) tuples from raw
    Playwright codegen output, in recorded order. Login-related steps are
    dropped (see _LOGIN_FIELD_RE docstring above) and reported separately
    so the caller can note what was intentionally skipped, not silently
    lost."""
    steps = []
    skipped_login = []
    dropped = []
    skip_next_submit = False
    sections = _section_index(text)
    matched_spans = []

    for m in _ACTION_RE.finditer(text):
        matched_spans.append(m.span())
        chain, action, arg = m.group(1), m.group(2), m.group(3)
        role, name = _extract_display_name(chain)
        if role is None:
            dropped.append(m.group(0)[:120])
            continue  # locator chain didn't match our simple no-nested-parens pattern

        # Login steps are only the PRELUDE of a recording (before its first real
        # step). Once the flow is under way, an "Email address" / "User role"
        # field is application data, and the Continue/Next after it is a real
        # click; dropping those lost steps from the middle of the flow.
        if not steps and action == "fill" and _LOGIN_FIELD_RE.search(name or ""):
            skipped_login.append((role, name, "login field — handled by authenticatedPage fixture"))
            skip_next_submit = True
            continue
        if not steps and skip_next_submit and action == "click" and _LOGIN_SUBMIT_RE.match((name or "").strip()):
            skipped_login.append((role, name, "login submit — handled by authenticatedPage fixture"))
            skip_next_submit = False
            continue
        skip_next_submit = False

        if _is_container_text(role, name):
            skipped_login.append((role, name, "container text, not a control label "
                                              "- would match only while every child is shown"))
            continue

        steps.append({
            "chain": chain, "action": action, "arg": arg,
            "role": role, "name": name,
            "section": _section_at(sections, m.start()),
        })

    for m in _ASSERT_RE.finditer(text):
        matched_spans.append(m.span())
        chain, assertion = m.group(1), m.group(2)
        role, name = _extract_display_name(chain)
        if role is None or _is_container_text(role, name):
            continue
        steps.append({
            "chain": chain, "action": assertion, "arg": None,
            "role": role, "name": name,
            "section": _section_at(sections, m.start()),
        })

    # Statements neither regex understood used to vanish without a trace.
    for stmt in re.finditer(r"await\s+(?:page|expect\(\s*page)\.[^\n]*", text):
        if not any(a <= stmt.start() < b for a, b in matched_spans):
            if not re.match(r"await\s+page\.(goto|waitFor|screenshot|setViewportSize|evaluate)", stmt.group(0)):
                dropped.append(stmt.group(0)[:120])
    if dropped:
        print(f"      ⚠️  recording_parser: {len(dropped)} recorded statement(s) could not be "
              f"parsed and were dropped, e.g. {dropped[0]!r}")

    goto_match = _GOTO_RE.search(text)
    return {
        "goto_url": goto_match.group(1) if goto_match else None,
        "steps": steps,
        "skipped_login": skipped_login,
        "dropped": dropped,
    }


def build_page_object(class_name: str, parsed: dict) -> tuple:
    """Returns (file_content, [(property_name, locator_chain, actions_seen)]).
    One readonly property per UNIQUE locator chain (deduped) — matches this
    repo's existing hand-written Page Object convention (readonly Locator
    fields assigned in the constructor; see src/pages/*Page.ts),
    not a click*()-method-per-action convention."""
    seen = {}  # locator_chain -> (property_name, set_of_actions, section)
    order = []
    for step in parsed["steps"]:
        chain = step["chain"]
        if chain not in seen:
            prop = _to_property_name(step["role"], step["name"])
            base_prop, n = prop, 2
            existing_props = {v[0] for v in seen.values()}
            while prop in existing_props:
                prop = f"{base_prop}{n}"
                n += 1
            # The section of the FIRST step that touched this locator. A
            # control reused across two sections keeps the one it was
            # introduced on, which is the screen a reader would name it by.
            seen[chain] = (prop, set(), step.get("section", ""))
            order.append(chain)
        seen[chain][1].add(step["action"])

    properties = [(seen[c][0], c, sorted(seen[c][1]), seen[c][2]) for c in order]

    lines = []
    lines.append("import { Page, Locator } from '@playwright/test';")
    lines.append("")
    lines.append("/**")
    lines.append(" * Mechanically generated from a human `npx playwright codegen` recording —")
    lines.append(" * see utils/recording_parser.py. Every locator below was actually exercised")
    lines.append(" * by a real recorded session, not guessed. Login steps are intentionally")
    lines.append(" * excluded — use the authenticatedPage fixture (src/fixtures/index.ts) instead.")
    lines.append(" * Regenerating from an updated recording OVERWRITES this file — hand edits")
    lines.append(" * here will be lost on the next run.")
    lines.append(" */")
    lines.append(f"export class {class_name} {{")
    lines.append("  readonly page: Page;")
    for prop, _chain, _actions, _section in properties:
        lines.append(f"  readonly {prop}: Locator;")
    lines.append("")
    lines.append("  constructor(page: Page) {")
    lines.append("    this.page = page;")
    for prop, chain, _actions, section in properties:
        if section:
            lines.append(f"    // {section}")
        lines.append(f"    this.{prop} = {chain};")
    lines.append("  }")
    lines.append("}")
    lines.append("")
    return "\n".join(lines), properties


def _pascal_case(text: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", text)
    return "".join(w.capitalize() for w in words) or "Recorded"


#: Delimiters for the auto-generated section this module owns inside
#: POM_SUMMARY.md — everything between these two lines is REPLACED on every
#: run; everything outside them (hand-written notes) is left untouched.
_AUTO_SECTION_START = "<!-- AUTO-GENERATED:RECORDED-PAGES:START (utils/recording_parser.py — do not hand-edit this section) -->"
_AUTO_SECTION_END = "<!-- AUTO-GENERATED:RECORDED-PAGES:END -->"


def _render_pom_summary_section(generated: list) -> str:
    """generated: list of dicts with keys test_case_id, class_name,
    import_path, properties (list of (prop, chain, actions)), goto_url,
    skipped_login."""
    if not generated:
        return ""
    parts = [_AUTO_SECTION_START, "", "## Recorded page objects (from real `npx playwright codegen` sessions)", ""]
    for g in generated:
        parts.append(f"### {g['class_name']} (`{g['import_path']}`) — recorded for {g['test_case_id']}")
        parts.append("")
        if g["goto_url"]:
            parts.append(f"Recording started at: `{g['goto_url']}`")
            parts.append("")
        parts.append(f"Import: `import {{ {g['class_name']} }} from '{g['import_path']}';`")
        parts.append("")
        parts.append("Real, actually-exercised locators — reuse these, do not write your own for the")
        parts.append("same elements:")
        parts.append("```ts")
        current_section = None
        for prop, chain, actions, section in g["properties"]:
            if section != current_section:
                current_section = section
                parts.append("")
                parts.append(f"// --- {section or 'unsectioned'} ---")
            note = f"seen: {', '.join(actions)}"
            # An element the demonstration only ever ASSERTED ON is not
            # evidence that it can be clicked, or that it is present at any
            # other point in the flow. Saying so plainly beats the bare
            # "seen: toBeVisible" this used to print, which read as a
            # neutral fact and was routinely taken as a licence to click.
            if actions and all(a.startswith("toBe") for a in actions):
                note += " ONLY — never clicked in the demonstration; do not click it"
            if section:
                note += f"; on: {section}"
            parts.append(f"{prop}: {chain}  // {note}")
        parts.append("```")
        if g["skipped_login"]:
            parts.append("")
            parts.append("Login steps recorded in this session were skipped here on purpose — use "
                          "the `authenticatedPage` fixture (`src/fixtures/index.ts`) instead of "
                          "re-implementing login from this recording.")
        parts.append("")
    parts.append(_AUTO_SECTION_END)
    return "\n".join(parts)


def _update_pom_summary(repo_dir: Path, generated: list) -> None:
    pom_path = repo_dir / "POM_SUMMARY.md"
    existing = pom_path.read_text(encoding="utf-8") if pom_path.exists() else ""
    new_section = _render_pom_summary_section(generated)

    start_idx = existing.find(_AUTO_SECTION_START)
    end_idx = existing.find(_AUTO_SECTION_END)
    if start_idx != -1 and end_idx != -1:
        before = existing[:start_idx].rstrip("\n")
        after = existing[end_idx + len(_AUTO_SECTION_END):].lstrip("\n")
        pieces = [p for p in (before, new_section, after) if p]
        updated = "\n\n".join(pieces) + "\n"
    elif new_section:
        updated = existing.rstrip("\n") + ("\n\n" if existing.strip() else "") + new_section + "\n"
    else:
        return  # nothing recorded and no existing section — leave the file alone
    pom_path.write_text(updated, encoding="utf-8")


def _load_manifest(recordings_dir: Path) -> dict:
    # read_json, not json.loads: manifest.json is HAND-WRITTEN by a human
    # mapping codegen's own filenames to Test Case IDs, so a trailing comma
    # or an unclosed brace is the most likely state for it to be in. That
    # raised from in here and took down the whole generation run — including
    # every recording that has no manifest entry and didn't need it. Now a
    # broken manifest degrades to "no filename mapping" (each recording uses
    # its own stem, the documented default) with a warning naming the file.
    return read_json(recordings_dir / "manifest.json", default={})


def generate(recordings_dir: Path, repo_dir: Path) -> dict:
    """Parses every recording in recordings_dir and writes one Page Object
    per recording into repo_dir/src/pages/recorded/, then rewrites
    POM_SUMMARY.md's auto-generated section. Returns a summary dict for
    logging/testing — {generated: [...], skipped_files: [...]}

    `src/pages/recorded/`, not `tests/pages/recorded/`: the framework
    scaffold puts every page object under `src/pages/` and points the
    `@pages/*` alias there, and `tests/` is Playwright's `testDir`. A
    recorded page object written under `tests/` sat outside the framework it
    was meant to join — unreachable by the alias the generated code is told
    to import through, and inside the directory Playwright scans for specs.
    """
    recordings_dir = Path(recordings_dir)
    repo_dir = Path(repo_dir)
    pages_dir = repo_dir / "src" / "pages" / "recorded"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # manifest.json maps an arbitrarily-named recording file's stem -> the
    # Test Case ID it's actually for (e.g. codegen's own ticket-style
    # suggested filename). A recording without an entry just uses its own
    # filename stem as the Test Case ID.
    filename_to_id = _load_manifest(recordings_dir)

    generated = []
    skipped_files = []

    ts_files = sorted(recordings_dir.glob("*.ts")) if recordings_dir.exists() else []
    for path in ts_files:
        stem = path.stem
        test_case_id = filename_to_id.get(stem, stem)
        text = path.read_text(encoding="utf-8")
        parsed = parse_recording(text)
        if not parsed["steps"]:
            skipped_files.append({"file": str(path), "reason": "no recognizable steps parsed"})
            continue

        class_name = f"Recorded{_pascal_case(test_case_id)}Page"
        content, properties = build_page_object(class_name, parsed)
        out_path = pages_dir / f"{class_name}.ts"
        out_path.write_text(content, encoding="utf-8")

        generated.append({
            "test_case_id": test_case_id,
            "class_name": class_name,
            "import_path": f"@pages/recorded/{class_name}",
            "properties": properties,
            "goto_url": parsed["goto_url"],
            "skipped_login": parsed["skipped_login"],
            "file": str(out_path),
        })

    _update_pom_summary(repo_dir, generated)
    return {"generated": generated, "skipped_files": skipped_files}
