"""The FINAL automation suite: one representative case per distinct tool behaviour.

Why this exists. The Test Case Writer is run once per JIRA story, and every story is handed the
same design document. So even a "development environment setup" story produced "verify the
Upload mode shows a single image panel", and the 543 per-story cases are mostly the same
tool-wide behaviours written again and again. Automating all of them would mean hours of
duplicate execution against an already slow app, and the Sub-Component labels cannot be used to
group them (there are 68 spellings: "admin", "admin Image Editor", "Toolbar", "Toolbar Controls"...).

So this groups by what the case actually TESTS:

    feature     which part of the tool         (classify)
    controls    which control(s) it names      (CONTROLS: zoom in is not zoom out)
    behaviours  what is checked about them     (BEHAVIOURS: default, minimum, keyboard...)

Cases with the same feature + controls + behaviours and a similar name are duplicates; one is
kept (the best candidate) and the rest are recorded against it as "similar cases merged", so
nothing is silently lost.

Rules this module keeps:
  - A case that already HAS a script is never merged away or dropped: it is an existing test.
  - Two cases naming different controls are never merged, however alike their wording.
  - Manual-only cases and engineering-only cases (unit tests, documentation, audit-log hashing)
    are not UI automation and are excluded, but counted so the totals still add up.
  - Every tool feature with at least one automatable case keeps at least one row.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

# ---------------------------------------------------------------------------
# 1. Which part of the tool is this case about? First match wins, so SPECIFIC rules come first.
# ---------------------------------------------------------------------------

EXCLUDED = "Not UI automation (engineering / back-end)"

FEATURE_RULES: list[tuple[str, str]] = [
    (EXCLUDED, r"unit test|documentation|regression suite|code coverage|\blint|ci/cd|pipeline|"
               r"repository|audit|change log|cryptograph|signed or hashed|\bhashed\b"),
    ("Feature toggle & tool configuration",
     r"feature toggle|toggle config|tool visib|configuration|config object|\bflags?\b"),
    ("Access control & authentication",
     r"permission|access control|cannot launch|not accessible|unauthori|authentication|\brole\b|entitle"),
    ("Save investigation", r"save investigation|investigation state|\bsave\b|round-trip|persist"),
    ("Export image & metadata", r"export"),
    ("Face detection & landmarks", r"landmark|auto-detect|face size|snap-to"),
    ("Toolbar: zoom, pan, rotate, flip, fit, grid", r"\bgrid\b"),     # "Grid toggle overlays a grid"
    ("Overlay mode & blend", r"overlay(?!s\b)|blend|flicker"),
    ("Swipe mode", r"swipe"),
    ("Difference mode", r"difference"),
    ("Sync view", r"\bsync"),
    ("Dual panel & mode switching", r"dual panel|mode switch|view mode|switch(ing)? (to|between|modes)|candidate"),
    ("Annotation, shapes & measurement",
     r"annotation|point marker|line tool|measure|marker|ruler|calibrat|rectangle|ellipse|\bcircle|\bsquare|shape"),
    ("History, undo & redo", r"undo|redo|history"),
    ("Reset", r"\breset\b"),
    ("Keyboard shortcuts & help", r"keyboard|shortcut|help\b|tooltip"),
    ("Accessibility", r"accessib|aria|screen reader|wcag|focus|tab order"),
    ("Tablet & responsive layout", r"tablet|resolution|responsive|viewport|touch|orientation"),
    ("Error & empty states", r"error|empty|invalid|corrupt|unsupported|fail|no image|missing|exception"),
    ("Upload, cancel & exit", r"breadcrumb|\bcancel|file browser|\bexit\b"),
    ("Image adjustments (sliders)", r"adjust|brightness|contrast|sharpen|\bblur\b|opacity|slider|rotation"),
    ("Toolbar: zoom, pan, rotate, flip, fit, grid",
     r"zoom|\bpan\b|rotate|flip|\bfit\b|grid|crop|invert|grayscale|toolbar|navigation|transformation|canvas"),
    ("Sidebar panel hide / show", r"sidebar|hide|docking|collapse|expand"),
    ("Upload, cancel & exit", r"upload|cancel|exit|close|breadcrumb|header"),
    ("Multi-image & image container", r"multiple|multi|container|pre-mounted|dynamic|mount"),
    ("Performance", r"performance|load time|latency|large image"),
    ("GUI layout, branding & consistency", r"layout|branding|gui|\bui\b|visual|consisten|theme|font|colou?r|spacing|icon"),
]
FALLBACK_FEATURE = "General tool behaviour"

# ---------------------------------------------------------------------------
# 2. Which control(s) does it name, and what does it check about them?
# ---------------------------------------------------------------------------

CONTROLS: list[tuple[str, str]] = [
    ("zoom in", r"zoom[- ]?in\b|zoom \+|magnif"), ("zoom out", r"zoom[- ]?out\b"),
    ("pan", r"\bpan\b|hand tool"),
    ("rotate left", r"rotate (left|counter)|counter-?clockwise|anti-?clockwise"),
    ("rotate right", r"rotate right|rotate clockwise|(?<!counter)(?<!counter-)(?<!anti)(?<!anti-)\bclockwise\b"),
    ("flip horizontal", r"flip horizontal|horizontal flip|mirror"), ("flip vertical", r"flip vertical|vertical flip"),
    ("fit to screen", r"fit to|fit-to|\bfit\b|full ?screen|actual size"), ("grid", r"\bgrid"),
    ("invert", r"invert|negative image"), ("grayscale", r"gr[ae]yscale"),
    ("circle / ellipse", r"\bcircle|ellipse"), ("square / rectangle", r"\bsquare|rectangle"),
    ("point marker", r"point marker|\bmarker\b|landmark"), ("line", r"\bline tool|\bline\b"),
    ("measure", r"measure|ruler|calibrat|distance"), ("crop", r"\bcrop"),
    ("eye (show/hide image)", r"\beye\b|show/hide|toggle visibility|hide image"),
    ("undo", r"\bundo"), ("redo", r"\bredo"), ("reset", r"\breset"),
    ("brightness", r"brightness"), ("contrast", r"contrast"), ("sharpen", r"sharpen"),
    ("blur", r"\bblur"), ("opacity", r"opacity"), ("rotation slider", r"rotation\b"),
    ("blend slider", r"blend"), ("flicker", r"flicker"),
    ("sidebar", r"sidebar|hide panel|docking"), ("help", r"\bhelp\b"),
    ("keyboard shortcuts", r"keyboard|shortcut"),
    ("upload", r"\bupload"), ("cancel", r"\bcancel"), ("exit", r"\bexit\b|\bclose\b"),
    ("save", r"\bsave\b"), ("export image", r"export image|export (as )?(png|jpe?g)"),
    ("export metadata", r"metadata"),
    ("swipe", r"swipe"), ("difference", r"difference"), ("sync", r"\bsync"), ("overlay", r"overlay"),
]

BEHAVIOURS: list[tuple[str, str]] = [
    ("default state", r"\bdefault\b|initial state|on load|by default"),
    ("minimum", r"\bmin(imum)?\b|\b0%|lowest"), ("maximum", r"\bmax(imum)?\b|\b100%|highest|upper limit"),
    ("keyboard", r"keyboard|arrow key|key press|shortcut"), ("tooltip", r"tooltip|hover"),
    ("disabled state", r"disabled|greyed|grayed|not clickable"),
    ("negative / error", r"negative|invalid|error|fail|cannot|unable|without"),
    ("persistence", r"persist|retain|remain|preserv|after (reload|refresh|switch)"),
    ("accessibility", r"accessib|aria|screen reader|focus"),
    ("repeated use", r"multiple|repeated|consecutive|successive"), ("tablet", r"tablet|1024|768"),
]

#: A cluster only absorbs a case whose name is at least this similar to the cluster's
#: representative AND which names the same controls/behaviours. Deliberately not too low:
#: a wrong merge hides a behaviour, a missed merge only costs one extra row.
SIMILARITY = 0.5
#: When the controls match but the behaviour labels differ, only near-identical wording merges.
SIMILARITY_DIFFERENT_BEHAVIOUR = 0.8

_STOP = frozenset(["verify", "that", "the", "a", "an", "of", "in", "on", "to", "is", "are", "and", "or", "with", "when", "mode", "view", "button", "tool", "face", "comparison", "displays", "display", "shows", "show", "correctly", "properly", "functions", "function", "works", "work", "applied", "renders", "render"])


def _tokens(name: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9%]+", _clean(name).lower()) if w not in _STOP)


#: Word pairs that flip a case's meaning. "...disabled when empty" and
#: "...enabled when valid" are 0.8+ alike by characters, yet opposite cases.
_OPPOSITES = (("enabled", "disabled"), ("enable", "disable"), ("valid", "invalid"),
              ("with", "without"), ("show", "hide"), ("shown", "hidden"),
              ("visible", "hidden"), ("displayed", "hidden"), ("present", "absent"),
              ("allowed", "blocked"), ("accepts", "rejects"), ("accepted", "rejected"),
              ("success", "failure"), ("checked", "unchecked"), ("selected", "unselected"),
              ("on", "off"), ("open", "close"), ("increase", "decrease"),
              ("empty", "filled"), ("before", "after"))


def opposing_states(a: str, b: str) -> bool:
    """Whether one name says X and the other says its opposite (see _OPPOSITES)."""
    wa = set(re.findall(r"[a-z]+", _clean(a).lower()))
    wb = set(re.findall(r"[a-z]+", _clean(b).lower()))
    for x, y in _OPPOSITES:
        if x in wa and y in wb and x not in wb and y not in wa:
            return True
        if y in wa and x in wb and y not in wb and x not in wa:
            return True
    return False


def similarity(a: str, b: str) -> float:
    """How alike two case names are: the better of word overlap and character overlap."""
    ta, tb = _tokens(a), _tokens(b)
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    chars = difflib.SequenceMatcher(None, _clean(a).lower(), _clean(b).lower()).ratio()
    return max(jaccard, chars)

_PREFIX = re.compile(r"^face comparison tool\s*-\s*", re.I)


def _clean(name: str) -> str:
    return re.sub(r"\s+", " ", _PREFIX.sub("", str(name or ""))).strip()


def classify(name: str, description: str = "") -> str:
    """The tool feature a case belongs to, judged from its name (then its description)."""
    for text in (_clean(name).lower(), str(description or "").lower()):
        for feature, pattern in FEATURE_RULES:
            if re.search(pattern, text):
                return feature
    return FALLBACK_FEATURE


def _labels(table: list[tuple[str, str]], text: str) -> tuple[str, ...]:
    low = text.lower()
    return tuple(label for label, pattern in table if re.search(pattern, low))


def controls_of(name: str) -> tuple[str, ...]:
    return _labels(CONTROLS, _clean(name))


def behaviours_of(name: str) -> tuple[str, ...]:
    return _labels(BEHAVIOURS, _clean(name))


# ---------------------------------------------------------------------------
# 3. Choose the representatives.
# ---------------------------------------------------------------------------

def _priority(row: dict[str, Any]) -> tuple[int, int, int, int, str]:
    """Lower sorts first = better representative."""
    scripted = row.get("Automation Status") == "Automated - script exists"
    has_id = bool(row.get("Automation ID"))
    automated = str(row.get("Execution Type (as written)") or "").lower() == "automated"
    detail = len(str(row.get("Expected Result") or "")) + len(str(row.get("Test Step") or ""))
    return (0 if scripted else 1, 0 if has_id else 1, 0 if automated else 1, -detail,
            str(row.get("Test Case ID") or ""))


# ---------------------------------------------------------------------------
# 2b. Scope: what is on THIS app's screen, and what an existing script already covers.
#
# That is application knowledge, so it lives in the profile (automation_scope.json), never here.
# The engine only applies the rules. First matching rule wins; no match takes the file's default.
# ---------------------------------------------------------------------------

MANUAL_ONLY = "Manual only"
_DEFAULT_DECISIONS = {"automated": "Already automated", "covered": "Covered by existing script",
                      "new": "New script needed", "blocked": "Blocked - needs test data or a tool",
                      "not_on_screen": "Not on this screen",
                      "excluded": "Excluded by decision"}


def load_scope(path: Any) -> dict[str, Any] | None:
    """The profile's automation_scope.json, or None when the profile has none."""
    import json
    from pathlib import Path

    try:
        scope = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    scope["decisions"] = {**_DEFAULT_DECISIONS, **(scope.get("decisions") or {})}
    return scope


