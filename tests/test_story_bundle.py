"""A person reading one story had to open runs/ for the test cases and workspaces/ for the scripts.

`bundle` must put the story's own specs beside its sheets, leave other stories' specs out, and never
copy the shared framework.
"""

from qa_pipeline.story_bundle import bundle_run, story_keys


def _spec(path, case_id):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"test('{case_id} does a thing', async () => {{}});\n", encoding="utf-8")


def test_only_this_storys_specs_are_copied_and_the_framework_is_not(tmp_path):
    ws = tmp_path / "workspaces" / "app"
    _spec(ws / "tests" / "e2e" / "mine.spec.ts", "PROJ-1-AUTO-TC-001")
    _spec(ws / "tests" / "e2e" / "other.spec.ts", "PROJ-2-AUTO-TC-001")
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "BasePage.ts").write_text("x", encoding="utf-8")
    run = tmp_path / "runs" / "app" / "PROJ-1_FEATURE"
    run.mkdir(parents=True)

    assert bundle_run(run, ws, "app") == 1

    copied = sorted(p.name for p in (run / "2_automation" / "scripts").rglob("*.ts"))
    assert copied == ["mine.spec.ts"]
    assert not (run / "src").exists()
    assert "mine.spec.ts" in (run / "README.md").read_text(encoding="utf-8")


def test_a_story_with_no_scripts_gets_no_empty_folder(tmp_path):
    ws = tmp_path / "ws"
    _spec(ws / "tests" / "e2e" / "a.spec.ts", "PROJ-9-AUTO-TC-001")
    run = tmp_path / "PROJ-1_PROJ-2"
    run.mkdir()
    assert bundle_run(run, ws, "app") is None
    assert not (run / "README.md").exists()


def test_epic_and_story_keys_come_from_the_folder_name():
    assert story_keys("PROJ-68294_PROJ-68309") == ["PROJ-68294", "PROJ-68309"]
    assert story_keys("PROJ-68294_FEATURE") == ["PROJ-68294"]


def test_two_stories_of_one_epic_do_not_take_each_others_scripts(tmp_path):
    import json
    ws = tmp_path / "workspaces" / "app"
    _spec(ws / "tests" / "e2e" / "a.spec.ts", "PROJ-1-AUTO-TC-001")   # same epic prefix in both
    _spec(ws / "tests" / "e2e" / "b.spec.ts", "PROJ-1-AUTO-TC-002")
    _spec(ws / "tests" / "e2e" / "shared.spec.ts", "PROJ-1-AUTO-TC-003")
    runs = tmp_path / "runs" / "app"
    for name, files in (("PROJ-1_PROJ-10", ["a", "shared"]), ("PROJ-1_PROJ-11", ["b", "shared"])):
        gen = runs / name / "2_automation"
        gen.mkdir(parents=True)
        (gen / "generated_files.json").write_text(
            json.dumps([{"path": f"tests/e2e/{f}.spec.ts", "content": ""} for f in files]), encoding="utf-8")

    assert bundle_run(runs / "PROJ-1_PROJ-10", ws, "app") == 2

    got = sorted(p.name for p in (runs / "PROJ-1_PROJ-10" / "2_automation" / "scripts").rglob("*.ts"))
    assert got == ["a.spec.ts", "shared.spec.ts"]
    assert "also written by PROJ-1_PROJ-11" in (runs / "PROJ-1_PROJ-10" / "README.md").read_text(encoding="utf-8")
