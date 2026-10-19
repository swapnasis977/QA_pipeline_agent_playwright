"""
Shared real-DOM inspection for Agent 2 (Automation Planner) and Agent 6
(Self-Healer) — replaces the "[PLACEHOLDER]" stubs for inspect_live_dom /
inspect_current_dom with real browser inspection against APP_BASE_URL, logged
in with APP_TEST_USER / APP_TEST_PASSWORD from the environment.

Two backends, both behind the same inspect(context_text) -> dict contract, so
neither automation_planner.py nor self_healer.py needed ANY change to switch:

  - Playwright MCP server (utils/playwright_mcp_client.py) — the
    section 4's FIRST-CHOICE mechanism. Used when USE_PLAYWRIGHT_MCP=true.
  - Direct in-process Playwright (this file's own sync_playwright() session,
    below) — the explicitly sanctioned FALLBACK, used when the MCP
    path is off or fails. This was previously the only implementation.

IMPORTANT — the direct-Playwright fallback PREFERS HEADED (headless=False).
Confirmed in practice against this app: OLD headless Chromium either got a
broken client-side render (blank page behind a "Page Not Found" modal) or,
with a hand-edited User-Agent, an outright 404 from the server — while an
identical login in HEADED mode rendered the real dashboard correctly.
Something in this app's stack (client JS check, or a reverse-proxy/WAF rule)
treats old-headless sessions differently.

Headed is therefore the default and the only VERIFIED mode. But a headed
launch is impossible on a runner with no display, and previously that meant
the launch threw, every case's inspection came back "unavailable", and the
Coding Agent fell back to guessing selectors for the whole suite. So:

  - DOM_INSPECT_HEADLESS is unset (default) -> try headed, and if that
    launch fails, fall back to Chromium's NEW headless mode rather than
    giving up. New headless is the real browser binary, not the separate
    stripped-down implementation old headless used, so the differences that
    broke this app do not straightforwardly apply — but it is NOT the
    verified configuration, so every snapshot taken that way carries a
    "launch_warning" through to planner_doc.json.
  - DOM_INSPECT_HEADLESS=new  -> go straight to new headless (CI).
  - DOM_INSPECT_HEADLESS=old  -> legacy headless, the mode known to break
    here; provided only for re-testing that claim.

USE_PLAYWRIGHT_MCP=true remains the preferred CI path (see below), since the
MCP server owns its own browser lifecycle.

The browser/context/page (direct path) and the MCP session (MCP path) are
each a lazy-initialized, process-wide singleton so a run that calls inspect()
many times (one per test case) pays the login cost once, not per call.
"""

import atexit
import contextlib
import os
import re
import time
from pathlib import Path

from utils import dom_navigation
from utils import playwright_mcp_client as _mcp
from utils import route_map

# Moved to utils/dom_snapshot.py; re-exported so existing imports keep working.
from utils.dom_snapshot import (  # noqa: F401
    _DEFAULT_SNAPSHOT_ELEMENTS,
    _MCP_SNAPSHOT_LINE_RE,
    _origin_of,
    _parse_mcp_snapshot,
    _safe,
    _snapshot,
)

_playwright = None
_browser = None
_context = None
_page = None
_login_attempted = False
_login_ok = False
#: Bounded re-login attempts on a cached page whose login failed (see _get_page).
_relogin_attempts = 0
_MAX_RELOGINS = 2

#: Set once a browser launch has definitively failed, so the failure is paid
#: ONCE per process instead of once per test case. Without this, a run on a
#: machine where the browser can't launch re-ran the whole
#: sync_playwright().start() + failed-launch sequence for every inspect()
#: call — 74 cases meant 74 failed launches and 74 identical warnings.
_launch_failed = False

#: Why it failed, so inspect() can report the real cause instead of
#: defaulting to "APP_BASE_URL not set" (which is what it used to say for
#: ANY unavailable page, including a launch failure — a misleading note that
#: then got persisted verbatim into planner_doc.json).
_launch_error = ""


def verify_login() -> tuple[bool, str]:
    """Cheap pre-flight check: can we actually log into the app right now?

    Pays the ONE browser launch + login attempt this costs, up front, instead
    of discovering the same failure 4 repair attempts deep on EVERY case —
    which is what happened before this existed: a single bad login (missing
    credentials, a broken IDP, a wrong APP_BASE_URL) burned the whole repair
    budget on all 89 cases in a real run, each one independently rediscovering
    the identical failure with no way to short-circuit.

    Returns (ok, reason) — reason is empty on success, otherwise the best
    diagnosis available (launch failure, missing credentials, or whatever
    `_looks_logged_in` already prints for a stalled IDP/MFA/bad-credentials
    screen).
    """
    page = _get_page()
    if page is None:
        return False, _launch_error or "could not launch a browser"
    if not os.environ.get("APP_TEST_USER") or not os.environ.get("APP_TEST_PASSWORD"):
        return False, ("APP_TEST_USER/APP_TEST_PASSWORD are empty — check TEST_ENV "
                        "and the matching LOCAL_/CENTRAL_ triple in .env")
    if _login_ok:
        return True, ""
    return False, ("login did not complete — see the warning(s) printed above "
                    "for the specific reason (stalled IDP prompt, MFA, or "
                    "rejected credentials)")


def _headless_preference() -> str:
    """How to launch Chromium: "headed" (default), "new" or "old".

    Default stays HEADED because that is the only mode verified to render
    this app correctly (see the module docstring). DOM_INSPECT_HEADLESS
    overrides it for environments where headed is impossible:

      unset / "0" / "false"  -> headed          (default, verified)
      "new" / "1" / "true"   -> --headless=new  (real Chromium, new headless)
      "old"                  -> legacy headless (the mode known to break here)

    "new" is the meaningful CI option: Chromium's new headless mode is the
    actual browser binary rather than the separate stripped-down
    implementation old headless used, so the client-JS/user-agent
    differences that broke this app under old headless largely do not apply.
    It is still not the verified-good configuration, so it is opt-in and
    reported in the snapshot rather than assumed equivalent.
    """
    value = os.environ.get("DOM_INSPECT_HEADLESS", "").strip().lower()
    if value in ("", "0", "false", "no", "headed"):
        return "headed"
    if value == "old":
        return "old"
    return "new"


def _start_playwright():
    """Starts a Playwright driver process.

    A separate one-line function purely as a seam: it is the only place the
    real `playwright` package is touched, so the launch-policy tests
    (tests/test_dom_inspector_launch.py) can exercise mode selection and
    the headed→headless fallback — the behavior that only MATTERS on a
    machine with no display — without starting a browser or an asyncio
    loop. Imported lazily so `import utils.dom_inspector` still works with
    playwright absent, which the MCP-backend path relies on.
    """
    from playwright.sync_api import sync_playwright

    return sync_playwright().start()


def _launch_browser(playwright, mode: str):
    """Launches Chromium in the given mode (see _headless_preference)."""
    if mode == "headed":
        return playwright.chromium.launch(headless=False)
    if mode == "old":
        return playwright.chromium.launch(headless=True)
    # Chromium's NEW headless mode. Passed as an explicit arg rather than
    # headless=True because Playwright's own `headless=True` still selects
    # old headless on the pinned versions this project supports.
    return playwright.chromium.launch(headless=True, args=["--headless=new"])


#: Max clicks a single inspect() call will chase through a multi-step flow
#: (e.g. dashboard tile -> target screen). Capped so a keyword that matches
#: something unrelated to the intended flow can't wander indefinitely.
_MAX_NAV_STEPS = 4

#: Max keywords pulled from the flow description text per inspect() call.
#: Raised from an earlier cap of 6 — that cap silently dropped keywords
#: appearing later in a longer flow_summary (e.g. "Editing"/"Tool" in
#: "...Face Comparison Tool admin screen..." past the first 6 4+-letter
#: words), so a real dashboard tile match was never even attempted.
_MAX_KEYWORDS = 12