_warned_scope: set = set()


def _warn_scope(message: str) -> None:
    """One printed warning per distinct scope-file problem (annotate runs per row)."""
    if message not in _warned_scope:
        _warned_scope.add(message)
        print(f"      ⚠️  automation_scope.json: {message}")


def annotate(rows: list[dict[str, Any]], scope: dict[str, Any] | None) -> None:
    """Sets Scope Decision / Covered By / Decision Note on EVERY row (manual ones included)."""
    for row in rows:
        decisions = (scope or {}).get("decisions") or _DEFAULT_DECISIONS
        if str(row.get("Execution Type (as written)") or "").strip().lower() == "manual":
            row.update({"Scope Decision": MANUAL_ONLY, "Covered By": "", "Decision Note": ""})
            continue
        declined = (scope or {}).get("declined") or {}
        reason = declined.get(str(row.get("Test Case ID") or "")) or declined.get(str(row.get("Automation ID") or ""))
        if reason:     # `qa-pipeline decline` remembered it: a rebuilt sheet must not resurrect the case
            row.update({"Scope Decision": "Declined - not applicable", "Covered By": "", "Decision Note": reason,
                        "Automation Status": "Declined - not applicable"})
            continue
        if row.get("Automation Status") == "Automated - script exists":
            row.update({"Scope Decision": decisions["automated"], "Covered By": "", "Decision Note": ""})
            continue
        default_key = (scope or {}).get("default", "new")
        if default_key not in decisions:
            _warn_scope(f"default decision {default_key!r} is not in 'decisions'; using 'new'")
            default_key = "new"
        key, by, note = default_key, [], ""
        name = _clean(str(row.get("Name") or "")).lower()
        for rule in (scope or {}).get("rules") or []:
            if not isinstance(rule, dict) or not rule.get("pattern"):
                _warn_scope(f"rule {rule!r} has no 'pattern'; skipped")
                continue
            if rule.get("decision") not in decisions:
                _warn_scope(f"rule {rule.get('pattern')!r} has unknown decision "
                            f"{rule.get('decision')!r}; skipped")
                continue
            try:
                hit = re.search(rule["pattern"], name)
            except re.error as exc:
                _warn_scope(f"rule pattern {rule['pattern']!r} is not a valid regex ({exc}); skipped")
                continue
            if hit:
                key, by, note = rule["decision"], rule.get("by") or [], rule.get("note") or ""
                break
        row.update({"Scope Decision": decisions[key], "Covered By": ", ".join(by), "Decision Note": note})


