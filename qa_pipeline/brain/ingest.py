"""Documents in, knowledge cards out.

The distinction this module enforces is the one that keeps the brain from
collapsing back into a prompt blob: a document is an INPUT to the ledger, not
the ledger itself. A design doc gets read once, distilled into typed facts,
and then never sent to a model again — what agents receive downstream is a
handful of retrieved cards, not forty pages of prose per case per stage.

CONFIDENCE CEILING. Everything ingested here is capped at its source's base
confidence (`trust.BASE_CONFIDENCE`), which for a design doc is 0.55 — below
the 0.80 grounding floor. That is deliberate and it is the whole point: a
document can tell you a screen exists, what a button is *called*, and that a
device is mocked in QA. It cannot tell you the locator, because the document
describes intent and only the running app describes reality. Locator cards
sourced from a document are therefore knowledge the Explorer will go and
CONFIRM, never knowledge the Coding Agent may build on.

Images (Figma frames, screenshots of old screens) are passed to the model as
real images via `llm_client.call_agent(images=...)` rather than being
described in text, so a frame's actual button labels can be read off it.
"""

from __future__ import annotations

from pathlib import Path

from utils.llm_client import call_agent, parse_json_response
from utils.timestamps import utc_iso

from . import cards as card_builders
from . import trust

#: What we know how to read. Anything else in the knowledge directory is
#: reported and skipped rather than silently ignored — a doc someone added
#: expecting it to be used, that never was, is a worse failure than a warning.
TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".rst", ".csv", ".json", ".yaml", ".yml"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DOC_SUFFIXES = {".docx", ".pdf"}

#: Per-document text cap. A 200-page spec sent whole would blow the context
#: window and bury the few facts worth extracting. Ingestion is chunked
#: instead, so every part of a long document gets its own extraction pass.
CHUNK_CHARS = 12000

#: How many images accompany one chunk. `llm_client._prepare_images` caps at
#: 6 per call anyway; matching it here avoids silently dropping the rest.
MAX_IMAGES_PER_CHUNK = 6


