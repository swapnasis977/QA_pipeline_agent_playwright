"""When tsc says a property does not exist, say where it DOES exist.

A spec imports three recorded page objects and calls properties across all of
them. Mixing up which object owns which property is the single most repeated
failure in this project's generations:

    TS2339: Property 'connected' does not exist on type
            'RecordedSanity2Tc001VendorPage'

`connected` is real — it lives on `RecordedRegisterTc001Page`, which the same
spec already imports as `register`. The fix is one identifier: `vendor.
connected` -> `register.connected`.

The compile gate caught this correctly every time and rolled the file back.
What it handed the repair loop was tsc's raw text, which says only that the
property is absent — so both repair attempts re-guessed, produced no file, and
the run ended with nothing. The information needed to fix it in one line was
sitting in the workspace the whole time.

Run with:  python -m pytest tests/test_property_owner_hint.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from agents.coding_agent import suggest_property_owner  # noqa: E402

_ERROR = ("tests/e2e/x.spec.ts(67,26): error TS2339: Property 'connected' does "
          "not exist on type 'RecordedSanity2Tc001VendorPage'.")


@pytest.fixture
def repo(tmp_path):
    pages = tmp_path / "src" / "pages" / "recorded"
    pages.mkdir(parents=True)
    (pages / "RecordedRegisterTc001Page.ts").write_text(
        "export class RecordedRegisterTc001Page {\n"
        "  readonly connected: Locator;\n"
        "  readonly startcapture: Locator;\n}\n", encoding="utf-8")
    (pages / "RecordedSanity2Tc001VendorPage.ts").write_text(
        "export class RecordedSanity2Tc001VendorPage {\n"
        "  readonly vendorLink: Locator;\n}\n", encoding="utf-8")
    return tmp_path


def test_the_owning_class_is_named(repo):
    hint = suggest_property_owner(_ERROR, repo)
    assert "connected" in hint
    assert "RecordedRegisterTc001Page" in hint


def test_a_property_that_exists_nowhere_gives_no_false_hint(repo):
    error = ("x.spec.ts(1,1): error TS2339: Property 'totallyInvented' does not "
             "exist on type 'RecordedRegisterTc001Page'.")
    assert suggest_property_owner(error, repo) == ""


def test_the_class_that_already_failed_is_not_suggested_back(repo):
    """Suggesting the same class the error came from would be noise."""
    error = ("x.spec.ts(1,1): error TS2339: Property 'vendorLink' does not "
             "exist on type 'RecordedSanity2Tc001VendorPage'.")
    assert suggest_property_owner(error, repo) == ""


def test_an_unrelated_compile_error_is_ignored(repo):
    assert suggest_property_owner("error TS2307: Cannot find module '@x'", repo) == ""


def test_several_missing_properties_are_all_reported(repo):
    error = (_ERROR + "\n" +
             "x.spec.ts(70,4): error TS2339: Property 'startcapture' does not "
             "exist on type 'RecordedSanity2Tc001VendorPage'.")
    hint = suggest_property_owner(error, repo)
    assert "connected" in hint and "startcapture" in hint


def test_no_repo_root_is_safe(repo):
    assert suggest_property_owner(_ERROR, None) == ""
