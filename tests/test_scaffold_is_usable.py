"""The scaffold must compile, and must not re-teach the traps it knows about.

THE DEFECT THIS FILE EXISTS FOR. `src/fixtures/index.ts` is emitted from a
non-raw Python string, and its `cssEscape` line lost a backslash on the way
out:

    emitted:  String(s).replace(/["\]/g, '\$&');
    correct:  String(s).replace(/["\]/g, '\$&');

`/["\]/` escapes the `]`, so the character class never closes and the regex
literal runs to end of line. Every freshly scaffolded workspace shipped a
fixtures file that did not compile — `error TS1161: Unterminated regular
expression literal` — and `src/fixtures/index.ts` is the module EVERY
generated spec imports `test`/`expect` from. A brand-new project was broken
before a single test was written.

It survived because the one workspace in this repo was scaffolded before the
regression and never re-scaffolded, so nothing ever compiled a fresh one.

Also guarded here: the `isVisible()` no-wait trap. `locator.isVisible()`
accepts a `timeout` option but does NOT wait for one — it answers from the
current DOM and returns immediately. The scaffold's own `auth.setup.ts` used
it to probe for the "Stay signed in?" interstitial, which had not rendered
yet, so the prompt was never dismissed and EVERY executor run failed at
sign-in, reported as a credentials/MFA problem it was not.

Run with:  python -m pytest tests/test_scaffold_is_usable.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.agents import framework_scaffolder  # noqa: E402
from qa_pipeline.framework import templates  # noqa: E402

#: Written as a name so the tests below never depend on how a
#: backslash survives this file's own escaping.
BACKSLASH = chr(92)


def _typescript_files():
    return {path: body for path, body in templates.FILES.items()
            if path.endswith((".ts", ".tsx"))}


# --- the emitted code must be syntactically sound ------------------------

def test_the_fixtures_css_escape_regex_closes_its_character_class():
    body = templates.FILES["src/fixtures/index.ts"]
    line = next(ln for ln in body.splitlines() if "CSS.escape" in ln)
    # Built from char codes so the expectation cannot itself be misread:
    # the class must hold an ESCAPED BACKSLASH (two chars) before the `]`.
    good = '/["' + BACKSLASH * 2 + ']/g'
    bad = '/["' + BACKSLASH + ']/g'
    assert good in line, line
    assert bad not in line.replace(good, ""), line


def test_no_emitted_typescript_escapes_a_closing_bracket_inside_a_class():
    """The whole family of lost-backslash defects, not just the one instance
    above. `["\]` closes its character class; `["\\]` — a lone escaped
    bracket — does not, and the regex literal then runs to end of line."""
    broken = '["' + BACKSLASH + ']'
    offenders = []
    for path, body in _typescript_files().items():
        for number, line in enumerate(body.splitlines(), start=1):
            if broken in line and ('["' + BACKSLASH * 2 + ']') not in line:
                offenders.append(f"{path}:{number}: {line.strip()}")
    assert not offenders, "escaped `]` inside a character class: " + "; ".join(offenders)


# --- the scaffold must not teach the traps it was bitten by --------------

_BARE_IS_VISIBLE = re.compile(r"\breturn\s+this\.\w+\.isVisible\(\)")


def test_no_base_class_probes_with_bare_isvisible():
    """`isVisible()` does not wait. A base class that returns it hands the
    defect to every page object and component in the project."""
    offenders = [path for path, body in _typescript_files().items()
                 if _BARE_IS_VISIBLE.search(body)]
    assert not offenders, offenders


def test_auth_setup_waits_for_optional_controls_rather_than_probing():
    body = templates.FILES["tests/auth.setup.ts"]
    # Comments here DISCUSS isVisible() at length (explaining why it is
    # wrong), so only real code lines count.
    code = [ln for ln in body.splitlines()
            if not ln.lstrip().startswith(("//", "*", "/*"))]
    offenders = [ln.strip() for ln in code if ".isVisible(" in ln]
    assert not offenders, offenders
    assert "waitFor({ state: 'visible'" in body


# --- reusability: the traps are solved ONCE, in the base class -----------

def test_base_page_exposes_the_interaction_helpers():
    """Each of these exists because its hand-rolled equivalent shipped and
    failed against the real application. They belong in one place so a fix
    lands once rather than once per spec."""
    body = templates.FILES["src/pages/BasePage.ts"]
    for helper in ("fillAndCommit", "typeAndCommit", "expectFieldValue",
                   "clickWhenEnabled", "appeared"):
        assert helper in body, f"BasePage is missing {helper}"


def test_fill_and_commit_actually_blurs():
    """The whole point of the helper: `.fill()` fires `input`, never
    `change`. Without the blur an app that commits on `change` holds an empty
    model while the DOM shows every typed value."""
    body = templates.FILES["src/pages/BasePage.ts"]
    helper = body[body.index("fillAndCommit"):]
    assert ".blur()" in helper[:400], helper[:400]


def test_waits_exposes_the_optional_control_probe():
    body = templates.FILES["src/utils/waits.ts"]
    assert "export async function appeared" in body
    assert ".catch(() => false)" in body


# --- structure ------------------------------------------------------------

def test_scaffold_is_idempotent_and_never_overwrites(tmp_path):
    first = framework_scaffolder.run(tmp_path, verbose=False)
    assert first["scaffolded"] is True
    assert framework_scaffolder.is_scaffolded(tmp_path) is True

    marker = tmp_path / "src/pages/BasePage.ts"
    marker.write_text("// hand-tuned by a human\n", encoding="utf-8")

    second = framework_scaffolder.run(tmp_path, verbose=False)
    assert second["created"] == [], "a re-run must create nothing"
    assert marker.read_text(encoding="utf-8") == "// hand-tuned by a human\n"


def test_every_declared_directory_exists_after_scaffolding(tmp_path):
    framework_scaffolder.run(tmp_path, verbose=False)
    for relative in templates.DIRECTORIES:
        assert (tmp_path / relative).is_dir(), relative


# --- maintenance: a workspace must be able to learn it is behind ----------

def test_a_stale_scaffold_file_is_reported_as_drifted(tmp_path):
    """NEVER OVERWRITES is right, but on its own it leaves no upgrade path:
    a workspace scaffolded once keeps its `BasePage.ts` forever, so a fix
    made in the template reaches only NEW projects. Both real examples were
    silent — a fixtures file that did not compile, and an `auth.setup.ts`
    that failed every sign-in — and every existing workspace would have kept
    the broken copy with no signal at all."""
    framework_scaffolder.run(tmp_path, verbose=False)
    (tmp_path / "src/pages/BasePage.ts").write_text("// old version\n", encoding="utf-8")

    result = framework_scaffolder.run(tmp_path, verbose=False)

    assert "src/pages/BasePage.ts" in result["drifted"]
    assert result["created"] == [], "drift reporting must not overwrite"
    assert (tmp_path / "src/pages/BasePage.ts").read_text(encoding="utf-8") == "// old version\n"


def test_an_untouched_scaffold_reports_no_drift(tmp_path):
    framework_scaffolder.run(tmp_path, verbose=False)
    assert framework_scaffolder.run(tmp_path, verbose=False)["drifted"] == []


def test_files_a_project_owns_are_not_reported_as_drift(tmp_path):
    """A tuned `playwright.config.ts` is the POINT of never-overwriting.
    Reporting it every run would train the reader to ignore the whole
    report — which is how a real BasePage regression goes unnoticed."""
    framework_scaffolder.run(tmp_path, verbose=False)
    (tmp_path / "playwright.config.ts").write_text("// tuned by us\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# our project\n", encoding="utf-8")

    drifted = framework_scaffolder.run(tmp_path, verbose=False)["drifted"]

    assert "playwright.config.ts" not in drifted
    assert "README.md" not in drifted
