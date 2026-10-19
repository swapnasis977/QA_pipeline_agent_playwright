"""`auth.setup` must prove it reached the application, not just that a
password box went away.

THE FAILURE THIS PREVENTS, observed end to end on a real run:

  - auth.setup reported PASS.
  - Every test then failed at step 1 with "element(s) not found" on a
    dashboard locator.
  - `GET <dashboard url>` returned HTTP 302, and a live DOM inspection
    reported `logged_in: False` with zero elements.
  - The saved session held 18 cookies — almost all `login.microsoftonline.com`
    — and `origins: 0`, i.e. no localStorage/sessionStorage at all.

The entire success condition was:

    await expect(page.locator(PASSWORD_FIELD)).toHaveCount(0);

"No password box on screen" is true on the dashboard — and equally true on a
consent screen, an error page, a KMSI interstitial, or a blank redirect. So a
broken sign-in reported green, and the real failure surfaced three stages
later disguised as a locator problem, sending the Reviewer and Self-Healer
chasing selectors on a page that was never the dashboard.

Two additions, both cheap and both decisive:
  1. Assert the browser actually landed on the app (URL is the app's own
     origin, not the identity provider's).
  2. Assert the saved state carries something for that origin. An empty
     `origins` array means any app that keeps its token in web storage —
     which is most SPAs behind Azure AD — restored nothing usable.

Run with:  python -m pytest tests/test_auth_setup_proves_sign_in.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from qa_pipeline.framework import templates  # noqa: E402

_AUTH = templates.AUTH_SETUP


def test_the_password_box_check_is_not_the_only_evidence():
    """It may stay — it is a useful early signal — but on its own it passed a
    sign-in that never reached the app."""
    assert "toHaveCount(0" in _AUTH, "the existing check should remain"
    # Counting `expect(` rather than `await expect(`: the landing checks are
    # synchronous assertions on a URL string, not on a locator.
    assert _AUTH.count("expect(") > 1, \
        "a single assertion cannot distinguish 'signed in' from 'redirected'"


def test_it_asserts_the_browser_landed_on_the_application():
    """The identity provider's own pages have no password box either."""
    assert "page.url()" in _AUTH or "waitForURL" in _AUTH, \
        "nothing checks WHERE the browser ended up"
    assert "login.microsoftonline.com" in _AUTH, \
        "the IDP origin should be named as the thing we must NOT be on"


def test_it_verifies_the_saved_state_is_usable():
    """18 cookies and `origins: 0` is what a broken capture looks like."""
    assert "origins" in _AUTH, "nothing inspects the saved storage state"
    assert "storageState(" in _AUTH


def test_a_useless_session_fails_loudly_rather_than_saving_quietly():
    """The whole point: this must stop the run here, where the cause is
    obvious, instead of leaking into 20 steps of misleading locator errors."""
    after_save = _AUTH.split("storageState(")[-1]
    assert "throw new Error" in after_save or "expect(" in after_save, \
        "an unusable session must fail the setup, not be saved silently"


def test_the_diagnosis_names_the_likely_cause():
    """A failure message that says what to check next is worth more than one
    that only says something is wrong."""
    lowered = _AUTH.lower()
    assert "sessionstorage" in lowered or "localstorage" in lowered, \
        "the message should point at web-storage tokens, the usual culprit"


def test_the_workspace_copy_matches_the_template():
    """The scaffold skips files that already exist, so a fixed template does
    not reach a workspace scaffolded earlier. This is the check that the two
    have not drifted — the same trap as the recorded page objects."""
    workspace = _ROOT / "workspaces" / "Example App" / "tests" / "auth.setup.ts"
    if not workspace.is_file():
        return
    assert workspace.read_text(encoding="utf-8") == _AUTH, \
        "workspaces/Example App/tests/auth.setup.ts has drifted from the template"
