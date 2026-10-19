"""Reading the visible, interactive elements of a page into a compact list with a verified locator for each (role+name, id, test id or CSS).

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""


from __future__ import annotations

import os
import re

#: How many interactive elements one snapshot keeps. 60 was exhausted by the
#: sidebar menu and the toolbar alone on this app's editing screen, so
#: `#brightness`/`#contrast`/`#sharpen` (real, and in the ledger) were never in
#: the snapshot and every page object using them was reported "unverified".
_DEFAULT_SNAPSHOT_ELEMENTS = 120


def _snapshot(page, max_elements: int = None) -> dict:
    """Extracts a compact list of visible interactive elements (role,
    accessible name, tag, and a ready-to-use Playwright locator string) plus
    URL/title — the real data that was missing from the "[PLACEHOLDER]"
    stub. Kept small since this goes straight into an LLM prompt.

    Each element's `locator` is chosen by priority — data-testid > id >
    role+accessible-name > CSS nth-of-type fallback — and verified unique
    against the live DOM (document.querySelectorAll(...).length === 1)
    before being accepted, so callers (coding_agent.py) get a locator that
    actually resolves to exactly this element instead of raw attributes
    they'd have to guess a selector from."""
    if max_elements is None:
        try:
            max_elements = int(os.environ.get("QA_SNAPSHOT_MAX_ELEMENTS", "")
                               or _DEFAULT_SNAPSHOT_ELEMENTS)
        except ValueError:
            max_elements = _DEFAULT_SNAPSHOT_ELEMENTS
    try:
        elements = page.evaluate(
            """(max) => {
                // Headings and aria-labelled regions too: a case that asserts a panel header ("Applicant")
                // needs something to ground to, and none of these is otherwise an interactive element.
                const sel = 'a, button, input, select, textarea, [role], h1, h2, h3, h4, [aria-label]';
                const nodes = Array.from(document.querySelectorAll(sel));
                const isVisible = el => {
                    const r = el.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                // Roles that carry no accessible meaning and only eat the cap.
                const NOISE_ROLES = new Set(['presentation', 'none', 'generic']);
                const visibleAll = nodes.filter(isVisible).filter(el =>
                    !NOISE_ROLES.has((el.getAttribute('role') || '').toLowerCase()));

                // An open modal sits at the END of <body>, so DOM-order truncation cut it
                // first once the sidebar filled the cap. Elements inside the topmost visible
                // dialog now come first, then interactive controls, then headings, then
                // everything else (stable within a tier); DOM order is restored afterwards.
                const modals = Array.from(document.querySelectorAll(
                    '[role="dialog"], [role="alertdialog"], dialog[open], .modal.show, [aria-modal="true"]'))
                    .filter(isVisible);
                const modal = modals.length ? modals[modals.length - 1] : null;
                const INTERACTIVE = /^(a|button|input|select|textarea)$/;
                const tierOf = (el) => {
                    const inModal = modal && modal.contains(el);
                    const tag = el.tagName.toLowerCase();
                    const interactive = INTERACTIVE.test(tag) || (el.getAttribute('role') && !/^(row|cell|gridcell|listitem|list|group|region|table|rowgroup|columnheader)$/.test(el.getAttribute('role')));
                    const heading = /^h[1-4]$/.test(tag);
                    const base = interactive ? 0 : (heading ? 1 : 2);
                    return (inModal ? 0 : 3) + base;
                };
                const visible = visibleAll
                    .map((el, i) => ({ el, i, t: tierOf(el) }))
                    .sort((a, b) => (a.t - b.t) || (a.i - b.i))
                    .slice(0, max)
                    .sort((a, b) => a.i - b.i)
                    .map(x => x.el);
                // Uniqueness of a role locator is judged against EVERYTHING getByRole can
                // see, not only the elements the selector above admits (h5/h6, li, img,
                // tr, nav ... also carry roles and names).
                const ROLE_SEL = sel + ', h5, h6, li, img, tr, td, th, nav, ul, ol, table, form, dialog, main, header, footer, aside';
                const roleNodes = Array.from(document.querySelectorAll(ROLE_SEL)).filter(isVisible);
                // Ids a framework regenerates per render (tooltip73348, react-select-7-input
                // is fine; five+ consecutive digits next to letters is not).
                const isVolatileId = (id) => /[A-Za-z]/.test(id) && /[0-9]{5,}/.test(id);

                const cssEscape = (s) => (window.CSS && CSS.escape) ? CSS.escape(s) : s.replace(/["\\\\]/g, '\\\\$&');
                const isUnique = (selector) => {
                    try { return document.querySelectorAll(selector).length === 1; } catch (e) { return false; }
                };
                // The ARIA role an element has WITHOUT a role attribute (what Playwright's
                // getByRole matches on). Without this only elements that spelled out
                // role="..." could get a role+name locator, so a plain
                // <button aria-label="Zoom In"> fell through to a positional CSS chain
                // (`section:nth-of-type(2) > button:nth-of-type(7)`) that breaks the moment
                // a sibling is added.
                const implicitRole = (el) => {
                    const tag = el.tagName.toLowerCase();
                    const t = (el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'button') return 'button';
                    if (tag === 'a' && el.hasAttribute('href')) return 'link';
                    if (tag === 'select') return el.multiple ? 'listbox' : 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (/^h[1-6]$/.test(tag)) return 'heading';
                    if (tag === 'img') return 'img';
                    if (tag === 'li') return 'listitem';
                    if (tag === 'tr') return 'row';
                    if (tag === 'td') return 'cell';
                    if (tag === 'th') return 'columnheader';
                    if (tag === 'nav') return 'navigation';
                    if (tag === 'ul' || tag === 'ol') return 'list';
                    if (tag === 'table') return 'table';
                    if (tag === 'input') {
                        if (['button', 'submit', 'reset', 'image'].includes(t)) return 'button';
                        if (t === 'checkbox') return 'checkbox';
                        if (t === 'radio') return 'radio';
                        if (t === 'range') return 'slider';
                        if (t === 'number') return 'spinbutton';
                        if (t === 'search') return 'searchbox';
                        if (['', 'text', 'email', 'tel', 'url'].includes(t)) return 'textbox';
                    }
                    return null;
                };
                const roleOf = (el) => el.getAttribute('role') || implicitRole(el);
                const nameOf = (el) => {
                    const labelledBy = el.getAttribute('aria-labelledby');
                    const labelText = (el.labels && el.labels.length
                        ? Array.from(el.labels).map(l => (l.innerText || '').trim()).join(' ')
                        : '')
                        || (labelledBy ? ((document.getElementById(labelledBy) || {}).innerText || '') : '');
                    const alt = el.tagName === 'IMG' ? (el.getAttribute('alt') || '') : '';
                    return (el.getAttribute('aria-label') || labelText.trim() || alt || el.innerText || '').trim().slice(0, 80);
                };
                const cssPath = (el) => {
                    // Fallback locator: tag + nth-of-type chain up to a few
                    // ancestors, only used when no testid/id/role+name works.
                    let node = el, parts = [], depth = 0;
                    while (node && node.nodeType === 1 && depth < 4) {
                        let part = node.tagName.toLowerCase();
                        const parent = node.parentElement;
                        if (parent) {
                            const siblings = Array.from(parent.children).filter(c => c.tagName === node.tagName);
                            if (siblings.length > 1) {
                                part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
                            }
                        }
                        parts.unshift(part);
                        node = parent;
                        depth += 1;
                    }
                    return parts.join(' > ');
                };

                // NEVER read .value for a field that can hold typed input.
                // Redacting only type=password was not enough: _attempt_login
                // FILLS the username field in this very session, so on any
                // inspection that stays on (or returns to) the login screen —
                // exactly the "login_page" grade this module reports — the
                // real APP_TEST_USER was read out of .value and persisted
                // verbatim into context/*/planner_doc.json, an artifact that
                // is committed, attached to tickets and fed to the LLM.
                // Autofill puts other personal data in these fields too.
                // .value is only a LAST-RESORT source for the accessible
                // name here, and for a text input the placeholder is both
                // safer and more useful as a locator hint anyway.
                const VALUE_UNSAFE_TYPES = new Set([
                    'password', 'text', 'email', 'tel', 'url', 'search', 'number', 'hidden',
                ]);
                return visible.map(el => {
                    const type = (el.getAttribute('type') || '').toLowerCase();
                    const isPassword = type === 'password';
                    // A bare <input> with no type attribute defaults to text.
                    const holdsTypedInput = el.tagName === 'TEXTAREA'
                        || (el.tagName === 'INPUT' && (VALUE_UNSAFE_TYPES.has(type) || !type));
                    const safeValue = holdsTypedInput
                        ? (el.getAttribute('placeholder') || '')
                        : (el.value || '');
                    // A form control's accessible name is its <label>, which innerText
                    // does not see (an <input> has none). Without this a radio was
                    // named by its VALUE ("true"/"false"), so a case that says "No"
                    // could never be grounded to it.
                    const labelledBy = el.getAttribute('aria-labelledby');
                    const labelText = (el.labels && el.labels.length
                        ? Array.from(el.labels).map(l => (l.innerText || '').trim()).join(' ')
                        : '')
                        || (labelledBy ? ((document.getElementById(labelledBy) || {}).innerText || '') : '');
                    const rawName = el.getAttribute('aria-label') || labelText.trim() || el.innerText || safeValue || '';
                    const name = isPassword ? '[REDACTED]' : rawName.trim().slice(0, 80);
                    const explicitRole = el.getAttribute('role') || null;
                    const role = explicitRole || implicitRole(el);
                    const id = el.id || null;
                    const testId = el.getAttribute('data-testid') || el.getAttribute('data-test-id') || null;

                    let locator = null, locatorType = null;
                    if (testId) {
                        const candidate = `[data-testid="${cssEscape(testId)}"]`;
                        if (isUnique(candidate)) { locator = candidate; locatorType = 'testid'; }
                    }
                    // A per-render id (#tooltip73348) is not a durable locator: fall through to role/css.
                    if (!locator && id && !isVolatileId(id)) {
                        const candidate = `#${cssEscape(id)}`;
                        if (isUnique(candidate)) { locator = candidate; locatorType = 'id'; }
                    }
                    if (!locator && role && name) {
                        // Playwright role locator syntax, not raw CSS — consumed
                        // as page.getByRole(role, {name}) by callers, not
                        // page.locator(...). locatorType tells them which.
                        //
                        // getByRole matches the name as a case-insensitive SUBSTRING, so
                        // an implicit-role locator is only taken when exactly one visible
                        // element of that role has a name containing this one; otherwise
                        // it would hit a strict-mode violation at run time. Explicit
                        // role="..." is now checked the same way (it used to be skipped).
                        const lc = name.toLowerCase();
                        const ambiguous = roleNodes.filter(o =>
                            roleOf(o) === role && nameOf(o).toLowerCase().includes(lc)).length !== 1;
                        if (!ambiguous) {
                            locator = JSON.stringify({role, name});
                            locatorType = 'role';
                        }
                    }
                    if (!locator) {
                        const candidate = cssPath(el);
                        if (candidate && isUnique(candidate)) { locator = candidate; locatorType = 'css'; }
                    }

                    // State a test asserts on, which a name and a locator cannot carry.
                    // Observed: the model wrote toHaveValue('100') for a slider whose
                    // real range is min=0 max=1 value=1, because the snapshot never
                    // said so — a third of the failing tests were that one guess.
                    // Only for controls that cannot hold typed (personal) input.
                    const isRange = type === 'range';
                    const isChoice = type === 'radio' || type === 'checkbox';
                    const state = {};
                    if (isRange || type === 'number') {
                        state.min = el.getAttribute('min');
                        state.max = el.getAttribute('max');
                        state.step = el.getAttribute('step');
                    }
                    if (isRange) { state.value = el.value; }
                    if (isChoice) { state.checked = !!el.checked; }
                    if (el.tagName === 'SELECT') {
                        state.options = Array.from(el.options).slice(0, 12).map(o => (o.text || '').trim());
                        state.value = el.value;
                    }
                    if (el.disabled) { state.disabled = true; }

                    return {
                        tag: el.tagName.toLowerCase(),
                        role,
                        type: el.getAttribute('type') || null,
                        name,
                        id,
                        testId,
                        locator,
                        locatorType,
                        ...(Object.keys(state).length ? { state } : {}),
                    };
                });
            }""",
            max_elements,
        )
    except Exception as exc:
        elements = []
        print(f"⚠️  DOM snapshot failed: {exc}")

    return {
        "url": page.url,
        "title": _safe(lambda: page.title()),
        "elements": elements,
    }