def _get_page():
    """Returns the shared logged-in Page, launching + logging in on first use.

    Returns None when no page can be obtained; `_launch_error` then holds
    the reason, which inspect() reports.
    """
    global _playwright, _browser, _context, _page, _login_attempted, _login_ok
    global _launch_failed, _launch_error, _relogin_attempts

    if _page is not None:
        if _safe(lambda: _page.is_closed(), False) is True:
            # A crashed/closed page would otherwise be handed back forever.
            print("⚠️  DOM inspection: the shared page was closed — relaunching.")
            close()
        else:
            if not _login_ok and _login_attempted and _relogin_attempts < _MAX_RELOGINS:
                # The singleton used to be cached even when login failed, so one
                # bad attempt poisoned every later case. Re-try, bounded.
                _relogin_attempts += 1
                try:
                    base = os.environ.get("APP_BASE_URL", "")
                    if base:
                        _page.goto(base, wait_until="domcontentloaded", timeout=20000)
                    _wait_for_login_form(_page)
                    _login_ok = _attempt_login(_page)
                except Exception as exc:
                    print(f"      ⚠️  DOM inspection: re-login attempt failed ({exc}).")
            return _page
    if _launch_failed:
        return None  # already established this process can't get a browser

    base_url = os.environ.get("APP_BASE_URL", "")
    if not base_url:
        _launch_failed = True
        _launch_error = "APP_BASE_URL is not set"
        return None

    _playwright = _start_playwright()

    # Preferred mode is HEADED — see the module docstring; old headless
    # broke this specific app's rendering, confirmed by direct comparison.
    # A headed launch throws immediately on a runner with no display, so
    # rather than giving up entirely (which made EVERY case's inspection
    # come back unusable on CI), fall back to Chromium's NEW headless mode,
    # which is the real browser binary and does not share old headless's
    # rendering differences. The fallback is recorded in `_launch_error` and
    # surfaced on every snapshot, so a locator read in that mode is never
    # silently presented as coming from the verified configuration.
    preferred = _headless_preference()
    attempts = [preferred] if preferred != "headed" else ["headed", "new"]
    for index, mode in enumerate(attempts):
        try:
            _browser = _launch_browser(_playwright, mode)
            if index > 0:
                _launch_error = (
                    "headed launch failed, fell back to Chromium's new headless mode "
                    "— rendering in this mode is NOT the configuration verified against "
                    "this app, so treat locators from this run as unconfirmed")
                print(f"⚠️  DOM inspection: {_launch_error}.")
            break
        except Exception as exc:
            if index < len(attempts) - 1:
                continue
            _launch_failed = True
            _launch_error = f"could not launch Chromium in {mode} mode ({exc})"
            print(f"⚠️  DOM inspection: {_launch_error}. Every case will be reported "
                  f"as 'unavailable' rather than guessed. On a headless runner either "
                  f"set USE_PLAYWRIGHT_MCP=true (preferred), run behind a virtual "
                  f"display (Xvfb), or set DOM_INSPECT_HEADLESS=new.")
            with contextlib.suppress(Exception):
                _playwright.stop()
            _playwright = None
            return None

    _context = _browser.new_context(ignore_https_errors=True)
    _page = _context.new_page()

    # ONE bounded retry on the initial navigation, same rationale as the
    # login retry below: `_page` is a process-wide singleton, so a FIRST
    # navigation that throws (a transient timeout/DNS blip, not a real
    # outage) used to `return _page` immediately WITHOUT ever setting
    # `_login_attempted` — and every later inspect() call then reused that
    # same never-logged-in page for the rest of the run with `_login_ok`
    # staying False and `_login_attempted` staying False too, so the
    # downstream `if not _login_ok and _login_attempted:` diagnostic never
    # fired either. The result was a SILENT unauthenticated snapshot on
    # every single case for the rest of the process — worse than a loud
    # failure, since nothing in planner_doc.json said why. Now a failed
    # goto still falls through to the login attempt below (best-effort,
    # against whatever did load) so `_login_attempted` is always set and
    # the existing "login not complete" note has a chance to fire.
    goto_error = None
    for goto_attempt in range(2):
        try:
            _page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
            goto_error = None
            break
        except Exception as exc:
            goto_error = exc
            if goto_attempt == 0:
                print(f"⚠️  DOM inspection: could not reach APP_BASE_URL on the first "
                      f"try ({exc}) — retrying once before giving up on navigation.")
                with contextlib.suppress(Exception):
                    _page.wait_for_timeout(1000)
    if goto_error is not None:
        print(f"⚠️  DOM inspection: could not reach APP_BASE_URL after a retry "
              f"({goto_error}). Attempting login anyway against whatever loaded, "
              f"if anything.")

    # Let the SSO redirect chain finish before looking for a login form.
    #
    # This is what blocked an entire real run. `goto(wait_until=
    # "domcontentloaded")` resolves on the FIRST document, which for a
    # SAML/OIDC app is an intermediate auto-submit page — observed against
    # a real Entra ID tenant as
    # `login.microsoftonline.com/<tenant>/saml2?SAMLRequest=...` with title
    # "Redirecting" and, because its form is hidden, ZERO visible elements.
    # _attempt_login then looked for a password field (3s) and a username
    # field on THAT page, found neither, and returned False without ever
    # typing anything. Every case's inspection came back
    # quality="unavailable" with 0 elements, the Coding Agent had no
    # locators for any screen, and all 5 cases went unresolved — a total
    # pipeline starvation caused entirely by not waiting out a redirect.
    _wait_for_login_form(_page)

    _login_attempted = True
    _login_ok = _attempt_login(_page)

    # ONE bounded retry on a failed first attempt, not zero. `_page` is a
    # process-wide singleton reused for every case in the run (see the
    # "one login for the whole run" note elsewhere in this module), so a
    # purely transient first failure — a slow IDP redirect, a one-off
    # render glitch — used to poison every remaining case for the rest of
    # the process with no second chance, each one silently graded
    # dom_inspection_quality="login_page" as if the app were genuinely
    # unreachable. A real credentials/IDP outage still fails both
    # attempts and is reported the same as before.
    if not _login_ok:
        print("      ⚠️  DOM inspection: first login attempt failed — retrying once "
              "before giving up for the rest of this run.")
        try:
            _page.goto(base_url, wait_until="domcontentloaded", timeout=20000)
            _wait_for_login_form(_page)
            _login_ok = _attempt_login(_page)
        except Exception as exc:
            print(f"      ⚠️  DOM inspection: retry navigation failed ({exc}).")

    return _page


#: How long to let an SSO redirect chain settle into an actual login form.
#: Generous on purpose: an enterprise IDP can chain several redirects, and
#: this cost is paid ONCE per process (the page is a singleton), whereas
#: giving up early costs every case its locators.
_LOGIN_FORM_WAIT_MS = 30000

#: Any field that means "a real login form is now on screen". Deliberately
#: broader than _attempt_login's own matchers — this only decides WHEN to
#: start, not what to fill.
_LOGIN_FORM_SELECTOR = (
    'input[type="password"], input[type="email"], input[name*="user" i], '
    'input[name*="email" i], input[id*="user" i], input[id*="email" i], '
    'input[type="text"]'
)


def _wait_for_login_form(page, timeout_ms: int = None) -> bool:
    """Waits for a real login form (or an already-authenticated app) to
    settle after APP_BASE_URL's redirect chain.

    Returns True if a login input appeared. Best-effort: a False here is not
    fatal — the caller still attempts login and reports what it finds — but
    it does distinguish "no form ever appeared" from "we looked too early",
    which the previous code could not.

    Playwright's wait_for_selector survives navigations, so one wait covers
    the whole chain regardless of how many hops it takes.
    """
    timeout_ms = timeout_ms or int(os.environ.get("LOGIN_FORM_WAIT_MS", _LOGIN_FORM_WAIT_MS))
    try:
        page.wait_for_selector(_LOGIN_FORM_SELECTOR, state="visible", timeout=timeout_ms)
        return True
    except Exception:
        pass

    # No login input appeared. Two very different reasons, so say which:
    # the session may already be authenticated (a cached SSO cookie lands
    # straight on the dashboard), or the redirect genuinely never resolved.
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=10000)
    title = _safe(lambda: page.title())
    if "redirect" in str(title).lower():
        print(f"⚠️  DOM inspection: still on an SSO redirect page after "
              f"{timeout_ms}ms (title={title!r}, url={_safe(lambda: page.url)[:80]}). "
              f"No login form appeared — inspection will report 'unavailable'. "
              f"Raise LOGIN_FORM_WAIT_MS if this tenant is just slow.")
    return False