def select(rows: list[dict[str, Any]], scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """From the master sheet's rows, pick the final automation suite.

    Returns {"suite": [...], "excluded_manual": n, "excluded_engineering": n, "merged": n}.
    Each suite row gains: Feature, Control / behaviour, Similar cases merged, Merged case IDs.
    """
    suite: list[dict[str, Any]] = []
    excluded_manual = excluded_engineering = excluded_not_on_screen = excluded_by_decision = 0
    pending_covered: list[dict[str, Any]] = []
    decisions = (scope or {}).get("decisions") or _DEFAULT_DECISIONS
    if scope is not None:
        annotate(rows, scope)
    clusters: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}

    for row in sorted(rows, key=_priority):
        name = str(row.get("Name") or "")
        if str(row.get("Execution Type (as written)") or "").strip().lower() == "manual":
            excluded_manual += 1
            continue
        feature = classify(name, str(row.get("Description") or ""))
        if feature == EXCLUDED:
            excluded_engineering += 1
            continue
        if scope is not None:
            if row.get("Scope Decision") == decisions["not_on_screen"]:
                excluded_not_on_screen += 1
                continue
            if row.get("Scope Decision") == decisions["excluded"]:
                excluded_by_decision += 1         # the owner removed it; counted, never silent
                continue
            if row.get("Scope Decision") == decisions["covered"]:
                pending_covered.append(row)       # attached to the script that covers it, below
                continue
        controls, behaviours = controls_of(name), behaviours_of(name)
        key = (feature, controls)       # a different control is NEVER the same case
        scripted = row.get("Automation Status") == "Automated - script exists"
        home = None
        if not scripted:       # an existing test is always its own row
            for rep in clusters.get(key, []):
                threshold = (SIMILARITY if rep["_behaviours"] == behaviours
                             else SIMILARITY_DIFFERENT_BEHAVIOUR)
                if opposing_states(name, str(rep.get("Name"))):
                    continue        # "disabled when empty" is not "enabled when valid"
                if similarity(name, str(rep.get("Name"))) >= threshold:
                    home = rep
                    break
        if home is not None:
            home["_merged"].append(row)
            continue
        entry = dict(row)
        entry.update({"Feature": feature,
                      "Control / behaviour": " | ".join(filter(None, [
                          ", ".join(controls), ", ".join(behaviours)])) or "(general)",
                      "_merged": [], "_behaviours": behaviours})
        clusters.setdefault(key, []).append(entry)
        suite.append(entry)

    covered_total = 0
    for row in pending_covered:
        targets = [t.strip() for t in str(row.get("Covered By") or "").split(",") if t.strip()]
        home = next((e for t in targets for e in suite
                     if str(e.get("Automation ID") or "").endswith(t)), None)
        if home is None:
            # Never lose a case because a rule named a script that is not in the suite.
            row["Scope Decision"] = decisions["new"]
            row["Decision Note"] = f"covered-by target not found ({row.get('Covered By')}); " + str(
                row.get("Decision Note") or "")
            entry = dict(row)
            entry.update({"Feature": classify(str(row.get("Name") or "")),
                          "Control / behaviour": "(general)", "_merged": [], "_behaviours": ()})
            suite.append(entry)
            continue
        home["_merged"].append(row)
        covered_total += 1

    merged_total = 0
    for entry in suite:
        merged = entry.pop("_merged")
        entry.pop("_behaviours", None)
        merged_total += len(merged)
        entry["Similar cases merged"] = len(merged)
        entry["Merged case IDs"] = ", ".join(str(m.get("Test Case ID")) for m in merged)
    suite.sort(key=lambda r: (r["Feature"], r["Control / behaviour"], str(r.get("Test Case ID"))))
    return {"suite": suite, "excluded_manual": excluded_manual,
            "excluded_engineering": excluded_engineering, "merged": merged_total,
            "excluded_not_on_screen": excluded_not_on_screen, "covered": covered_total,
            "excluded_by_decision": excluded_by_decision}


