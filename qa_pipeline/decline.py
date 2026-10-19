"""`qa-pipeline decline`: take one test case out of play, everywhere it lives, in one step.

A case that no longer applies (the feature is not in the build, the requirement changed) used to
need four hand edits that drift apart: the run's case JSON, the automation plan, the spec file and
every workbook. This does them together and says what it changed, so nothing is left half-declined.

Nothing is deleted. The case stays in every sheet with its reason; its script is SKIPPED (not
removed) so the original steps stay for the day the feature comes back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

STATUS = "Declined - not applicable"
RESULT = "Skipped (declined)"
_MARK = "Declined:"
_BACKUP = re.compile(r"\.backup|_BACKUP", re.I)


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_text(path: Path, text: str, crlf: bool = False) -> None:
    """UTF-8 write that keeps the line endings it is given. `Path.write_text` turns
    every LF into CRLF on Windows, rewriting each line of a file the user edits."""
    if crlf:
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _uses_crlf(path: Path) -> bool:
    try:
        return b"\r\n" in path.read_bytes()
    except OSError:
        return False


def _write_json(path: Path, data) -> None:
    _write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                crlf=_uses_crlf(path))


def _mark_json_cases(folder: Path, case_id: str, reason: str, dry_run: bool) -> list[str]:
    """`declined` / `decline_reason` on every entry for this case in the run's JSON files."""
    changed = []
    for path in sorted(folder.glob("*/1_testcases/test_cases.json")) + \
            sorted(folder.glob("*/2_automation/automation_plan.json")):
        data = _read_json(path)
        if not isinstance(data, list):
            continue
        hit = False
        for entry in data:
            if isinstance(entry, dict) and entry.get("test_case_id") == case_id:
                entry["declined"], entry["decline_reason"] = True, reason
                hit = True
        if hit:
            changed.append(str(path))
            if not dry_run:
                _write_json(path, data)
    return changed


def skip_in_spec(text: str, case_id: str, reason: str) -> tuple[str, str]:
    """(status, text): the spec with `test.skip(true, ...)` as the first statement of the case's test.

    status is "done", "already" (that test is already declined) or "by_hand" (the id is not in a
    plain `test('<id> ...'` title, e.g. a title built in a loop, which is not edited blindly).
    """
    title = re.search(r"test\(\s*[`'\"]" + re.escape(case_id) + r"\b", text)
    if not title:
        return "by_hand", text
    body = re.search(r"=>\s*\{\n", text[title.end():])
    if not body:
        return "by_hand", text
    at = title.end() + body.end()
    if f"test.skip(true, '{_MARK}" in text[at:at + 600]:
        return "already", text
    indent = re.match(r"[ \t]*", text[at:]).group(0) or "    "
    # One physical line: a newline in the reason would end the string literal early.
    safe = " ".join(str(reason).split()).replace("\\", "/").replace("'", "’")
    line = (f"{indent}// DECLINED ({case_id}): the steps below are kept for reference.\n"
            f"{indent}test.skip(true, '{_MARK} {safe}');\n")
    return "done", text[:at] + line + text[at:]


def _skip_specs(workspace: Path, case_id: str, reason: str, dry_run: bool) -> tuple[list[str], list[str]]:
    done, manual = [], []
    for path in sorted((workspace / "tests").rglob("*.ts")):
        text = path.read_text(encoding="utf-8")
        if case_id not in text:
            continue
        status, updated = skip_in_spec(text, case_id, reason)
        if status == "by_hand":
            manual.append(str(path))
        elif status == "done":
            done.append(str(path))
            if not dry_run:
                _write_text(path, updated, crlf=_uses_crlf(path))
    return done, manual


def _load_workbooks(folder: Path) -> list:
    """[(path, workbook)] for every real workbook, loaded BEFORE anything is written.

    A file that cannot be opened (a corrupt zip, an Excel lock file `~$x.xlsx`, a file
    held open by Excel so it cannot be saved back) is skipped here, so it can never fail
    halfway through and leave the case declined in the JSON and the specs but not the
    sheets.
    """
    import openpyxl

    loaded = []
    for path in sorted(folder.glob("*/1_testcases/*.xlsx")):
        if path.name.startswith("~$") or _BACKUP.search(path.name):
            continue
        try:
            workbook = openpyxl.load_workbook(path)
            with open(path, "ab"):      # raises while another program has it locked
                pass
        except Exception as exc:        # BadZipFile, KeyError, PermissionError, ...
            print(f"      Skipping workbook {path} ({type(exc).__name__}: {exc}).")
            continue
        loaded.append((path, workbook))
    return loaded