#: ADVANCE — "go to the password step". Deliberately NARROW: only wordings
#: that mean "there is another step after this one".
#:
#: This is split from _SUBMIT_PATTERN because conflating them broke login
#: against a real Entra ID tenant. The old single pattern
#: ("next|continue|sign ?in|log ?in|submit") was used for BOTH the advance
#: click and the final submit, so on the email screen it matched whichever
#: of "Next"/"Sign in" came first — and the presence of an advance control
#: is exactly what distinguishes a two-step IDP from a single-step form.
#: Keeping them separate makes that distinction usable (see _attempt_login).
_ADVANCE_PATTERN = re.compile(r"^\s*(next|continue)\s*$", re.I)

#: SUBMIT — the final "log me in" control. Broad, and checked only once a
#: password has actually been entered, so a premature match is impossible.
_SUBMIT_PATTERN = re.compile("sign ?in|log ?in|submit|next|continue", re.I)

#: Kept as an alias: it is imported/patched by tests and referenced in
#: the heal path. Points at the submit pattern, its historical
#: broad meaning.
_NEXT_STEP_PATTERN = _SUBMIT_PATTERN


def _find_username_field(page, timeout: int = 3000):
    """Same field-matching heuristic used by both the single-step and
    two-step login paths: label-based first (most robust to markup
    changes), then common attribute patterns."""
    for locator in (
        lambda: page.get_by_label(re.compile("user|email|login", re.I)).first,
        lambda: page.locator(
            'input[name*="user" i], input[id*="user" i], '
            'input[name*="email" i], input[id*="email" i], '
            'input[type="email"], input[type="text"]'
        ).first,
    ):
        try:
            candidate = locator()
            candidate.wait_for(state="visible", timeout=timeout)
            return candidate
        except Exception:
            continue
    return None


def _attempt_login(page) -> bool:
    """Best-effort login covering BOTH shapes seen in practice:
    - Single-step: username + password fields on the same screen (Keycloak
      and similar), filled together, one submit.
    - Two-step: email-only screen -> "Next" -> password-only screen ->
      "Sign in" (Microsoft Entra ID / Okta / most enterprise SSO IDPs).
      Confirmed necessary against this app's real Microsoft login via live
      DOM inspection — a single-step-only heuristic silently returns False
      here every time, since no password field exists on the first screen.
    Returns whether the page looks logged-in afterward (heuristic: no
    visible password field remains)."""
    username = os.environ.get("APP_TEST_USER", "")
    password = os.environ.get("APP_TEST_PASSWORD", "")
    if not username or not password:
        return False

    # ONE flow for both shapes, ordered so it cannot get them wrong:
    # username -> (advance, if this IDP has a separate password step) ->
    # password -> submit.
    #
    # The previous version branched up-front on "is a password field visible
    # right now?", and that test is not a reliable discriminator: Entra ID
    # PRE-RENDERS its password input on the email screen, so once the SSO
    # redirect actually settled (see _wait_for_login_form) the check saw a
    # visible password field, took the single-step branch, typed the password
    # into a step that wasn't active yet, and clicked "Sign in" — which on
    # that screen behaves as "Next". Result: the password screen with an
    # EMPTY password box, login never completing, and every case's inspection
    # reporting the login page as the target screen.
    #
    # The presence of a dedicated ADVANCE control ("Next"/"Continue") is the
    # discriminator that actually holds: a two-step IDP has one, a
    # username+password form does not.
    username_field = _find_username_field(page)
    if username_field is None:
        return False  # no login form on this page at all
    try:
        username_field.fill(username)
    except Exception:
        return False

    advanced = _click_matching_button(page, _ADVANCE_PATTERN)
    if advanced:
        # Two-step IDP: the password field belongs to the screen we just
        # navigated to, so re-resolve it rather than reusing any earlier handle.
        password_field = page.locator('input[type="password"]').first
        try:
            password_field.wait_for(state="visible", timeout=15000)
        except Exception:
            return False  # never reached a password step
    else:
        password_field = page.locator('input[type="password"]').first
        try:
            password_field.wait_for(state="visible", timeout=5000)
        except Exception:
            # No advance control AND no password field: some IDPs only reveal
            # it after Enter on the username.
            with contextlib.suppress(Exception):
                username_field.press("Enter")
            password_field = page.locator('input[type="password"]').first
            try:
                password_field.wait_for(state="visible", timeout=15000)
            except Exception:
                return False

    try:
        password_field.fill(password)
    except Exception:
        return False
    # Verify the value actually landed before submitting — on a step that is
    # present but not yet active, fill() can silently no-op, and submitting
    # an empty password just bounces back to this same screen.
    with contextlib.suppress(Exception):
        if password_field.input_value() != password:
            print("      ⚠️  DOM inspection: the password field did not accept the value "
                  "(the step may not be active yet) — not submitting.")
            return False

    if not _click_matching_button(page, _SUBMIT_PATTERN):
        with contextlib.suppress(Exception):
            password_field.press("Enter")

    with contextlib.suppress(Exception):  # SPAs with persistent sockets never go idle — not fatal
        page.wait_for_load_state("networkidle", timeout=15000)
    # Belt-and-suspenders: the networkidle wait above is best-effort and can
    # throw/no-op on an SPA with persistent sockets, leaving zero guaranteed
    # wait between submit and the login-success check just below — give the
    # post-submit navigation/render a fixed floor to land before checking.
    with contextlib.suppress(Exception):
        page.wait_for_timeout(1000)

    _dismiss_stay_signed_in(page)
    return _looks_logged_in(page)


#: The "Stay signed in?" (KMSI) prompt Entra ID shows AFTER a successful
#: password step. Its two controls are `<input type="submit">`, not
#: `<button>`, and their ids are stable across tenants: idBtn_Back = "No",
#: idSIButton9 = "Yes". Matching by id first is what makes this reliable —
#: the previous role-and-exact-name lookup (`get_by_role("button",
#: name=/^no$/)`) is fragile against localization and against however a
#: given tenant labels the control, and when it missed, the run sat on the
#: KMSI screen: password submitted, credentials accepted, app never reached.
#: Confirmed against a real tenant, where inspection stalled here with the
#: page reading "Stay signed in? / Do this to reduce the number of times you
#: are asked to sign in."
_KMSI_DECLINE_SELECTORS = ("#idBtn_Back", 'input[value="No" i]', 'button[value="No" i]')
_KMSI_MARKER_RE = re.compile(r"stay signed in|keep me signed in|reduce the number of times", re.I)


def _dismiss_stay_signed_in(page) -> bool:
    """Answers the KMSI prompt with "No" so the session stays scoped to this
    run. Returns whether a prompt was found and dismissed.

    "No" rather than "Yes" deliberately: this is an automation session
    against a shared test tenant, and persisting the sign-in would leave a
    long-lived token behind on the runner.
    """
    body = _safe(lambda: page.inner_text("body"), "")
    if not _KMSI_MARKER_RE.search(body or ""):
        return False

    for selector in _KMSI_DECLINE_SELECTORS:
        try:
            control = page.locator(selector).first
            if control.count() and control.is_visible(timeout=1500):
                control.click(timeout=3000)
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("networkidle", timeout=15000)
                print("      DOM inspection: dismissed the 'Stay signed in?' prompt.")
                return True
        except Exception:
            continue

    # Role-based last resort, for a tenant that really does render a <button>.
    with contextlib.suppress(Exception):
        control = page.get_by_role("button", name=re.compile(r"^\s*no\s*$", re.I)).first
        if control.is_visible(timeout=1500):
            control.click(timeout=3000)
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=15000)
            return True

    print("      ⚠️  DOM inspection: a 'Stay signed in?' prompt is on screen but no "
          "decline control could be clicked — inspection will report the IDP page, "
          "not the app.")
    return False