# ---------------------------------------------------------------------------
# 4. Does the suite cover the tool? (Reported, never papered over.)
# ---------------------------------------------------------------------------

#: What the admin tool screen offers, as (what a person sees, regex over the case names).
TOOL_CONTROLS: list[tuple[str, str]] = [
    ("Toolbar - Pan", r"\bpan\b"), ("Toolbar - Zoom in", r"zoom[- ]?in"),
    ("Toolbar - Zoom out", r"zoom[- ]?out"), ("Toolbar - Rotate left", r"rotate left|counter-?clockwise"),
    ("Toolbar - Rotate right", r"rotate right|rotate clockwise"), ("Toolbar - Flip horizontal", r"flip horizontal|mirror"),
    ("Toolbar - Flip vertical", r"flip vertical"), ("Toolbar - Fit to screen", r"\bfit\b|fit to"),
    ("Toolbar - Grid", r"\bgrid"), ("Toolbar - Invert / contrast toggle", r"invert|negative"),
    ("Toolbar - Circle shape", r"circle|ellipse"), ("Toolbar - Square shape", r"square|rectangle"),
    ("Toolbar - Point marker", r"point marker|marker"), ("Toolbar - Line", r"\bline\b"),
    ("Toolbar - Ruler / measure", r"measure|ruler"), ("Toolbar - Show/hide (eye)", r"\beye\b|show/hide|visibility"),
    ("Toolbar - Crop", r"\bcrop"), ("Toolbar - Undo", r"\bundo"), ("Toolbar - Redo", r"\bredo"),
    ("Toolbar - Reset", r"\breset"),
    ("Adjustments - Brightness", r"brightness"), ("Adjustments - Contrast", r"contrast"),
    ("Adjustments - Sharpen", r"sharpen"), ("Adjustments - Blur", r"\bblur"),
    ("Adjustments - Opacity", r"opacity"), ("Adjustments - Rotation", r"rotation"),
    ("Sidebar - hide / show", r"sidebar|hide"), ("Header - Help", r"\bhelp\b"),
    ("Header - Keyboard shortcuts", r"keyboard|shortcut"), ("Footer - Upload", r"\bupload"),
    ("Footer - Cancel", r"\bcancel"),
    ("Mode - Overlay (blend)", r"overlay|blend"), ("Mode - Swipe", r"swipe"),
    ("Mode - Difference", r"difference"), ("Mode - Sync view", r"\bsync"),
    ("Save investigation", r"save"), ("Export image", r"export"),
]


