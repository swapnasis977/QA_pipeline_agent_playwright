"""A spec that loads a data file the workspace does not have must be flagged early.

The failure this prevents: three specs loaded src/data/*.jpg that no one had
committed, so they failed on every machine but the one that generated them, at a
step unrelated to what they test - and nothing said so until the run.
"""

from qa_pipeline import verifiers
from qa_pipeline.verifiers import data_files

SPEC = """
const IMG = path.resolve(__dirname, '../../src/data/test-image.jpg');
const OTHER = `src/data/sub dir/file.png`;
"""


def test_references_are_found_through_relative_prefixes():
    assert data_files.referenced(SPEC) == ["test-image.jpg", "sub dir/file.png"]


def test_a_missing_file_is_reported_with_the_specs_that_need_it(tmp_path):
    gaps = data_files.missing([{"path": "tests/e2e/a.spec.ts", "content": SPEC}], tmp_path)
    assert gaps == {"test-image.jpg": ["tests/e2e/a.spec.ts"],
                    "sub dir/file.png": ["tests/e2e/a.spec.ts"]}


def test_a_present_file_is_not_reported(tmp_path):
    (tmp_path / "src" / "data").mkdir(parents=True)
    (tmp_path / "src" / "data" / "test-image.jpg").write_bytes(b"x")
    gaps = data_files.missing([{"path": "a", "content": "'../../src/data/test-image.jpg'"}], tmp_path)
    assert gaps == {}


def test_it_is_a_warning_not_a_failure(tmp_path):
    # Failing it would block every upload test until someone adds the images.
    result = data_files.warnings_for({"path": "a", "content": SPEC}, tmp_path)
    assert len(result) == 2 and "does not exist" in result[0]
    assert hasattr(verifiers, "data_files")