_is_idp_url = dom_navigation.is_idp_url
_is_destructive = dom_navigation.is_destructive
_KEYWORD_STOPWORDS = dom_navigation.KEYWORD_STOPWORDS


def _password_field_visible(page) -> bool:
    """True when a password input is still visible: rejected credentials on an
    app that serves its own login form at its own URL leave one on screen."""
    def _check():
        loc = page.locator('input[type="password"]')
        return any(loc.nth(i).is_visible() for i in range(min(loc.count(), 3)))
    return bool(_safe(_check, False))


def _looks_logged_in(page) -> bool:
    """Whether the browser is actually in the APP now.

    Checked by URL host first. The previous heuristic — "the password field
    is gone" — cannot distinguish the post-login app from the intermediate
    KMSI prompt or an MFA challenge, both of which also lack a password
    field. That made a stalled login report success, and the caller then
    labelled a snapshot of the IDP as the target screen.
    """
    url = _safe(lambda: page.url, "")
    if url and not _is_idp_url(url) and not _password_field_visible(page):
        return True

    # Still on the IDP. Say WHY, since the three reasons need different fixes.
    body = _safe(lambda: page.inner_text("body"), "") or ""
    if _KMSI_MARKER_RE.search(body):
        print("      ⚠️  Login stalled on the 'Stay signed in?' prompt.")
    elif re.search(r"approve|verification code|authenticator|two-step|verify your identity",
                   body, re.I):
        print("      ⚠️  Login requires MFA/second-factor approval, which this "
              "unattended inspection cannot complete. Use a test account with MFA "
              "exempted for automation, or set USE_PLAYWRIGHT_MCP with a pre-authenticated "
              "session.")
    elif re.search(r"incorrect|didn't work|isn't right|locked|disabled", body, re.I):
        print("      ⚠️  The IDP rejected the credentials — check APP_TEST_USER / "
              "APP_TEST_PASSWORD in .env.")
    return False


def _click_matching_button(page, name_pattern: "re.Pattern") -> bool:
    """Clicks the first visible button matching name_pattern. Returns
    whether a click actually happened, so callers can fall back to
    pressing Enter when no matching button exists."""
    try:
        btn = page.get_by_role("button", name=name_pattern).first
        btn.click(timeout=3000)
        return True
    except Exception:
        return False


def cached_route_elements() -> list:
    """Elements from every route snapshot taken in this process.

    A page object also holds the SETUP steps that lead to its screen (New
    Registration, Continue, the upload input), which live on earlier screens. A
    one-screen snapshot can never corroborate those, so verification pools what
    this run has already observed on its other routes.
    """
    pooled = []
    for _taken_at, snapshot in _route_snapshots.values():
        pooled.extend(snapshot.get("elements") or [])
    return pooled


def _dismiss_blocking_dialogs(page, max_dismissals: int = 3) -> bool:
    """Closes visible modal/dialog elements with an Ok/Close/Dismiss-style
    button. Observed in practice: this app shows a 'Page Not Found' dialog
    that would otherwise be ALL the snapshot ever sees, hiding the real
    underlying page on every single inspection call. Returns whether
    anything was dismissed.

    'cancel' is deliberately NOT matched. Dismissing an informational dialog
    and CANCELLING a flow are different acts, and this function only has a
    mandate for the first. Matching it aborted the very screens it was called
    to help capture: this app's Context Information popup is a `role=dialog`
    carrying a Cancel button, so the crawler navigated to it correctly and
    then instantly cancelled out of it, and the registration flow was recorded
    as nothing but the dashboard behind it.
    """
    dismissed_any = False
    for _ in range(max_dismissals):
        try:
            dialog = page.locator('[role="dialog"], [role="alertdialog"]').first
            if not dialog.is_visible(timeout=1000):
                break
            btn = dialog.get_by_role(
                "button", name=re.compile(r"\b(ok|close|dismiss)\b", re.I)
            ).first
            btn.click(timeout=2000)
            page.wait_for_timeout(300)
            dismissed_any = True
        except Exception:
            break
    return dismissed_any


#: A confirm dialog asking whether to abandon work in progress. This app puts
#: one up ("Are you sure you want to quit this registration? All data captured
#: for this registration will be lost") whenever the page is navigated away from
#: mid-flow — which is precisely what a declared route's reset-to-home step
#: does on every crawl after the first.
_LEAVE_PROMPT_RE = re.compile(
    r"\b(quit|leave|discard|abandon|will be lost|unsaved)\b", re.I)

#: The affirmative answer to one. Deliberately narrow: "yes"/"discard"/"leave"
#: only, never "ok", so this cannot swallow an informational dialog that
#: `_dismiss_blocking_dialogs` should be handling instead.
_LEAVE_CONFIRM_RE = re.compile(r"^\s*(yes|discard|leave|don.?t save)\s*$", re.I)


def _confirm_leaving(page, tries: int = 3) -> bool:
    """Answers an 'are you sure you want to quit?' prompt with YES.

    Called ONLY straight after a deliberate reset to the app's home page,
    where abandoning whatever flow the previous crawl was left inside is the
    entire intent of the reset. Anywhere else this would be a destructive
    guess, which is why it is not folded into `_dismiss_blocking_dialogs`.

    Without it, one crawl that ends inside a wizard poisons every later crawl
    in the same process: the prompt is `data-bs-backdrop="static"` with a
    Yes/No pair, so it matches none of the ok/close/dismiss patterns, and its
    backdrop intercepts every click — Playwright reports "subtree intercepts
    pointer events" for elements it can otherwise see perfectly. Observed
    exactly that way: every route after the first failed to click anything.

    The prompt is also raced against the navigation that causes it, so this
    polls briefly rather than looking once.
    """
    for _ in range(tries):
        try:
            dialog = page.locator('[role="dialog"], [role="alertdialog"]').filter(
                has_text=_LEAVE_PROMPT_RE).first
            if not dialog.is_visible(timeout=1500):
                page.wait_for_timeout(700)
                continue
            dialog.get_by_role("button", name=_LEAVE_CONFIRM_RE).first.click(timeout=2000)
            page.wait_for_timeout(1200)
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=5000)
            return True
        except Exception:
            page.wait_for_timeout(500)
    return False


def _mcp_find(elements: list, name_pattern: str, roles: tuple = ()) -> dict:
    rx = re.compile(name_pattern, re.I)
    for el in elements:
        if roles and el["role"] not in roles:
            continue
        if el.get("ref") and rx.search(el.get("name", "")):
            return el
    return None


def _inspect_via_mcp(context_text: str) -> dict:
    """MCP-backed equivalent of _get_page()+_attempt_login()+_snapshot()
    below — navigates, best-effort logs in, best-effort clicks toward a
    flow-description keyword, and returns a snapshot, all via Playwright MCP
    tool calls instead of an in-process Playwright session."""
    base_url = os.environ.get("APP_BASE_URL", "")
    if not base_url:
        return {"note": "APP_BASE_URL not set — cannot perform real DOM inspection.",
                "logged_in": False}

    global _mcp_logged_in, _mcp_login_attempted
    if not _mcp_navigated_once():
        _mcp.call_tool("browser_navigate", {"url": base_url})
        _mark_mcp_navigated()
        _mcp_login_attempted = True
        _mcp_logged_in = _mcp_attempt_login()
    else:
        # Every case must start from home, like the direct path does; without
        # this each inspection began wherever the previous case's clicks left
        # the session, so the same case text reached different screens
        # depending on run order.
        with contextlib.suppress(Exception):
            _mcp.call_tool("browser_navigate", {"url": base_url})

    snapshot_text = _mcp.call_tool("browser_snapshot")
    elements = _parse_mcp_snapshot(snapshot_text)

    # Multi-step, same rationale as the direct-Playwright path in inspect()
    # below: re-scan the REMAINING keywords against a fresh snapshot after
    # each successful click, so a multi-click flow (e.g. dashboard tile ->
    # target screen) can be followed instead of stopping after one click.
    keywords = _extract_keywords(context_text)
    remaining = list(keywords)
    navigation_path = []
    for _step in range(_MAX_NAV_STEPS):
        target = None
        matched_kw = None
        for kw in remaining:
            target = _mcp_find([e for e in elements if not _is_destructive(e.get("name", ""))],
                               r"(?<![A-Za-z])" + re.escape(kw),
                               roles=("link", "button", "menuitem", "tab"))
            if target:
                matched_kw = kw
                break
        if not target:
            break
        try:
            _mcp.call_tool("browser_click", {"element": target["name"], "ref": target["ref"]})
            snapshot_text = _mcp.call_tool("browser_snapshot")
            elements = _parse_mcp_snapshot(snapshot_text)
            navigation_path.append(matched_kw)
            remaining.remove(matched_kw)
        except Exception:
            remaining.remove(matched_kw)
            continue

    navigated = bool(navigation_path)

    result = {
        "elements": elements,
        "raw_snapshot": snapshot_text[:4000],
        "logged_in": _mcp_logged_in,
        "navigated_by_keyword": navigated,
        "navigation_path": navigation_path,
        "backend": "playwright_mcp",
    }
    if not _mcp_logged_in and _mcp_login_attempted:
        result["note"] = ("Login was attempted via MCP but no password field disappeared "
                          "afterward — treat 'elements' below as the LOGIN page's real "
                          "fields, not the post-login app.")
    return result