def coverage(suite: list[dict[str, Any]],
             controls: list[tuple[str, str]] | list[list[str]] | None = None) -> list[tuple[str, int]]:
    """[(tool control, number of suite cases that name it)] - zero means a gap.

    `controls` is the profile's `screen_controls` (what THIS application's screen offers); the
    built-in TOOL_CONTROLS list is only the fallback for a profile that has none.
    """
    texts = [(_clean(str(r.get("Name"))) + " " + str(r.get("Merged case IDs") or "")).lower() for r in suite]
    return [(label, sum(1 for t in texts if re.search(pattern, t)))
            for label, pattern in (controls or TOOL_CONTROLS)]


# ---------------------------------------------------------------------------
# 5. Write it.
# ---------------------------------------------------------------------------

def _columns() -> list[str]:
    # The pipeline's own sheet columns, so the Coding Agent / Jira uploader can read this file.
    from agents.testcase_sheet import COLUMNS
    return list(COLUMNS)


_EXTRA = ["Scope Decision", "Decision Note", "Automation Status", "Automated Test File", "Last Run Result", "Last Run Note",
          "Similar cases merged", "Merged case IDs", "Source story", "Source story title"]
_WIDTHS = {"Feature": 30, "Control / behaviour": 28, "Name": 46, "Description": 38,
           "Test Step": 52, "Expected Result": 52, "Prerequisite": 30, "Automated Test File": 40,
           "Last Run Note": 44, "Merged case IDs": 34, "Source story title": 30,
           "Automation Status": 28, "Test Case ID": 22}