def _mark_sheets(folder: Path, case_id: str, reason: str, dry_run: bool, loaded: list = None) -> list[str]:
    changed = []
    for path, workbook in (loaded if loaded is not None else _load_workbooks(folder)):
        touched = False
        for sheet in workbook:
            header = [c.value for c in sheet[1]]
            if "Test Case ID" not in header:
                continue
            ix = {h: i for i, h in enumerate(header) if h}
            for row in sheet.iter_rows(min_row=2):
                ids = {str(row[ix["Test Case ID"]].value or "")}
                if "Automation ID" in ix:
                    ids.add(str(row[ix["Automation ID"]].value or ""))
                if case_id not in ids:
                    continue
                for column, value in (("Scope Decision", STATUS), ("Automation Status", STATUS),
                                      ("Decision Note", reason), ("Status", "Declined"),
                                      ("Last Run Result", RESULT), ("Result", RESULT)):
                    if column in ix:
                        row[ix[column]].value = value
                touched = True
        if touched:
            if not dry_run:
                try:
                    workbook.save(path)
                except Exception as exc:
                    print(f"      Could not save workbook {path} ({type(exc).__name__}: {exc}) - "
                          f"close it and run decline again; the rest was already declined.")
                    continue
            changed.append(str(path))
    return changed


def _remember_in_scope(profile, case_id: str, reason: str, dry_run: bool) -> bool:
    """`declined` in the profile's automation_scope.json, so a REBUILT sheet keeps the decision."""
    path = Path(profile.root) / "automation_scope.json"
    scope = _read_json(path)
    if not isinstance(scope, dict):
        return False
    if (scope.get("declined") or {}).get(case_id) == reason:
        return False
    scope.setdefault("declined", {})[case_id] = reason
    if not dry_run:
        _write_json(path, scope)
    return True


def decline(profile, case_id: str, reason: str, dry_run: bool = False) -> dict:
    """Declines `case_id` everywhere; returns what changed (and what needs a hand edit)."""
    # One line: the reason is written into spec string literals and sheet cells.
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise ValueError("a reason is required: it is what the next person reads in every sheet")
    runs = Path(profile.context_root)
    workbooks = _load_workbooks(runs)     # preflight: nothing is written if this cannot finish
    specs_done, specs_manual = _skip_specs(Path(profile.workspace), case_id, reason, dry_run)
    return {
        "case": case_id, "reason": reason, "dry_run": dry_run,
        "json": _mark_json_cases(runs, case_id, reason, dry_run),
        "specs": specs_done,
        "specs_by_hand": specs_manual,
        "sheets": _mark_sheets(runs, case_id, reason, dry_run, loaded=workbooks),
        "scope_file": _remember_in_scope(profile, case_id, reason, dry_run),
    }


def cmd_decline(args, profile) -> int:
    try:
        report = decline(profile, args.case, args.reason, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"[FAILED] {exc}")
        return 1
    verb = "Would change" if args.dry_run else "Changed"
    print(f"      Declining {report['case']}: {report['reason']}")
    for label, key in (("case/plan JSON", "json"), ("spec files (test.skip)", "specs"), ("workbooks", "sheets")):
        print(f"      {verb} {label}: {len(report[key])}" + "".join(f"\n        {p}" for p in report[key]))
    if report["scope_file"]:
        print("      Remembered in automation_scope.json, so a rebuilt sheet keeps it.")
    for path in report["specs_by_hand"]:
        print(f"      NEEDS A HAND EDIT: {path} mentions {report['case']} but not as a plain test title "
              f"(a data-driven table?). Add test.skip(true, '{_MARK} ...') there.")
    if not any(report[k] for k in ("json", "specs", "sheets")) and not report["specs_by_hand"]:
        print(f"      Nothing found for {report['case']}: check the id.")
        return 1
    return 0
