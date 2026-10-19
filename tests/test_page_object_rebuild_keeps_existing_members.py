"""Rebuilding a page object for a new story must not delete what the last story used.

The failure this prevents: page objects are rebuilt from the CURRENT run's plans only.
A second story on the same screen overwrote the file with its own elements, so every
member the first story's specs referenced vanished and those specs stopped compiling
- found only when the next run's compile gate rejected files that had been fine.
"""

from agents import pom_builder


def _plan(*elements):
    return [{"test_case_id": "S-1", "elements": [
        {"screen": "Zoom Tool", "logical_name": name, "locator": loc} for name, loc in elements]}]


FIRST = _plan(("Zoom In", "page.getByRole('button', { name: 'Zoom In' })"),
              ("Zoom Out", "page.getByRole('button', { name: 'Zoom Out' })"))
SECOND = _plan(("Reset", "page.getByRole('button', { name: 'Reset all' })"))


def _page(plans, existing=None):
    (page,) = pom_builder.build_page_objects(plans, existing=existing)
    return page


def test_a_second_story_adds_to_the_page_object_instead_of_replacing_it():
    first = _page(FIRST)
    second = _page(SECOND, existing={first["path"]: first["content"]})

    props = [prop for prop, _ in second["members"]]
    assert {"zoomIn", "zoomOut", "reset"} <= set(props)
    assert "this.zoomIn = page.getByRole('button', { name: 'Zoom In' })" in second["content"]


def test_a_member_keeps_its_name_when_its_element_is_planned_again():
    first = _page(FIRST)
    # Same element, described differently this time: the name a spec already uses stays.
    again = _plan(("Zoom in button", "page.getByRole('button', { name: 'Zoom In' })"))
    second = _page(again, existing={first["path"]: first["content"]})

    assert [p for p, _ in second["members"]].count("zoomIn") == 1
    assert len(second["members"]) == 2        # nothing duplicated, nothing lost


def test_a_new_member_never_takes_an_existing_name():
    first = _page(FIRST)
    clash = _plan(("Zoom In", "page.getByTestId('other-zoom')"))     # same name, different element
    second = _page(clash, existing={first["path"]: first["content"]})

    props = [p for p, _ in second["members"]]
    assert len(props) == len(set(props)) == 3
    assert "this.zoomIn = page.getByRole('button', { name: 'Zoom In' })" in second["content"]


def test_with_no_existing_file_the_result_is_unchanged():
    assert [p for p, _ in _page(FIRST)["members"]] == ["zoomIn", "zoomOut"]


def test_instruction_text_in_an_accessible_name_does_not_become_the_member_name():
    assert pom_builder._camel("Cancel button to cancel the operation") == "cancelCancelOperation"
    assert pom_builder._camel("Zoom In, Press Enter to activate") == "zoomIn"


def test_a_name_made_only_of_filler_words_is_not_erased():
    assert pom_builder._camel("Select") == "select"
    assert pom_builder._camel("Button") == "button"


def test_existing_members_are_read_back_from_a_generated_file():
    page = _page(FIRST)
    assert [(p, loc) for p, loc, _c in pom_builder.existing_members(page["content"])] == [
        ("zoomIn", "page.getByRole('button', { name: 'Zoom In' })"),
        ("zoomOut", "page.getByRole('button', { name: 'Zoom Out' })")]