def _safe(fn, default=""):
    try:
        return fn()
    except Exception:
        return default


#: Matches one Playwright MCP accessibility-snapshot line, e.g.
#: `- textbox "Username" [ref=e3]` or `- button "Sign in" [ref=e7]`. This is
#: the standard playwright-mcp `browser_snapshot` output shape; if your MCP
#: server emits a different format, adjust this regex only — every caller
#: below just consumes the parsed (role, name, ref) tuples.
#:
#: The name may contain escaped quotes (`"Say \"hi\""`), and state tokens may
#: precede the ref (`- checkbox "X" [checked] [ref=e3]`): the old pattern
#: skipped only non-bracket text before `[ref=`, so the ref was lost then.
_MCP_SNAPSHOT_LINE_RE = re.compile(
    r'-\s*(\w[\w-]*)\s+"((?:[^"\\]|\\.)*)"(?:[^\[]*(?:\[(?!ref=)[^\]]*\][^\[]*)*\[ref=([^\]]+)\])?')


def _unescape_mcp_name(raw: str) -> str:
    return re.sub(r'\\(.)', r'\1', raw or "")


def _parse_mcp_snapshot(snapshot_text: str) -> list:
    """Parses playwright-mcp's `browser_snapshot` text into a flat list of
    element dicts shaped like the direct-Playwright path's _snapshot()
    output (tag/role/type/name/id/testId/locator/locatorType), so callers
    (coding_agent.py) can consume either backend's "elements" identically
    instead of branching on which one ran.

    `ref` is kept alongside for THIS module's own re-click use (it's an
    MCP-session-scoped handle, not a durable selector) but every OTHER
    field mirrors the direct path: `locator` is a Playwright role-locator
    payload (`{"role": ..., "name": ...}`, consumed via
    page.get_by_role(role, name=name) — same convention as locatorType
    'role' in _snapshot()) since accessibility-tree snapshots don't expose
    id/data-testid/CSS, only role+accessible-name.

    Uniqueness is checked like the direct path: getByRole matches names as a
    case-insensitive substring, so a (role, name) another element also
    contains gets `"exact": true` when exactly one element IS that name, and
    NO locator (locator/locatorType None) when it can't be told apart."""
    import json as _json

    elements = []
    for line in (snapshot_text or "").splitlines():
        m = _MCP_SNAPSHOT_LINE_RE.search(line)
        if m:
            role, name, ref = m.group(1), _unescape_mcp_name(m.group(2)).strip()[:80], m.group(3)
            elements.append({
                "tag": None,
                "role": role,
                "type": None,
                "name": name,
                "id": None,
                "testId": None,
                "locator": None,
                "locatorType": None,
                "ref": ref,
            })
    named = [(e["role"], e["name"]) for e in elements if e["name"]]
    for e in elements:
        if not e["name"]:
            continue
        low = e["name"].lower()
        contains = sum(1 for r, n in named if r == e["role"] and low in n.lower())
        exact = sum(1 for r, n in named if r == e["role"] and n == e["name"])
        if contains == 1:
            e["locator"] = _json.dumps({"role": e["role"], "name": e["name"]})
        elif exact == 1 and len(e["name"]) < 80:
            e["locator"] = _json.dumps({"role": e["role"], "name": e["name"], "exact": True})
        else:
            continue
        e["locatorType"] = "role"
    return elements