#: Whether the MCP session has already navigated to APP_BASE_URL + attempted
#: login — set once, same "pay login cost once" intent as the direct path's
#: module-level _login_attempted/_login_ok globals.
_mcp_navigated = False
_mcp_logged_in = False
_mcp_login_attempted = False


def _mcp_navigated_once() -> bool:
    return _mcp_navigated


def _mark_mcp_navigated():
    global _mcp_navigated
    _mcp_navigated = True


def _mcp_attempt_login() -> bool:
    """Best-effort login via MCP tool calls — MCP equivalent of
    _attempt_login() above, driven by browser_type/browser_click against
    refs parsed out of a browser_snapshot instead of Playwright locators.
    Covers the same two shapes: single-step (username+password together)
    and two-step (email -> Next -> password -> Sign in, e.g. Microsoft
    Entra ID / Okta) — see _attempt_login()'s docstring for why the
    two-step case matters; a single-step-only heuristic silently fails
    every time against this app's real login."""
    username = os.environ.get("APP_TEST_USER", "")
    password = os.environ.get("APP_TEST_PASSWORD", "")
    if not username or not password:
        return False

    snapshot_text = _mcp.call_tool("browser_snapshot")
    elements = _parse_mcp_snapshot(snapshot_text)

    password_field = _mcp_find(elements, "password", roles=("textbox",))
    username_field = _mcp_find(elements, "user|email|login", roles=("textbox",))

    if not password_field:
        # Two-step path: fill email, click Next/Continue, re-snapshot, look
        # for a password field on the screen that appears next.
        if not username_field:
            return False  # no login form found on the landing page at all
        try:
            _mcp.call_tool("browser_type", {
                "element": username_field["name"], "ref": username_field["ref"], "text": username,
            })
        except Exception:
            return False
        if not _mcp_click_matching(elements, "next|continue|sign ?in|log ?in|submit"):
            with contextlib.suppress(Exception):
                _mcp.call_tool("browser_press_key", {"key": "Enter"})
        snapshot_text = _mcp.call_tool("browser_snapshot")
        elements = _parse_mcp_snapshot(snapshot_text)
        password_field = _mcp_find(elements, "password", roles=("textbox",))
        if not password_field:
            return False  # never reached a password step — genuinely can't log in
        try:
            _mcp.call_tool("browser_type", {
                "element": password_field["name"], "ref": password_field["ref"], "text": password,
            })
        except Exception:
            return False
        if not _mcp_click_matching(elements, "sign ?in|log ?in|submit|next"):
            with contextlib.suppress(Exception):
                _mcp.call_tool("browser_press_key", {"key": "Enter"})
    else:
        # Single-step path: username + password both present together.
        if not username_field:
            return False
        try:
            _mcp.call_tool("browser_type", {
                "element": username_field["name"], "ref": username_field["ref"], "text": username,
            })
            _mcp.call_tool("browser_type", {
                "element": password_field["name"], "ref": password_field["ref"], "text": password,
            })
        except Exception:
            return False
        if not _mcp_click_matching(elements, "sign in|log ?in|submit"):
            with contextlib.suppress(Exception):
                _mcp.call_tool("browser_press_key", {"key": "Enter"})

    try:
        post_login_text = _mcp.call_tool("browser_snapshot")
        post_login_elements = _parse_mcp_snapshot(post_login_text)
        # Optional "Stay signed in?" (KMSI) prompt — dismiss ("No") so it
        # isn't mistaken for a still-pending login screen.
        kmsi_no = _mcp_find(post_login_elements, "^no$", roles=("button",))
        if kmsi_no:
            with contextlib.suppress(Exception):
                _mcp.call_tool("browser_click", {"element": kmsi_no["name"], "ref": kmsi_no["ref"]})
                post_login_text = _mcp.call_tool("browser_snapshot")
                post_login_elements = _parse_mcp_snapshot(post_login_text)
        return _mcp_find(post_login_elements, "password", roles=("textbox",)) is None
    except Exception:
        # Could not even read the page afterwards: unknown is NOT success.
        # (This used to return True, so a dead session "logged in".)
        return False


def _mcp_click_matching(elements: list, name_pattern: str) -> bool:
    """Clicks the first button/link matching name_pattern via MCP. Returns
    whether a click actually happened, so callers can fall back to
    pressing Enter when no matching control exists."""
    target = _mcp_find(elements, name_pattern, roles=("button", "link"))
    if not target:
        return False
    try:
        _mcp.call_tool("browser_click", {"element": target["name"], "ref": target["ref"]})
        return True
    except Exception:
        return False


#: How long to keep waiting for a single-page app to actually paint a screen
#: after the document is ready. The dashboard of this project's own app spends
#: several seconds behind a full-screen spinner while it fetches its module
#: list — well past `networkidle`, which never settles at all on an app that
#: polls in the background.
_APP_READY_TIMEOUT_MS = 30000


