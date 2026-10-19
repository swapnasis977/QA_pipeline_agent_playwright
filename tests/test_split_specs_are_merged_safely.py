"""A feature split across `_tc003-019`-style files is merged - but only when that is safe.

The failure this prevents (both directions):
  - Leaving it split: "the adjustments panel tests" were six files named after case
    numbers, each re-inventing the same helpers.
  - Merging carelessly: file-level `test.beforeEach` or a same-named helper with a
    different body would silently change what some tests do. Those cases must keep the
    files split and say why.
"""

from agents import spec_merge

HEAD = "import { test, expect } from '@fixtures/index';\n"


def _part(path, body, imports=HEAD):
    return {"path": path, "kind": "spec", "content": imports + "\n" + body,
            "test_case_ids": [path]}


T1 = "test('A-1 first', async () => {\n  await expect(1).toBe(1);\n});\n"
T2 = "test('A-2 second', async () => {\n  await expect(2).toBe(2);\n});\n"
BASE = "tests/e2e/zoom.spec.ts"
P1 = "tests/e2e/zoom_tc001-002.spec.ts"
P2 = "tests/e2e/zoom_tc003.spec.ts"


def test_part_names_are_recognised_and_stripped():
    assert spec_merge.base_path(P1) == BASE
    assert spec_merge.is_split_part(P1) and not spec_merge.is_split_part(BASE)


def test_a_group_needs_at_least_one_real_split_part():
    assert spec_merge.plan([_part(BASE, T1)]) == []
    assert [b for b, _ in spec_merge.plan([_part(P1, T1), _part(P2, T2)])] == [BASE]


def test_two_parts_become_one_with_every_test_kept():
    merged, why = spec_merge.merge([_part(P2, T2), _part(P1, T1)])
    assert why is None
    assert merged.count("import { test, expect }") == 1
    assert merged.index("A-1 first") < merged.index("A-2 second")   # by case number, not input order


def test_named_imports_from_one_module_are_combined():
    a = _part(P1, T1, "import { test } from '@fixtures/index';\n")
    b = _part(P2, T2, "import { expect } from '@fixtures/index';\n")
    merged, _ = spec_merge.merge([a, b])
    assert "import { test, expect } from '@fixtures/index';" in merged


def test_the_same_name_imported_two_ways_is_bound_once():
    # `import path from 'path'` + `import * as path from 'path'` is a duplicate identifier.
    a = _part(P1, T1, HEAD + "import path from 'path';\n")
    b = _part(P2, T2, HEAD + "import * as path from 'path';\n")
    merged, _ = spec_merge.merge([a, b])
    assert merged.count("path from 'path'") == 1


def test_an_identical_helper_in_both_parts_is_kept_once():
    helper = "async function openEditor(page) {\n  await page.goto('/');\n}\n"
    merged, why = spec_merge.merge([_part(P1, helper + "\n" + T1), _part(P2, helper + "\n" + T2)])
    assert why is None and merged.count("function openEditor") == 1


def test_a_helper_declared_differently_keeps_the_files_split():
    h1 = "async function openEditor(page) {\n  await page.goto('/a');\n}\n"
    h2 = "async function openEditor(page) {\n  await page.goto('/b');\n}\n"
    merged, why = spec_merge.merge([_part(P1, h1 + "\n" + T1), _part(P2, h2 + "\n" + T2)])
    assert merged is None and "openEditor" in why


def test_file_level_hooks_in_only_one_part_keep_the_files_split():
    # At file level a beforeEach applies to EVERY test; merging would change part 2's tests.
    hook = "test.beforeEach(async ({ page }) => {\n  await page.goto('/');\n});\n"
    merged, why = spec_merge.merge([_part(P1, hook + "\n" + T1), _part(P2, T2)])
    assert merged is None and "hooks" in why


def test_the_same_hook_in_every_part_is_fine():
    hook = "test.beforeEach(async ({ page }) => {\n  await page.goto('/');\n});\n"
    merged, why = spec_merge.merge([_part(P1, hook + "\n" + T1), _part(P2, hook + "\n" + T2)])
    assert why is None and merged.count("test.beforeEach") == 1


def test_two_tests_with_one_title_keep_the_files_split():
    other = "test('A-1 first', async () => {\n  await expect(9).toBe(9);\n});\n"
    merged, why = spec_merge.merge([_part(P1, T1), _part(P2, other)])
    assert merged is None and "share the title" in why


def test_apply_writes_the_merged_file_and_removes_the_parts(tmp_path):
    for path, body in ((P1, T1), (P2, T2)):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(HEAD + "\n" + body, encoding="utf-8")
    files = [{**_part(P1, T1), "test_case_ids": ["A-1"]}, {**_part(P2, T2), "test_case_ids": ["A-2"]}]

    new_files, report = spec_merge.apply(files, tmp_path, check=lambda t: (True, ""))

    assert [f["path"] for f in new_files] == [BASE]
    assert new_files[0]["test_case_ids"] == ["A-1", "A-2"]       # the heal stage joins on these
    assert (tmp_path / BASE).is_file()
    assert not (tmp_path / P1).exists() and not (tmp_path / P2).exists()
    assert report["merged"][0]["from"] == [P1, P2]


def test_a_merge_that_does_not_compile_is_undone_completely(tmp_path):
    for path, body in ((P1, T1), (P2, T2)):
        (tmp_path / path).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / path).write_text(HEAD + "\n" + body, encoding="utf-8")
    files = [_part(P1, T1), _part(P2, T2)]

    new_files, report = spec_merge.apply(files, tmp_path, check=lambda t: (False, "TS2300"))

    assert [f["path"] for f in new_files] == [P1, P2]            # unchanged
    assert (tmp_path / P1).is_file() and (tmp_path / P2).is_file()
    assert not (tmp_path / BASE).exists()
    assert "did not compile" in report["kept_split"][0]["reason"]