def read_document(path: Path) -> str:
    """Best-effort text extraction. Returns "" for anything unreadable — a
    document that cannot be parsed must not stop the ingest of the ones that
    can."""
    suffix = path.suffix.lower()
    try:
        if suffix in TEXT_SUFFIXES:
            return path.read_text(encoding="utf-8", errors="replace")
        if suffix == ".docx":
            import docx  # python-docx, already a hard requirement
            document = docx.Document(str(path))
            parts = [p.text for p in document.paragraphs if p.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            return "\n".join(parts)
        if suffix == ".pdf":
            from pypdf import PdfReader  # already a hard requirement
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception as exc:
        print(f"      ⚠️  Could not read {path.name} ({type(exc).__name__}: {exc}) — skipping it.")
    return ""


def _chunks(text: str, size: int = CHUNK_CHARS):
    """Splits on paragraph boundaries where possible, so a fact is not cut in
    half across two extraction passes."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, current = [], []
    length = 0
    for para in text.split("\n\n"):
        if length + len(para) > size and current:
            out.append("\n\n".join(current))
            current, length = [], 0
        current.append(para)
        length += len(para) + 2
    if current:
        out.append("\n\n".join(current))
    return out


def _source_for(path: Path) -> str:
    """Which provenance a document's facts get.

    A Figma export describes the design, which the built app may have
    diverged from; a legacy test sheet describes a previous release. Both
    rank below a hand-written design doc about the app as it stands, and all
    three rank far below anything observed.
    """
    name = path.name.casefold()
    if "figma" in name or path.suffix.lower() in IMAGE_SUFFIXES:
        return "figma"
    if "testcase" in name or "test_case" in name or path.suffix.lower() in (".xlsx", ".xlsm", ".csv"):
        return "legacy_testcase"
    return "design_doc"


def _build_cards(proposals, source: str, evidence: str, when: str) -> tuple:
    """Turns the model's loose proposals into real cards.

    Every card is rebuilt through `brain.cards`, never accepted as the model
    emitted it. Two reasons: the id is a natural key that must be formatted
    identically everywhere (see `cards.make_id`), and the confidence is
    clamped to the source's ceiling here rather than being whatever number
    the model felt like claiming.
    """
    built, rejected = [], []
    ceiling = trust.base_confidence(source)

    for raw in proposals or []:
        if not isinstance(raw, dict):
            rejected.append(f"not an object: {raw!r}")
            continue
        card_type = str(raw.get("type") or "").strip()
        builder = card_builders.BUILDERS.get(card_type)
        if builder is None:
            rejected.append(f"unknown card type {card_type!r}")
            continue

        # A model can answer "high" or "0.8 " here. The bare float() sat outside
        # the try, so one such card raised out of the whole stage and every
        # later document was lost. Unusable means "use the ceiling's default",
        # never "stop ingesting".
        try:
            confidence = min(float(raw.get("confidence") or ceiling), ceiling)
        except (TypeError, ValueError):
            confidence = ceiling
        confidence = max(0.0, confidence)
        try:
            if card_type == "element":
                built.append(builder(
                    screen=raw.get("screen", ""), logical_name=raw.get("logical_name", ""),
                    locator=raw.get("locator", ""), source=source,
                    role=raw.get("role", ""), accessible_name=raw.get("accessible_name", ""),
                    unique=bool(raw.get("unique", True)),
                    confidence=confidence, evidence=evidence, when=when))
            elif card_type == "screen":
                built.append(builder(
                    raw.get("name", ""), source, route=raw.get("route", ""),
                    purpose=raw.get("purpose", ""), reached_by=raw.get("reached_by") or [],
                    confidence=confidence, evidence=evidence, when=when))
            elif card_type == "nav_edge":
                built.append(builder(
                    raw.get("from", ""), raw.get("to", ""), raw.get("action", ""),
                    source, confidence=confidence, evidence=evidence, when=when))
            elif card_type == "glossary":
                built.append(builder(
                    raw.get("tester_word", ""), raw.get("ui_label", ""), source,
                    confidence=confidence, evidence=evidence, when=when))
            elif card_type in ("rule", "env_quirk"):
                built.append(builder(
                    raw.get("name", ""), raw.get("statement", ""), source,
                    confidence=confidence, evidence=evidence, when=when))
            elif card_type == "test_data":
                built.append(builder(
                    raw.get("name", ""), raw.get("payload"), source,
                    confidence=confidence, evidence=evidence, when=when))
        except Exception as exc:
            rejected.append(f"{card_type} {raw.get('name') or raw.get('logical_name')!r}: {exc}")

    return built, rejected


MANIFEST_FILENAME = "ingest_manifest.json"


def _ingest_version() -> str:
    """Changes when the extraction prompt or the doc_ingestor skill changes, so
    a prompt improvement re-reads every document once, and nothing else does."""
    import hashlib
    h = hashlib.sha256(_prompt("", "design_doc", "").encode("utf-8"))
    skill = Path(__file__).resolve().parents[2] / "skills" / "doc_ingestor.skill.md"
    try:
        h.update(skill.read_bytes())
    except OSError:
        pass
    return h.hexdigest()[:16]


def _file_sha(path: Path) -> str:
    import hashlib
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _load_manifest(ledger) -> dict:
    from agents.artifact_io import read_json
    data = read_json(Path(ledger.dir) / MANIFEST_FILENAME, default={}) or {}
    files = data.get("files") if isinstance(data, dict) else None
    return files if isinstance(files, dict) else {}


def _save_manifest(ledger, files: dict) -> None:
    from agents.artifact_io import write_json
    write_json(Path(ledger.dir) / MANIFEST_FILENAME, {"files": files})


def from_documents(ledger, knowledge_dir, feature_dir, *, verbose: bool = True) -> dict:
    """Reads every document under `knowledge_dir` and folds it into the ledger.

    Idempotent by construction: cards are keyed by natural id and merged
    through `trust.resolve`, so re-ingesting an unchanged document changes
    nothing. It is also CHEAP to repeat: `brain/ingest_manifest.json` records
    each file's sha256 plus a prompt/skill version, and an unchanged file that
    was fully ingested under the current version is not sent to the model
    again. A file with any failed chunk is never recorded, so it is retried.
    """
    knowledge_dir = Path(knowledge_dir)
    when = utc_iso()
    report = {"documents": 0, "chunks": 0, "cards": 0, "rejected": [], "skipped": [],
              "unchanged": [],
              "tally": {"added": 0, "updated": 0, "kept_existing": 0}}
    version = _ingest_version()
    manifest = _load_manifest(ledger)
    # A manifest with an empty ledger means the ledger was wiped; trust nothing.
    if not ledger.cards:
        manifest = {}
    manifest_dirty = False

    if not knowledge_dir.is_dir():
        if verbose:
            print(f"      No knowledge directory at {knowledge_dir} — nothing to ingest.")
        return report

    paths = sorted(p for p in knowledge_dir.rglob("*") if p.is_file())
    if not paths:
        if verbose:
            print(f"      {knowledge_dir} is empty — nothing to ingest.")
        return report

    for path in paths:
        suffix = path.suffix.lower()
        source = _source_for(path)

        if suffix in IMAGE_SUFFIXES:
            text, images = "", [str(path)]
        elif suffix in TEXT_SUFFIXES or suffix in DOC_SUFFIXES:
            text, images = read_document(path), []
        else:
            report["skipped"].append(f"{path.name} (unsupported type {suffix})")
            continue

        if not text.strip() and not images:
            report["skipped"].append(f"{path.name} (no readable content)")
            continue

        key = path.relative_to(knowledge_dir).as_posix()
        sha = _file_sha(path)
        entry = manifest.get(key)
        if (sha and isinstance(entry, dict) and entry.get("sha256") == sha
                and entry.get("version") == version):
            report["unchanged"].append(key)
            continue

        report["documents"] += 1
        pieces = _chunks(text) or [""]
        doc_failed = False

        for i, chunk in enumerate(pieces, start=1):
            report["chunks"] += 1
            label = f"{path.name}" + (f" (part {i}/{len(pieces)})" if len(pieces) > 1 else "")
            if verbose:
                print(f"      Ingesting {label} as '{source}'...")

            prompt = _prompt(label, source, chunk)
            try:
                raw = call_agent("doc_ingestor", prompt, feature_dir,
                                 max_tokens=6000,
                                 images=images[:MAX_IMAGES_PER_CHUNK] if i == 1 else None)
                parsed = parse_json_response(raw, feature_dir, "doc_ingestor")
            except Exception as exc:
                report["rejected"].append(f"{label}: {type(exc).__name__}: {exc}")
                doc_failed = True
                continue

            proposals = parsed.get("cards") if isinstance(parsed, dict) else parsed
            built, rejected = _build_cards(proposals, source, path.name, when)
            report["rejected"].extend(f"{label}: {r}" for r in rejected)
            if rejected and not built:
                doc_failed = True

            tally = ledger.upsert_many(built)
            for tally_key in report["tally"]:
                report["tally"][tally_key] += tally.get(tally_key, 0)
            report["cards"] += len(built)

        if sha and not doc_failed:
            manifest[key] = {"sha256": sha, "version": version}
            manifest_dirty = True

    if manifest_dirty:
        try:
            _save_manifest(ledger, manifest)
        except OSError as exc:
            print(f"      ⚠️  Could not write {MANIFEST_FILENAME} ({exc}) — "
                  f"documents will be re-read next time.")

    if verbose:
        if report["unchanged"]:
            print(f"      {len(report['unchanged'])} document(s) unchanged since the last "
                  f"ingest — not re-read.")
        t = report["tally"]
        print(f"      Ingested {report['documents']} document(s): {t['added']} new card(s), "
              f"{t['updated']} updated, {t['kept_existing']} already known"
              + (f", {len(report['rejected'])} rejected" if report["rejected"] else ""))
        for skipped in report["skipped"]:
            print(f"      ⚠️  Skipped {skipped}")

    return report


def _prompt(label: str, source: str, text: str) -> str:
    ceiling = trust.base_confidence(source)
    return f"""Extract durable, reusable facts about the TARGET APPLICATION from the
document below, as knowledge cards.

DOCUMENT: {label}
PROVENANCE: {source} (confidence ceiling {ceiling:.2f})

{"CONTENT:" if text.strip() else "The content is in the attached image(s)."}
{text}

Return JSON only: {{"cards": [ ... ]}}

Extract ONLY facts that will still be true next month and that an automated
test would need. Skip narrative, history, meeting notes, and anything specific
to one release.

Card shapes:
  {{"type":"glossary","tester_word":"tile","ui_label":"module button"}}
  {{"type":"screen","name":"Face Comparison admin","route":"/compare","purpose":"...","reached_by":["Cases","Open case","Compare"]}}
  {{"type":"nav_edge","from":"Dashboard","to":"Face Comparison admin","action":"click Compare"}}
  {{"type":"element","screen":"Login","logical_name":"Sign in","locator":"","role":"button","accessible_name":"Sign in"}}
  {{"type":"rule","name":"Password policy","statement":"..."}}
  {{"type":"env_quirk","name":"Fingerprint scanner","statement":"mocked in QA; always returns a match"}}
  {{"type":"test_data","name":"Valid member","payload":{{"id":"...","name":"..."}}}}

Rules that matter:
  - GLOSSARY IS THE HIGHEST-VALUE CARD TYPE. Any place the document reveals
    that testers/business call something one thing and the UI says another,
    emit a glossary card. That mismatch is the single most common reason
    automation fails to find a screen at all.
  - For `element`, leave `locator` EMPTY unless the document literally quotes
    a selector. You cannot know the locator from prose, and a guessed one is
    worse than none: it will be treated as a lead to confirm, not as truth.
  - Prefer few, precise cards over many vague ones. A card nobody can act on
    is noise that will fuzzy-match a real request later and win.
  - Emit nothing at all (`{{"cards": []}}`) if the document holds no durable
    application facts. That is a valid and useful answer.
"""
