"""A plain <button aria-label="Zoom In"> must get a role locator, not a positional chain.

The failure this prevents: only elements that spelled out role="..." could get a
role+name locator, so the generated page object bound ~24 toolbar buttons to
`div > section:nth-of-type(2) > section > button:nth-of-type(N)`. Inserting one button
shifted every locator after it, and member names came out as the whole aria-label.
The accessible names were right there.

Needs a real browser (the logic is JavaScript run in the page); skipped without one.
"""

import json

import pytest

sync_api = pytest.importorskip("playwright.sync_api")

from utils import dom_inspector  # noqa: E402

HTML = """
<div><section><section>
  <button aria-label="Zoom In">+</button>
  <button aria-label="Close">x</button>
  <button aria-label="Close">x</button>
  <button aria-label="Reset">r</button>
  <button aria-label="Reset all">R</button>
  <input type="range" aria-label="Brightness" min="0" max="1" value="1">
  <a href="/home">Home</a>
</section></section></div>
"""


@pytest.fixture(scope="module")
def snapshot():
    try:
        with sync_api.sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(HTML)
            snap = dom_inspector._snapshot(page)
            browser.close()
            return {(e["name"], i): e for i, e in enumerate(snap["elements"])}
    except Exception as exc:  # no browser installed on this machine
        pytest.skip(f"no usable Chromium: {exc}")


def _by_name(snapshot, name):
    return [e for (n, _), e in snapshot.items() if n == name]


def test_a_unique_plain_button_gets_a_role_locator(snapshot):
    (el,) = _by_name(snapshot, "Zoom In")
    assert el["locatorType"] == "role"
    assert json.loads(el["locator"]) == {"role": "button", "name": "Zoom In"}
    assert el["role"] == "button"          # the verifier matches on this field


def test_sliders_and_links_get_their_implicit_role(snapshot):
    assert _by_name(snapshot, "Brightness")[0]["role"] == "slider"
    assert _by_name(snapshot, "Home")[0]["role"] == "link"


def test_two_buttons_with_one_name_do_not_get_an_ambiguous_role_locator(snapshot):
    # getByRole would match both -> a strict-mode violation at run time.
    assert all(e["locatorType"] == "css" for e in _by_name(snapshot, "Close"))


def test_a_name_that_is_a_substring_of_another_is_not_trusted(snapshot):
    # getByRole('button', {name: 'Reset'}) also matches "Reset all".
    assert _by_name(snapshot, "Reset")[0]["locatorType"] == "css"
    assert _by_name(snapshot, "Reset all")[0]["locatorType"] == "role"