def write_final_sheet(suite: list[dict[str, Any]], out_path: Any, epic: str = "") -> Any:
    """ONE worksheet: Feature, Control / behaviour, the pipeline's standard columns, then status."""
    from pathlib import Path

    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    standard = _columns()
    headers = ["Feature", "Control / behaviour"] + standard + _EXTRA
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Automation Suite"
    for col, name in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
        cell.alignment = Alignment(wrap_text=True, vertical="center")
        ws.column_dimensions[get_column_letter(col)].width = _WIDTHS.get(name, 18)
    fills = {"Already automated": "C6EFCE", "New script needed": "FCE4D6",
             "Blocked - needs test data or a tool": "FFEB9C", "Automated - script exists": "C6EFCE", "Passed": "C6EFCE", "Failed": "FFC7CE",
             "Not run": "EDEDED", "Selected for automation - script not written yet": "FFEB9C",
             "Automatable - no script yet": "FCE4D6"}
    wrap = Alignment(wrap_text=True, vertical="top")
    for r, row in enumerate(suite, start=2):
        source = row.get("_source") or {}
        story = str(row.get("Story") or "")
        values: dict[str, Any] = {name: source.get(name) for name in standard}
        # The id downstream stages join on: the automation id when there is one (the scripts'
        # titles start with it), else the case's own id.
        values["Test Case ID"] = row.get("Automation ID") or row.get("Test Case ID")
        # The epic is run/profile data, never an engine constant: take what the
        # source row already carries, else the caller's `epic`, else the story alone.
        existing_ref = str(values.get("JIRA Ref (Epic/Story)") or "").strip()
        if existing_ref:
            values["JIRA Ref (Epic/Story)"] = existing_ref
        else:
            values["JIRA Ref (Epic/Story)"] = f"{epic} / {story}" if epic and story else story
        values.update({"Feature": row.get("Feature"), "Control / behaviour": row.get("Control / behaviour"),
                       "Scope Decision": row.get("Scope Decision"), "Decision Note": row.get("Decision Note"),
                       "Automation Status": row.get("Automation Status"),
                       "Automated Test File": row.get("Automated Test File"),
                       "Last Run Result": row.get("Last Run Result"), "Last Run Note": row.get("Last Run Note"),
                       "Similar cases merged": row.get("Similar cases merged"),
                       "Merged case IDs": row.get("Merged case IDs"),
                       "Source story": story, "Source story title": row.get("Story title")})
        for col, name in enumerate(headers, start=1):
            cell = ws.cell(row=r, column=col, value=values.get(name))
            cell.alignment = wrap
            colour = fills.get(str(values.get(name))) if name in (
                "Automation Status", "Last Run Result", "Scope Decision") else None
            if colour:
                cell.fill = PatternFill("solid", fgColor=colour)
    ws.freeze_panes = "C2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(suite) + 1}"
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out
