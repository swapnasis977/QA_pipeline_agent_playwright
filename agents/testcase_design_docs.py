"""Reading design material: Figma files and local design documents (including images embedded in a .docx).

Split out of agents/testcase_writer.py, which re-exports every name below, so
`agents.testcase_writer.<name>` keeps working. Code is moved verbatim.
"""

from __future__ import annotations

from pathlib import Path

import openpyxl

from utils.run_layout import artifact_path


def fetch_figma_context(figma_url: str, feature_dir: Path = None) -> dict:
    """Fetches design context either from a local design-reference doc
    (Word/Excel/PDF — used when your network can't reach the Figma API
    directly) or from the real Figma REST API for an actual figma.com URL.

    `figma_url` is treated as a local file path when it doesn't start with
    "http" and points at an existing .docx/.xlsx/.pdf file; otherwise it's
    treated as a Figma file URL as before. `feature_dir`, when given, is
    where a .docx's embedded screenshots get extracted to (see
    `_extract_docx_images`) — optional so existing callers that don't pass
    it still work, just without image extraction.
    """
    path = Path(figma_url) if figma_url else None
    if path and not figma_url.lower().startswith("http") and path.exists():
        return _read_local_design_doc(path, feature_dir)
    data = _fetch_figma_api(figma_url)
    _save_screen_model(data, feature_dir)
    return data


def _save_screen_model(data: dict, feature_dir: Path = None) -> None:
    """Writes the screen object model next to the test cases so a reviewer can read it."""
    model = data.get("screen_model")
    if not model or feature_dir is None:
        return
    from agents.artifact_io import write_json, write_text_file
    from utils.figma_model import render_markdown

    write_json(artifact_path(feature_dir, "screen_model.json"), model)
    write_text_file(artifact_path(feature_dir, "SCREEN_MODEL.md"), render_markdown(model))


def _extract_docx_images(path: Path, feature_dir: Path) -> list:
    """Pulls embedded screenshots out of a .docx's word/media/ folder.

    python-docx's paragraph iteration (used for text/heading extraction
    above) never touches images — a .docx is a zip archive, and embedded
    pictures live in word/media/*.png|jpg as separate parts with no
    corresponding text run. Confirmed in practice: a design doc with 19
    embedded Figma screenshots yielded ZERO images via paragraph text
    extraction. Unzips directly instead and copies each media file out.

    NOTE: `call_agent()` (utils/llm_client.py) now supports a multimodal
    `images` param — `run()` below passes these paths through, so the model
    actually sees the screenshots (bounded by
    llm_client._MAX_IMAGES_PER_CALL), not just a human reviewing them on
    disk. They're still saved to disk regardless, for that human review too.
    """
    import zipfile

    out_dir = (artifact_path(feature_dir, "figma_images") / path.stem) if feature_dir else None
    saved = []
    with zipfile.ZipFile(path) as zf:
        media_names = sorted(n for n in zf.namelist() if n.startswith("word/media/"))
        if not media_names:
            return []
        if out_dir is None:
            # No feature_dir given — report what WOULD be extracted without
            # writing anything, so callers still learn images exist.
            return [f"(unsaved — no feature_dir passed) {n}" for n in media_names]
        out_dir.mkdir(parents=True, exist_ok=True)
        for name in media_names:
            dest = out_dir / Path(name).name
            dest.write_bytes(zf.read(name))
            saved.append(str(dest))
    return saved


