"""Folding `reused_pom` into `page_objects` must not invent a second path for
a page object the plan already located.

The Planner emits reuse as "ClassName.method" — no path — so `normalize_plan`
turns the class half into a scaffold path, which is how the reuse signal
reaches the scaffold gate and the Coding Agent's import list. It did that with
a hardcoded `src/pages/<Class>.ts`, and page objects do not all live directly
in `src/pages/`: everything generated from a human recording sits in
`src/pages/recorded/`.

Found by running the pipeline. A real plan for VENDOR-TC-001 came back
listing SIX page objects — the three real recorded ones under
`src/pages/recorded/`, plus the same three again under `src/pages/`, which
exist nowhere on disk. The Coding Agent is handed that list as its imports, so
half of them resolve to nothing: TS2307, the file fails its syntax gate, and
the chunk is rolled back. A phantom import is how a fully-grounded plan (31/31
elements resolved) still produces no spec.

Run with:  python -m pytest tests/test_reused_pom_paths.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.agents import adapters  # noqa: E402


def _plan(**over):
    base = {
        "test_case_id": "VENDOR-TC-001",
        "target_file": "tests/e2e/vendor-registration.spec.ts",
        "test_kind": "e2e",
        "page_objects": [],
        "reused_pom": [],
    }
    base.update(over)
    return adapters.normalize_plan(base)


def test_a_class_already_located_is_not_duplicated_at_a_guessed_path():
    """The real defect: the plan already said where the file is."""
    plan = _plan(
        page_objects=["src/pages/recorded/RecordedRegisterTc001Page.ts"],
        reused_pom=["RecordedRegisterTc001Page.registerVendor"])

    assert plan["page_objects"] == ["src/pages/recorded/RecordedRegisterTc001Page.ts"]


def test_several_reused_classes_all_keep_their_real_directory():
    """The exact shape of the observed plan, minus the phantoms."""
    real = [
        "src/pages/recorded/RecordedSanity2Tc001VendorPage.ts",
        "src/pages/recorded/RecordedSampleIdentManagePage.ts",
        "src/pages/recorded/RecordedRegisterTc001Page.ts",
    ]
    plan = _plan(page_objects=list(real), reused_pom=[
        "RecordedSanity2Tc001VendorPage.fillPersonalDetails",
        "RecordedSampleIdentManagePage.search",
        "RecordedRegisterTc001Page.driveCaptureTillResult",
    ])

    assert plan["page_objects"] == real
    assert not any(p.startswith("src/pages/Recorded") for p in plan["page_objects"])


def test_a_class_the_plan_did_not_locate_still_gets_the_scaffold_default():
    """The behaviour worth keeping: a reuse claim naming a page object the
    plan forgot to list still reaches the Coding Agent, at the scaffold's
    conventional location for a page object it must create."""
    plan = _plan(reused_pom=["LoginPage.loginAsStandardUser"])
    assert plan["page_objects"] == ["src/pages/LoginPage.ts"]


def test_a_differently_named_class_is_still_added():
    """Matching is on the FILE name, not a substring — two distinct classes
    must not collapse into one."""
    plan = _plan(page_objects=["src/pages/recorded/RecordedRegisterTc001Page.ts"],
                 reused_pom=["RegistrationPage.submit"])
    assert plan["page_objects"] == [
        "src/pages/recorded/RecordedRegisterTc001Page.ts",
        "src/pages/RegistrationPage.ts",
    ]


def test_an_exact_duplicate_is_still_collapsed():
    plan = _plan(page_objects=["src/pages/RegistrationPage.ts"],
                 reused_pom=["RegistrationPage.submit"])
    assert plan["page_objects"] == ["src/pages/RegistrationPage.ts"]


def test_a_reused_entry_that_is_already_a_path_is_left_alone():
    plan = _plan(reused_pom=["src/pages/recorded/RecordedRegisterTc001Page.ts"])
    assert plan["page_objects"] == []


def test_a_recorded_page_object_falls_back_to_its_real_directory():
    """`recording_parser.generate` writes every recorded page object to
    src/pages/recorded/ and names the class Recorded<Case>Page, so the prefix
    and the directory are two halves of one guarantee — not a heuristic.
    The flat fallback sent the Coding Agent an import that resolves to
    nothing for precisely this project's highest-confidence source."""
    plan = _plan(reused_pom=["RecordedRegisterTc001Page.driveCaptureTillResult"])
    assert plan["page_objects"] == [
        "src/pages/recorded/RecordedRegisterTc001Page.ts"]


def test_a_normal_page_object_still_falls_back_to_src_pages():
    plan = _plan(reused_pom=["RegistrationPage.submit"])
    assert plan["page_objects"] == ["src/pages/RegistrationPage.ts"]
