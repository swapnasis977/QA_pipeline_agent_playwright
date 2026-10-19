"""Reading a case id back out of a Playwright test title.

THE JOIN depends on this. The Coding Agent prefixes each test title with the
case id, and `_flatten_cases` reads it back to attribute a result to a case.
Everything downstream keys on what comes out: `heal_succeeded` decides whether
a heal worked by matching these ids, the Bug Reporter matches screenshots by
them, and `writeback` records outcomes against them.

Found by running the pipeline, not by reading it. For the title
"VENDOR-TC-001 Registration - Vendor: ..." the extractor returned
"TC-001" — the tail only. The pattern was built for ids shaped
`PROJ-1234-TC-007` (a project key, a story number, then the case number) and had
no alternative for `<PREFIX>-TC-<n>`, which is this project's entire scheme.
So `heal_succeeded(rerun, ["VENDOR-TC-001"])` matched nothing, fell back
to the process exit code, and the per-case verdict it exists to provide was
never actually used on a real run.

Run with:  python -m pytest tests/test_case_id_from_title.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import pytest  # noqa: E402

from qa_pipeline.agents import adapters  # noqa: E402

_extract = adapters._case_id_from_title


@pytest.mark.parametrize("title,expected", [
    # The shape this project actually uses — the one that was broken.
    ("VENDOR-TC-001 Registration - Vendor: successful new registration",
     "VENDOR-TC-001"),
    ("REG-TC-001 capture until result", "REG-TC-001"),
    ("SANITY2-TC-014 something", "SANITY2-TC-014"),
    # The shape the pattern was originally written for — must not regress.
    ("PROJ-68308-TC-012 checkout flow", "PROJ-68308-TC-012"),
    ("PROJ-1234 story-level title", "PROJ-1234"),
    # A bare case number is still a valid id.
    ("TC-007 login works", "TC-007"),
    # No id at all — "" rather than a guess. An invented id silently drops
    # the case, which is the failure reviewer.skill.md warns about.
    ("authenticate", ""),
    ("", ""),
])
def test_ids_are_read_back_whole(title, expected):
    assert _extract(title) == expected


def test_the_longest_qualified_id_wins():
    """The bug in one line: a prefixed id must not be shortened to its tail."""
    assert _extract("VENDOR-TC-001 x") != "TC-001"


def test_an_id_embedded_mid_title_is_still_found():
    assert _extract("Registration VENDOR-TC-001 happy path") == "VENDOR-TC-001"


def test_the_extracted_id_round_trips_through_the_heal_verdict():
    """Why it mattered: the id the Executor reports has to equal the id the
    heal stage is holding, or the per-case verdict silently degrades to the
    exit code it was written to replace."""
    from qa_pipeline.orchestrator.stages import heal_succeeded

    title = "VENDOR-TC-001 Registration - Vendor: successful new registration"
    rerun = {"returncode": 1,
             "cases": [{"test_case_id": _extract(title), "status": "passed"},
                       {"test_case_id": "", "status": "failed"}]}  # the auth setup

    assert heal_succeeded(rerun, ["VENDOR-TC-001"]) is True
