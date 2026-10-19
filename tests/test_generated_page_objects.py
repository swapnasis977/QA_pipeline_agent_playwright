"""Page objects come from the plan, so specs and page objects cannot disagree.

THE FAILURE. Specs were generated in independent chunks and each chunk wrote
the SHARED page objects itself. One chunk defined `brightnessSlider`, another
rewrote the file without it, a third spec called `clickRegistrationTile` that no
chunk had written. `tsc` rejected a third of the files and the gate asked for a
whole-stage regeneration that made the same mistake. The gate also refuses a
page object whose locator is not the one the Planner grounded, so a model's
creative page object was rejected on that count too.

Run with:  python -m pytest tests/test_generated_page_objects.py -q
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agents import pom_builder  # noqa: E402

_PLANS = [{
    "test_case_id": "TC-1",
    "elements": [
        {"screen": "Image Editing Tool", "logical_name": "Brightness, Use Left/Right Arrow Keys",
         "locator": 'page.locator("#brightness")'},
        {"screen": "Image Editing Tool", "logical_name": "Contrast",
         "locator": 'page.locator("#contrast")'},
        {"screen": "Image Editing Tool", "logical_name": "Toggle grid, Press Enter",
         "locator": 'page.locator("div > button:nth-of-type(9)")'},
        {"screen": "Registration", "logical_name": "New Registration",
         "locator": 'page.locator("#btnNewRegistration")'},
    ],
}, {
    "test_case_id": "TC-2",
    "elements": [
        # the same element again, from a second case: must not duplicate
        {"screen": "Image Editing Tool", "logical_name": "Brightness",
         "locator": 'page.locator("#brightness")'},
        # not a locator expression the page object can hold
        {"screen": "Image Editing Tool", "logical_name": "Junk", "locator": "not a locator"},
        {"screen": "", "logical_name": "No screen", "locator": 'page.locator("#x")'},
    ],
}]


def _by_class(pages):
    return {p["class_name"]: p for p in pages}


def test_one_page_object_per_screen_with_every_grounded_element_once():
    pages = _by_class(pom_builder.build_page_objects(_PLANS))
    assert set(pages) == {"ImageEditingToolPage", "RegistrationPage"}
    members = [name for name, _logical in pages["ImageEditingToolPage"]["members"]]
    assert members == ["brightness", "contrast", "toggleGrid"]


def test_the_locator_in_the_file_is_exactly_the_one_the_planner_grounded():
    page = _by_class(pom_builder.build_page_objects(_PLANS))["ImageEditingToolPage"]
    assert 'this.brightness = page.locator("#brightness");' in page["content"]
    assert page["locators_used"] == [
        'page.locator("#brightness")', 'page.locator("#contrast")',
        'page.locator("div > button:nth-of-type(9)")']


def test_the_shape_matches_the_scaffolds_base_page_contract():
    content = _by_class(pom_builder.build_page_objects(_PLANS))["RegistrationPage"]["content"]
    assert "export class RegistrationPage extends BasePage" in content
    assert "readonly ready: Locator;" in content and "super(page);" in content
    # declared without initializers, assigned in the constructor body (TS2729)
    assert not re.search(r"readonly \w+: Locator = ", content)


def test_an_id_based_locator_is_preferred_as_the_ready_marker():
    page = _by_class(pom_builder.build_page_objects(_PLANS))["ImageEditingToolPage"]
    assert 'this.ready = page.locator("#brightness");' in page["content"]


def test_two_elements_with_the_same_name_get_distinct_properties():
    plans = [{"elements": [
        {"screen": "S", "logical_name": "Save", "locator": 'page.locator("#a")'},
        {"screen": "S", "logical_name": "Save", "locator": 'page.locator("#b")'},
        {"screen": "S", "logical_name": "path", "locator": 'page.locator("#c")'},
    ]}]
    members = [n for n, _ in pom_builder.build_page_objects(plans)[0]["members"]]
    assert len(set(members)) == 3 and "path" not in members


def test_no_grounded_elements_means_no_page_objects():
    assert pom_builder.build_page_objects([{"elements": []}, {}]) == []
    assert pom_builder.describe([]) == ""


def test_the_prompt_lists_only_members_that_exist():
    text = pom_builder.describe(pom_builder.build_page_objects(_PLANS))
    assert "ImageEditingToolPage" in text and "- brightness" in text
    assert "Do NOT emit these files" in text


# --- the route helper -------------------------------------------------------

_ROUTES = {
    "app-dashboard": {"url": "app/home", "clicks": [], "aliases": []},
    "tool": {"url": "", "clicks": ["Open", "css=#go:visible", "upload=#f|C:\\imgs"], "aliases": []},
}


def test_the_route_helper_carries_every_step_including_upload():
    helper = pom_builder.build_route_helper(_ROUTES)
    assert helper["path"] == "src/utils/routes.ts"
    assert helper["route_names"] == ["app-dashboard", "tool"]
    assert '"css=#go:visible"' in helper["content"]
    assert "upload=#f|C:\\\\imgs" in helper["content"]  # JSON-escaped backslash
    assert "export async function followRoute" in helper["content"]


def test_the_typescript_regexes_survive_the_python_string():
    content = pom_builder.build_route_helper(_ROUTES)["content"]
    assert r"/\.(jpe?g|png)$/i" in content and r"replace(/^\//, '')" in content


def test_a_profile_without_routes_gets_no_helper():
    assert pom_builder.build_route_helper({}) == {}
    assert pom_builder.describe_routes({}) == ""


# --- the hygiene check that reported a name as a copy of itself ------------

def test_the_same_name_bound_to_the_same_locator_in_two_tests_is_not_a_duplicate():
    from qa_pipeline.verifiers import spec_hygiene
    source = (
        "test('TC-1', async ({ page }) => {\n"
        "  const blurSlider = page.locator('#blur');\n"
        "  await blurSlider.click();\n"
        "});\n"
        "test('TC-2', async ({ page }) => {\n"
        "  const blurSlider = page.locator('#blur');\n"
        "  await blurSlider.click();\n"
        "});\n")
    errors, _repairs = spec_hygiene._duplicate_bindings(source)
    assert errors == []


def test_two_different_names_for_one_locator_is_still_flagged():
    from qa_pipeline.verifiers import spec_hygiene
    source = ("const blurSlider = page.locator('#blur');\n"
              "const blurControl = page.locator('#blur');\n")
    errors, _repairs = spec_hygiene._duplicate_bindings(source)
    assert len(errors) == 1


# --- the slider helper -------------------------------------------------------

def test_a_plan_with_sliders_gets_the_slider_helper_and_others_do_not():
    helper = pom_builder.build_slider_helper([{"elements": [{"logical_name": "Brightness"}]}])
    assert helper["path"] == "src/utils/sliders.ts"
    assert "setSliderPercent" in helper["content"] and "expectSliderPercent" in helper["content"]
    assert pom_builder.build_slider_helper([{"elements": [{"logical_name": "Save"}]}]) == {}
    assert pom_builder.build_slider_helper([]) == {}


def test_the_slider_prompt_teaches_step_not_percent():
    text = pom_builder.describe_sliders(
        pom_builder.build_slider_helper([{"elements": [{"logical_name": "Opacity"}]}]))
    assert "STEP per arrow key" in text and "0.05" in text and "never fill()" in text.lower()
    assert pom_builder.describe_sliders({}) == ""


# --- failed route attempts must not leave work open on the server ------------

def test_a_failed_route_attempt_cleans_up_before_the_retry():
    """Observed: retries that walked away from a half-started transaction grew the
    in-progress list from 168 to 213 records in a few runs, slowing the very page
    the next attempt needed and making it fail more often."""
    content = pom_builder.build_route_helper({"r": {"url": "", "clicks": ["x"]}})["content"]
    catch = content[content.index("lastError = error;"):]
    assert "await cleanupRoute(page)" in catch.split("route-retry")[0]


def test_every_cleanup_route_is_tried_independently():
    routes = {
        "app": {"url": "home", "clicks": []},
        "zz-cleanup-open-transaction": {"clicks": ["css=#btnCancel:visible", "css=#DlgBtn_Yes"]},
        "zz-cleanup-open-dialog": {"clicks": ["css=#cancelDialog"]},
    }
    content = pom_builder.build_route_helper(routes)["content"]
    assert "startsWith('zz-cleanup-')" in content
    assert "zz-cleanup-open-dialog" in content and "zz-cleanup-open-transaction" in content
