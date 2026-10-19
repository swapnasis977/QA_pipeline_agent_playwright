"""Control coverage must come from what the scripts DO, not from what their titles SAY.

The failure this prevents: "every control has a test" was answered by matching words in test names,
so a case titled "Verify the Grid toggle ..." counted even if its script never touched the Grid
button. A control is covered when a test's code uses its locator.
"""

from __future__ import annotations

from types import SimpleNamespace

from qa_pipeline import coverage_report

PAGE = """export class ToolPage {
  readonly zoomIn: Locator;
  readonly zoomOut: Locator;
  readonly toggleGrid: Locator;
}
"""
SPEC = """import { test } from '@fixtures/index';

test('PROJ-1-AUTO-TC-001 Zoom in and out', async ({ page }) => {
  const tool = new ToolPage(page);
  await tool.zoomIn.click();
  await tool.zoomOut.click();
});

test('PROJ-1-AUTO-TC-002 Zoom in again', async ({ page }) => {
  const tool = new ToolPage(page);
  await tool.zoomIn.click();
});

test('PROJ-1-AUTO-TC-003 Verify the Grid toggle works', async ({ page }) => {
  await page.goto('/');
});
"""


def _workspace(tmp_path):
    (tmp_path / "src" / "pages").mkdir(parents=True)
    (tmp_path / "tests" / "e2e").mkdir(parents=True)
    (tmp_path / "src" / "pages" / "ToolPage.ts").write_text(PAGE)
    (tmp_path / "tests" / "e2e" / "tool.spec.ts").write_text(SPEC)
    return tmp_path


def test_a_control_named_in_a_title_but_never_used_is_untested(tmp_path):
    result = coverage_report.report(_workspace(tmp_path))
    assert result["total"] == 3
    assert result["untested"] == ["toggleGrid"]       # the title says Grid; the script never clicks it


def test_a_control_used_by_one_test_is_thin_and_by_two_is_not(tmp_path):
    result = coverage_report.report(_workspace(tmp_path))
    assert result["thin"] == ["zoomOut"]
    counts = {r["control"]: r["tests"] for r in result["controls"]}
    assert counts == {"toggleGrid": 0, "zoomOut": 1, "zoomIn": 2}


def test_the_command_lists_what_needs_attention(tmp_path, capsys):
    code = coverage_report.cmd_coverage(SimpleNamespace(), SimpleNamespace(workspace=_workspace(tmp_path)))
    out = capsys.readouterr().out
    assert code == 0 and "Used by NO test:                       1" in out and "toggleGrid" in out


def test_a_workspace_with_no_page_objects_is_explained(tmp_path, capsys):
    assert coverage_report.cmd_coverage(SimpleNamespace(), SimpleNamespace(workspace=tmp_path)) == 1
    assert "No page-object controls" in capsys.readouterr().out