def compact(snapshot: dict) -> dict:
    """A token-lean COPY of a snapshot for putting in a model prompt.

    The in-memory shape (what `_snapshot`/`_parse_mcp_snapshot` return) stays
    as is because verifiers and generators read it; this is only for
    serialising it into a prompt. It

      * drops `raw_snapshot` (the MCP text, already represented by `elements`),
        MCP `ref` handles (session-scoped, meaningless later) and every null/
        empty field;
      * drops `tag`/`type` when a role is present, `id` when the locator is
        `#id`, `testId` when the locator is the test id;
      * for locatorType 'role' drops the `locator` JSON string (it only repeats
        role+name) and keeps `"exact": true` when the payload carried it;
      * collapses identical rows into one carrying `n`.

    Reading a compacted element: `locatorType` 'role' -> getByRole(role, {name});
    'id'/'testid'/'css' -> `locator` as before (testid: getByTestId(testId)).
    Signature: compact(snapshot: dict) -> dict. Never mutates its argument.
    """
    import json as _json

    if not isinstance(snapshot, dict):
        return snapshot
    out = {k: v for k, v in snapshot.items()
           if k not in ("raw_snapshot", "elements") and v not in (None, "", [], {})}
    rows, index = [], {}
    for el in snapshot.get("elements") or []:
        if not isinstance(el, dict):
            continue
        row = {k: v for k, v in el.items() if k != "ref" and v not in (None, "")}
        ltype = row.get("locatorType")
        if ltype == "role":
            try:
                payload = _json.loads(row.get("locator") or "{}")
            except (ValueError, TypeError):
                payload = {}
            row.pop("locator", None)
            if payload.get("exact"):
                row["exact"] = True
        elif ltype == "id":
            row.pop("id", None)
        elif ltype == "testid":
            row.pop("testId", None)
        if row.get("role"):
            row.pop("tag", None)
            row.pop("type", None)
        key = _json.dumps(row, sort_keys=True, default=str)
        if key in index:
            index[key]["n"] = index[key].get("n", 1) + 1
            continue
        index[key] = row
        rows.append(row)
    out["elements"] = rows
    return out


def _origin_of(url: str) -> str:
    """scheme://host[:port] of a URL, with no path or query."""
    from urllib.parse import urlparse

    parsed = urlparse(url or "")
    if not parsed.scheme or not parsed.netloc:
        return (url or "").rstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}"