def _read_local_design_doc(path: Path, feature_dir: Path = None) -> dict:
    """Extracts content from a locally downloaded design-reference file
    (Word/Excel/PDF), segmented into distinct "screens" where the file's own
    structure makes that possible — a .docx with heading styles becomes one
    section per heading, a .xlsx becomes one section per worksheet. This
    matters when one file covers multiple screens/features and only some are
    relevant to the specific story being processed right now: the caller
    (testcase_writer.run) passes ALL sections to the model, clearly labeled,
    and lets it decide which are relevant rather than treating the whole file
    as one relevant blob."""
    suffix = path.suffix.lower()
    images = []

    if suffix == ".docx":
        import docx
        doc = docx.Document(str(path))
        sections = []
        current_heading, current_lines = None, []
        for p in doc.paragraphs:
            if not p.text.strip():
                continue
            if p.style.name.startswith("Heading"):
                if current_heading is not None or current_lines:
                    sections.append({
                        "heading": current_heading or "(untitled section)",
                        "content": "\n".join(current_lines),
                    })
                current_heading, current_lines = p.text.strip(), []
            else:
                current_lines.append(p.text)
        if current_heading is not None or current_lines:
            sections.append({
                "heading": current_heading or "(untitled section)",
                "content": "\n".join(current_lines),
            })
        # No heading styles used at all — nothing to segment by, treat the
        # whole document as a single section (same behavior as before).
        if not sections:
            full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
            sections = [{"heading": path.stem, "content": full_text}]

        # Not correlated to individual sections — python-docx's paragraph
        # API doesn't expose which heading an inline image visually falls
        # under without walking raw XML relationship IDs, so all screenshots
        # from the file are surfaced together rather than guessed per-section.
        images = _extract_docx_images(path, feature_dir)

    elif suffix == ".xlsx":
        wb = openpyxl.load_workbook(path, data_only=True)
        sections = []
        for ws in wb.worksheets:
            lines = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) for c in row if c is not None]
                if cells:
                    lines.append(" | ".join(cells))
            sections.append({"heading": ws.title, "content": "\n".join(lines)})

    elif suffix == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(str(path))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        # PDFs don't expose a reliable heading structure to segment by —
        # treated as a single section (no cross-screen filtering possible).
        sections = [{"heading": path.stem, "content": text}]

    else:
        print(f"⚠️  Unsupported local design doc format '{suffix}' for '{path}' "
              "— expected .docx, .xlsx, or .pdf. Returning placeholder data.")
        return {
            "figma_url": str(path),
            "frames": [f"[PLACEHOLDER] Unsupported local file format '{suffix}'"],
        }

    result = {
        "figma_url": str(path),
        "source": "local_file",
        "sections": sections or [{"heading": path.stem, "content": "(no text extracted from file)"}],
    }
    if images:
        result["screenshots"] = images
        result["screenshots_note"] = (
            "Sent to the model as real image inputs (see run() / "
            "utils/llm_client._prepare_images) in addition to being saved "
            "here on disk for human review — bounded to the first "
            "llm_client._MAX_IMAGES_PER_CALL images per call if there are more."
        )
    return result


def _fetch_figma_api(figma_url: str) -> dict:
    """Fetches top-level frame names from a Figma file via the REST API.

    Requires FIGMA_API_TOKEN in your environment (see .env.example). Falls
    back to placeholder data with a clear warning if the token is missing or
    the URL can't be parsed, so the pipeline doesn't crash — it just tells
    you plainly it's running on fake data instead of silently doing so.
    """
    import os
    import re

    import requests

    token = os.environ.get("FIGMA_API_TOKEN", "")
    if not token:
        print("⚠️  FIGMA_API_TOKEN missing — returning placeholder data. "
              "Set it in .env for real Figma data.")
        return {
            "figma_url": figma_url,
            "frames": ["[PLACEHOLDER] Fetch frame list via Figma API"],
        }

    # Figma file URLs look like .../file/<key>/... or .../design/<key>/...
    match = re.search(r"/(?:file|design)/([a-zA-Z0-9]+)", figma_url)
    if not match:
        print(f"⚠️  Could not parse a Figma file key out of '{figma_url}' — "
              "returning placeholder data.")
        return {
            "figma_url": figma_url,
            "frames": ["[PLACEHOLDER] Unrecognized Figma URL format"],
        }
    file_key = match.group(1)

    resp = requests.get(
        f"https://api.figma.com/v1/files/{file_key}",
        headers={"X-Figma-Token": token},
        timeout=15,
    )
    resp.raise_for_status()
    file_data = resp.json()

    # Top-level frames (direct children of each page's canvas) are enough
    # context for the LLM to reason about the UI flow without needing the
    # full (often huge) node tree.
    frames = []
    for page in file_data.get("document", {}).get("children", []):
        for node in page.get("children", []):
            if node.get("type") == "FRAME":
                frames.append(node.get("name", ""))

    from utils.figma_model import build_screen_model

    return {
        "figma_url": figma_url,
        "file_name": file_data.get("name", ""),
        "frames": frames or ["(no top-level frames found)"],
        # Components per screen, read from layer names. Design input only: the prompt may
        # use it to know which controls a screen has, but it never supplies a locator.
        "screen_model": build_screen_model(file_data),
    }