def _wait_for_app_ready(page, timeout_ms: int = _APP_READY_TIMEOUT_MS,
                        allow_reload: bool = True) -> bool:
    """Blocks until the SPA has painted something addressable, or gives up.

    `wait_until="domcontentloaded"` fires when the shell document is ready,
    which for a single-page app is BEFORE it has rendered any of the screen,
    and `networkidle` is no better here: this app keeps a background poll
    open, so that wait always burns its full timeout and then returns with
    the spinner still up. The fixed `wait_for_timeout(1500)` that followed
    was a guess, and on the dashboard it was the wrong one — the Explorer
    snapshotted an empty shell and reported "no elements captured" for a
    screen that loads perfectly a few seconds later. Three of four declared
    screens were unreachable for this reason alone.

    So wait for the thing actually being waited FOR: at least one visible,
    clickable element. That is the precondition `_snapshot` needs, it is true
    of every real screen including modal ones, and it lets a fast screen
    continue immediately instead of always paying a fixed sleep.
    """
    ready_js = """() => {
      const visible = el => {
        const r = el.getBoundingClientRect();
        return r.width > 1 && r.height > 1;
      };
      return Array.from(
        document.querySelectorAll('a,button,[role=button],input,select,textarea')
      ).some(visible);
    }"""
    def _poll(budget_ms: int) -> bool:
        deadline = time.time() + (budget_ms / 1000.0)
        while time.time() < deadline:
            try:
                if page.evaluate(ready_js):
                    # One short settle so a screen caught mid-paint finishes
                    # its remaining controls before the snapshot reads the DOM.
                    page.wait_for_timeout(500)
                    return True
            except Exception:
                # Navigation mid-evaluate — retry against the new document.
                pass
            page.wait_for_timeout(250)
        return False

    if _poll(timeout_ms // 2):
        return True

    # RELOAD ONCE before giving up. This app's dashboard intermittently comes
    # back as a live shell that never paints: `document.readyState` is
    # "complete", `<main>` holds its 28KB of markup, and not one element has a
    # box — observed sitting that way for a full 38 seconds while its local
    # helper services returned 404. Without this the Explorer recorded "no
    # elements captured" for a screen that works, and a run's grounding
    # quietly depended on which way that race fell.
    #
    # The reload helps but does NOT always fix it — this comment used to
    # claim "a reload does, every time" and that has since been measured
    # false: 5 consecutive resets of this app's dashboard came back blank 3
    # times, each staying blank through the full poll budget AND this
    # reload. So callers must treat a False return as a real, common
    # outcome (the app or its backing services are down), not as a rare
    # timing edge worth clicking through anyway — see
    # `_navigate_declared_route`, which used to discard this result.
    if not allow_reload:
        return False
    try:
        page.reload(wait_until="domcontentloaded", timeout=20000)
    except Exception:
        return False
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=10000)
    return _poll(timeout_ms // 2)


def _first_visible(page, selector: str):
    """The first VISIBLE match for `selector`, else `.first`.

    Ids are not always unique in this app (`#btnContinue` is also a hidden
    "Resume Current Registration" Cancel button), and `.first` is DOM order, so
    it clicked the hidden one and the route silently stayed put.
    """
    matches = page.locator(selector)
    try:
        for i in range(min(matches.count(), 10)):
            if matches.nth(i).is_visible():
                return matches.nth(i)
    except Exception:
        pass
    return matches.first


def _upload_route_step(page, spec: str) -> str:
    """Route step "upload=<css selector>|<file or folder>".

    A screen that only exists after an image is chosen (an editing tool, a
    preview) cannot be reached by clicks alone. The selector is the control
    that opens the file chooser (or the <input type=file> itself); the path is
    one file, or a folder from which a fixed image is picked (first jpg/png
    under 5 MB by name; set QA_UPLOAD_RANDOM=1 for a random one).
    """
    import random
    selector, _, source = spec.partition("|")
    selector, source = selector.strip(), source.strip()
    if not selector or not source:
        return None
    path = Path(source)
    if path.is_dir():
        # jpg/png under 5 MB: what this app's upload accepts. A random pick of
        # anything in the folder could be rejected, and a rejected upload
        # leaves the route on Portrait Capture. Smallest-first (the app processes the upload before opening the editor) keeps runs
        # repeatable; QA_UPLOAD_RANDOM=1 restores a random pick.
        images = sorted((p for p in path.iterdir()
                         if p.suffix.lower() in (".jpg", ".jpeg", ".png")
                         and p.stat().st_size <= 5 * 1024 * 1024),
                        key=lambda p: (p.stat().st_size, p.name))
        if not images:
            return None
        path = random.choice(images) if os.environ.get("QA_UPLOAD_RANDOM") == "1" else images[0]
    if not path.is_file():
        return None
    try:
        target = page.locator(selector).first
        is_input = (target.evaluate("el => el.tagName") or "").upper() == "INPUT"
        if is_input:
            target.set_input_files(str(path))
        else:
            with page.expect_file_chooser(timeout=5000) as chooser:
                target.click(timeout=4000)
            chooser.value.set_files(str(path))
    except Exception:
        return None
    with contextlib.suppress(Exception):
        page.wait_for_load_state("networkidle", timeout=10000)
    page.wait_for_timeout(1500)
    return f"upload {path.name!r} via {selector!r}"


def _click_route_step(page, label) -> str:
    """Performs one declared-route step. Returns what it did, or None.

    An explicit "css=" step exists for the elements no name can address:
    `_snapshot` falls back to a css locator precisely when it could NOT
    derive a role/name one — an <a> with no href has no link role, so
    `get_by_role("link", name=...)` can never match it however exactly the
    name is written. The sidebar submenu item that opens this app's registration
    flow is one of those, and without this the whole registration branch was
    uncrawlable. Write such a step as "css=li:nth-of-type(2) > ul > li > a".

    Named steps are matched EXACTLY across the roles a human would call
    clickable, then by aria-label/title/text — the labels in a route map were
    written down by someone reading the real UI, so a near-miss means the map
    is stale and should be fixed rather than papered over with fuzzy matching.
    """
    if str(label).startswith("upload="):
        return _upload_route_step(page, str(label)[7:])

    if str(label).startswith("css="):
        selector = str(label)[4:]
        done = False
        try:
            _first_visible(page, selector).click(timeout=4000)
            done = True
        except Exception:
            # Playwright refuses to click an element it considers not
            # visible/stable. Menu anchors that navigate via an onclick
            # handler routinely fail that test while being perfectly clickable
            # by a real user — this app's own
            # `<a href="javascript:;" onclick="...navigateToRegistrationModule(true)">`
            # is one, and it is the ONLY way into the registration flow.
            # Dispatching the DOM click runs the same handler.
            try:
                _first_visible(page, selector).evaluate("el => el.click()")
                done = True
            except Exception:
                return None
        if done:
            with contextlib.suppress(Exception):
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            page.wait_for_timeout(1500)
            return f"click css {selector!r}"
        return None

    for build in (
        lambda: page.get_by_role("link", name=label, exact=True).first,
        lambda: page.get_by_role("button", name=label, exact=True).first,
        lambda: page.get_by_role("tab", name=label, exact=True).first,
        lambda: page.get_by_role("menuitem", name=label, exact=True).first,
        # aria-label / title carry the name for elements with no implicit
        # role — exactly the population `_snapshot` falls back to a css
        # locator for, and so exactly the population a route map most often
        # needs to click through.
        lambda: page.get_by_label(label, exact=True).first,
        lambda: page.get_by_title(label, exact=True).first,
        lambda: page.get_by_text(label, exact=True).first,
    ):
        try:
            element = build()
        except Exception:
            continue
        try:
            element.click(timeout=4000)
        except Exception:
            # Same escape hatch the "css=" branch above already has, and for
            # a reason observed on this app's own Context Information popup:
            # the transaction-type dropdown menu renders INSIDE a Bootstrap
            # modal whose `.modal-footer` overlaps it geometrically. The
            # menu item is visible, enabled and stable — Playwright says so
            # — but the hit test hands the click to the footer, so a real
            # click can never land and the whole registration-capture route was
            # unfollowable.
            #
            # Only dispatch on an element that actually resolved AND is
            # visible. Without the visibility guard this would happily
            # "click" the hidden native <option> that get_by_text matches
            # for the same label and report success for a step that changed
            # nothing — a false pass is worse than the failure it replaces.
            try:
                if not element.is_visible():
                    continue
                element.evaluate("el => el.click()")
            except Exception:
                continue
        with contextlib.suppress(Exception):
            page.wait_for_load_state("domcontentloaded", timeout=5000)
        page.wait_for_timeout(800)
        return f"click {label!r}"
    return None


def _route_landed(page, route: dict) -> bool:
    """True when the route declares no `expect`, or its selector is on the page."""
    expect = route.get("expect")
    if not expect:
        return True
    try:
        page.locator(expect).first.wait_for(state="attached", timeout=8000)
        return True
    except Exception:
        print(f"      ⚠️  Route '{route['name']}' ran every click but its expected "
              f"element {expect!r} is not on the page — the route did not land.")
        return False


def _navigate_declared_route(page, route: dict) -> list:
    """Follows a declared route: direct URL first, then exact-label clicks.

    Returns the steps actually taken, or [] if the route could not be
    followed — an unfollowable route is reported rather than silently
    treated as success, because a snapshot of the wrong screen presented as
    the right one is exactly what this whole mechanism exists to prevent.

    Clicks use EXACT accessible names (get_by_role over link/button/tab/
    menuitem, then a text fallback) rather than the fuzzy substring
    matching of the keyword path — the labels here were written down by a
    human who read the real UI, so a near-miss means the map is stale and
    should be fixed, not papered over.
    """
    taken = []
    base = (os.environ.get("APP_BASE_URL", "") or "").rstrip("/")

    if route.get("url"):
        target = route["url"]
        # A relative route url resolves against the app's ORIGIN, not against
        # APP_BASE_URL verbatim. APP_BASE_URL routinely carries a path AND a
        # query (this project's own is
        # ".../exampleApp/dashboard/home/?customer_code=demo"), so
        # concatenating produced ".../dashboard/home/?customer_code=demo/reports"
        # — a URL that cannot exist. Only the scheme+host are meaningful as a
        # base for a declared route.
        url = target if target.startswith("http") else _origin_of(base) + "/" + target.lstrip("/")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            # Same SPA settle the click path below already does, and for the
            # same reason: "domcontentloaded" fires when the shell document is
            # ready, which for a single-page app is BEFORE it has rendered any
            # of the screen. Without this the snapshot was taken against an
            # empty shell, and the Explorer reported "no elements captured" for
            # a screen that loads perfectly — a url route looked strictly worse
            # than the click route it is supposed to be more reliable than.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=10000)
            if not _wait_for_app_ready(page):
                print(f"      ⚠️  Route '{route['name']}': {url} never painted a "
                      f"clickable element — snapshotting the shell anyway.")
            taken.append(f"goto {url}")
        except Exception as exc:
            print(f"      ⚠️  Route '{route['name']}': could not open {url} ({exc}).")
            return []
    elif route.get("clicks"):
        # RESET to the app's landing page before following a click path.
        #
        # The page is a process-wide singleton (one login for the whole run),
        # so it is left wherever the PREVIOUS case navigated to. A click path
        # that starts from a dashboard tile therefore worked for the first
        # case and then failed for every later one — observed exactly that
        # way on a real run: case 1 reported
        # navigation_source="declared_route", cases 2-5 reported "no
        # clickable element named 'Identification. Click to access
        # Identification module' on the current screen", because the browser
        # was still inside the Identification screen from case 1 and the
        # dashboard tiles were no longer present.
        try:
            page.goto(base, wait_until="domcontentloaded", timeout=20000)
            # An SPA renders its tiles after the document is ready.
            with contextlib.suppress(Exception):
                page.wait_for_load_state("networkidle", timeout=10000)
            # A BLANK APP IS NOT A STALE ROUTE MAP — SAY WHICH ONE IT IS.
            #
            # This return value was discarded, while the `url` branch above
            # has always checked it. The cost was a misdiagnosis, not a
            # missed step: when the reload leaves the dashboard blank, the
            # step loop below still runs, finds nothing (there is nothing on
            # the page AT ALL), and reports "no clickable element named
            # 'Registration. Click to access Registration module'" followed by
            # "check the route map against the real UI". So an operator is
            # sent to fix routes.json — which is correct and unchanged —
            # while the actual fault is that the app never painted.
            #
            # Measured on this app: 5 resets, 3 came back with ZERO visible
            # clickable elements and stayed that way through the reload
            # `_wait_for_app_ready` already performs. (Its docstring says a
            # reload fixes this "every time"; that is no longer true, so do
            # not treat a False here as merely slow.)
            if not _wait_for_app_ready(page):
                print(f"      ⚠️  Route '{route['name']}': the app home page never "
                      f"painted a clickable element (blank shell) — this is the app "
                      f"or its backing services, NOT the route map. Not following the "
                      f"click path against an empty page.")
                return []
            # Answer the abandon-this-flow prompt this navigation may have
            # raised, BEFORE anything else: its static backdrop blocks every
            # click on the page underneath it.
            _confirm_leaving(page)
            _dismiss_blocking_dialogs(page)
            taken.append("reset to app home")
        except Exception as exc:
            print(f"      ⚠️  Route '{route['name']}': could not return to the app home "
                  f"page before following the click path ({exc}).")
            return []

    steps = list(route.get("clicks", []))
    for label in steps:
        # DISMISS LAZILY, NEVER EAGERLY.
        #
        # This loop used to clear blocking dialogs after every click. That is
        # correct for junk encountered on the way (this app throws a "Page Not
        # Found" dialog that would otherwise be all any snapshot ever sees)
        # and catastrophic for a route that walks THROUGH a modal: the
        # Context Information popup's close control is named "Button, close
        # the modal box", so the eager dismissal closed the popup the moment
        # the route stepped into it, and every step after that had nothing to
        # click. The destination modal was destroyed the same way.
        #
        # So the step is attempted first. Only a step that FAILS is treated as
        # possibly blocked, and only then are dialogs cleared and the step
        # retried once. A dialog that is not in the way is now left alone —
        # which is the only way a route can both survive junk and end on a
        # modal.
        taken_step = _click_route_step(page, label)
        if taken_step is None and _dismiss_blocking_dialogs(page):
            taken_step = _click_route_step(page, label)
        if taken_step is None:
            print(f"      ⚠️  Route '{route['name']}': no clickable element named "
                  f"{label!r} on the current screen.")
            return []
        taken.append(taken_step)

    return taken


def _extract_keywords(text: str) -> list:
    return dom_navigation.extract_keywords(text, _MAX_KEYWORDS)


#: Roles checked, in order, before falling back to a plain text match — see
#: _find_keyword_target.
_CLICKABLE_ROLES = ("link", "button", "tab", "menuitem")


def _find_keyword_target(page, keyword: str):
    """Finds the best clickable element for one keyword from the multi-step
    navigation loop in inspect().

    Tries clickable roles (link/button/tab/menuitem) by accessible name
    FIRST, and only falls back to a bare `get_by_text` substring match when
    no such role exists. `get_by_text` alone matches ANY element containing
    the keyword ANYWHERE in the DOM — a breadcrumb, a page header, a tooltip,
    an unrelated wrapper div — not just something a user could actually
    click, so a keyword that happens to appear in copy near the real target
    could click (or "succeed" on) the wrong element and silently derail a
    multi-step flow. Restricting to clickable roles first removes that class
    of false match while keeping the text fallback for the elements `_snapshot`
    itself falls back to a css locator for (e.g. an <a> with no href/role).

    `loc.count()` is a cheap, non-waiting existence check, so scanning
    multiple roles across multiple remaining keywords doesn't pay a timeout
    for every miss — only a real candidate pays the short is_visible() wait.
    """
    rx = re.compile(r"(?<![A-Za-z])" + re.escape(keyword), re.I)
    for role in _CLICKABLE_ROLES:
        try:
            loc = page.get_by_role(role, name=rx)
            if loc.count() == 0:
                continue
            candidate = loc.first
            if candidate.is_visible(timeout=300):
                if _is_destructive(_safe(lambda: candidate.inner_text(timeout=200), "")
                                   or _safe(lambda: candidate.get_attribute("aria-label"), "")):
                    continue
                return candidate
        except Exception:
            continue
    try:
        candidate = page.get_by_text(rx).first
        if candidate.is_visible(timeout=700) and not _is_destructive(
                _safe(lambda: candidate.inner_text(timeout=200), "")):
            return candidate
    except Exception:
        pass
    return None


#: route name -> (monotonic time, snapshot). Only consulted when a caller passes
#: `cache_ttl`: the Planner and Coding Agent inspect the SAME screen once per
#: case, and every inspection re-walks login -> registration -> upload in a real
#: browser (a minute or more each). The crawler and the Self-Healer never pass
#: it — they want the screen as it is right now.
_route_snapshots = {}


def inspect(context_text: str, cache_ttl: float = 0) -> dict:
    """Main entry point for both inspect_live_dom(flow_description) and
    inspect_current_dom(context_hint). Logs in once (cached), makes a
    best-effort click toward whatever the flow description mentions, then
    returns a real element snapshot instead of a placeholder string.

    Tries the Playwright MCP backend first when USE_PLAYWRIGHT_MCP=true;
    falls back to the direct in-process Playwright session below on any
    MCP failure (server not running, `mcp` package not installed, etc), so
    a misconfigured MCP setup degrades to the previous behavior instead of
    breaking the pipeline.
    """
    if _mcp.available():
        try:
            return _inspect_via_mcp(context_text)
        except Exception as exc:
            print(f"⚠️  Playwright MCP inspection failed ({exc}) — "
                  f"falling back to direct Playwright.")

    page = _get_page()
    if page is None:
        # Reports the ACTUAL reason. This previously always said
        # "APP_BASE_URL not set", including when the real cause was a
        # browser that wouldn't launch — and that string is persisted
        # verbatim into planner_doc.json, so it sent whoever read the
        # artifact to check an environment variable that was already set.
        return {"note": f"No DOM inspection: {_launch_error or 'browser unavailable'}.",
                "logged_in": False,
                "elements": [],
                "launch_error": _launch_error}

    dialog_dismissed = _dismiss_blocking_dialogs(page)

    # DECLARED route first (utils/route_map). A human states once, per
    # screen, what it's called and how to reach it — information no keyword
    # heuristic can derive: this app's real tile is named "Image Editing
    # Tool" while the test cases call it "Face Comparison Tool admin", so no
    # word from the case text appears anywhere in the UI and the guessing
    # below never leaves the dashboard. When a route matches, navigation is
    # deterministic and the keyword passes are skipped entirely.
    route = route_map.match(context_text)
    route_failed = ""
    if route and cache_ttl > 0:
        hit = _route_snapshots.get(route["name"])
        if hit and time.monotonic() - hit[0] < cache_ttl:
            import copy
            cached = copy.deepcopy(hit[1])
            cached["cached"] = True
            return cached
    if route:
        print(f"      DOM inspection: using declared {route_map.describe(route)}")
        route_path = _navigate_declared_route(page, route)
        if route_path:
            # NO dialog dismissal here, unlike the keyword paths below. A
            # declared route states exactly where to land, so whatever is on
            # screen at the end of it IS the target — and for a screen that is
            # itself a modal (this app's Context Information popup, any wizard
            # step, any confirm-first flow) dismissing at this point can only
            # destroy the thing we navigated to. Junk encountered ON THE WAY is
            # still cleared: `_navigate_declared_route` dismisses between
            # clicks, where the mandate is genuine.
            snapshot = _snapshot(page)
            snapshot["logged_in"] = _login_ok
            snapshot["navigated_by_keyword"] = True
            snapshot["navigation_path"] = route_path
            snapshot["navigation_source"] = "declared_route"
            if not _route_landed(page, route):
                # Every click ran, yet the screen the route promises is not
                # there. Say so on the snapshot; consumers that would file it
                # under that screen's name (the crawler) must refuse it.
                snapshot["route_unverified"] = route["name"]
            snapshot["route"] = route["name"]
            snapshot["dialog_dismissed"] = dialog_dismissed
            snapshot["backend"] = "playwright_direct"
            snapshot["launch_mode"] = _headless_preference()
            if _launch_error:
                snapshot["launch_warning"] = _launch_error
            if cache_ttl > 0 and not snapshot.get("route_unverified"):
                import copy
                _route_snapshots[route["name"]] = (time.monotonic(), copy.deepcopy(snapshot))
            return snapshot
        print(f"      ⚠️  Declared route '{route['name']}' did not resolve against the "
              f"live app — falling back to keyword navigation. Check the route map "
              f"against the real UI ({route_map.route_map_path()}).")
        route_failed = route["name"]

    # Best-effort MULTI-STEP navigation: a single flow (e.g. "open a case,
    # navigate to the Face Comparison Tool admin screen") often needs several
    # clicks — a one-shot single click left DOM inspection stuck on
    # whatever the first click happened to land on (frequently still the
    # login/dashboard screen), so Coding Agent had no real data for the
    # actual target screen and had to guess its selectors. Re-scan the
    # REMAINING keywords against the page after each successful click
    # (not the original page), so each step can match something that only
    # appeared after the previous step — capped at _MAX_NAV_STEPS so a
    # keyword that matches something unrelated can't wander indefinitely.
    keywords = _extract_keywords(context_text)
    remaining = list(keywords)
    navigation_path = []
    for _step in range(_MAX_NAV_STEPS):
        clicked = None
        for kw in remaining:
            candidate = _find_keyword_target(page, kw)
            if candidate is None:
                continue
            try:
                candidate.click(timeout=3000)
            except Exception:
                continue
            # Same SPA-paint wait the declared-route path already relies on
            # (see _wait_for_app_ready's docstring) — "domcontentloaded" alone
            # fires before an SPA has rendered the next screen, so re-scanning
            # for the NEXT keyword immediately afterward routinely found
            # nothing visible yet and the multi-step chain stopped after one
            # click even though the click itself worked. allow_reload=False:
            # a mid-flow click that doesn't paint in time should be reported
            # as "stopped here", not papered over with a full page reload that
            # would also discard whatever state the click just produced.
            if not _wait_for_app_ready(page, timeout_ms=8000, allow_reload=False):
                with contextlib.suppress(Exception):
                    page.wait_for_load_state("domcontentloaded", timeout=5000)
            clicked = kw
            break
        if clicked is None:
            break
        navigation_path.append(clicked)
        remaining.remove(clicked)
        dialog_dismissed = _dismiss_blocking_dialogs(page) or dialog_dismissed

    # FUZZY fallback when exact substring matching found nothing at all:
    # the case text's domain wording ("Face Comparison Tool admin screen")
    # frequently doesn't literally appear anywhere in the app's own UI
    # (the real tile is named "Image Editing Tool") — no substring keyword
    # can ever bridge that gap. Score every visible button/link by shared
    # 4+-letter words with the FULL context text (not just the first
    # _MAX_KEYWORDS) and click the best-scoring one if any word overlaps
    # at all — "Tool" alone is enough to prefer "Image Editing Tool" over
    # unrelated tiles like "Role Management".
    if not navigation_path:
        context_words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", context_text or "")
                         if w.lower() not in _KEYWORD_STOPWORDS}
        if context_words:
            best_score, best_locator, best_text = 0, None, None
            try:
                candidates = page.get_by_role("button").or_(page.get_by_role("link"))
                count = min(candidates.count(), 40)
            except Exception:
                count = 0
            for i in range(count):
                try:
                    el = candidates.nth(i)
                    if not el.is_visible(timeout=200):
                        continue
                    text = el.inner_text(timeout=200)
                except Exception:
                    continue
                if _is_destructive(text):
                    continue
                words = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text or "")
                         if w.lower() not in _KEYWORD_STOPWORDS}
                score = len(words & context_words)
                if score > best_score:
                    best_score, best_locator, best_text = score, el, text
            # Require at least 2 shared 4+-letter words, not just 1 — a
            # single shared word (e.g. "Tool") is too weak a signal and can
            # click into an unrelated tile purely by coincidence.
            if best_locator is not None and best_score >= 2:
                try:
                    best_locator.click(timeout=3000)
                    if not _wait_for_app_ready(page, timeout_ms=8000, allow_reload=False):
                        with contextlib.suppress(Exception):
                            page.wait_for_load_state("domcontentloaded", timeout=5000)
                    navigation_path.append(f"[fuzzy match, score={best_score}] {(best_text or '').strip()[:40]}")
                    dialog_dismissed = _dismiss_blocking_dialogs(page) or dialog_dismissed
                except Exception:
                    pass

    navigated = bool(navigation_path)

    snapshot = _snapshot(page)
    snapshot["logged_in"] = _login_ok
    if route_failed:
        # A declared route was supposed to land here and did not; whatever the
        # keyword fallback reached is NOT the screen the route names.
        snapshot["route_failed"] = route_failed
    snapshot["navigated_by_keyword"] = navigated
    snapshot["navigation_path"] = navigation_path
    snapshot["dialog_dismissed"] = dialog_dismissed
    snapshot["backend"] = "playwright_direct"
    snapshot["launch_mode"] = _headless_preference()
    if _launch_error:
        # A headed-to-new-headless fallback happened. The locators are real,
        # but they came from a rendering mode not verified against this app,
        # so that qualification travels WITH the snapshot into
        # planner_doc.json rather than living only in a console line nobody
        # re-reads later.
        snapshot["launch_warning"] = _launch_error
    if not _login_ok and _login_attempted:
        snapshot["note"] = ("Login was attempted but the page still shows a password "
                            "field afterward — treat 'elements' below as the LOGIN "
                            "page's real fields, not the post-login app.")
    return snapshot


def close():
    """Best-effort cleanup — registered via atexit, not required to be called manually."""
    global _browser, _playwright, _page, _context
    for obj in (_page, _context, _browser):
        try:
            if obj:
                obj.close()
        except Exception:
            pass
    try:
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _page = _context = _browser = _playwright = None


atexit.register(close)
