"""requirements.txt and pyproject.toml must list the same dependencies.

The failure this prevents: the Dockerfile installs from requirements.txt while CI and
developers install from pyproject.toml. Two lists drift - a package added to one only
- and the image then fails at runtime with an ImportError that CI never saw.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _name(spec: str) -> str:
    return re.split(r"[<>=!~;\[ ]", spec.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _pyproject_dependencies() -> set:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)\]", text).group(1)
    return {_name(m) for m in re.findall(r'"([^"]+)"', block)}


def _requirements() -> set:
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return {_name(line) for line in lines if line.strip() and not line.lstrip().startswith("#")}


def test_both_lists_name_the_same_packages():
    only_toml = _pyproject_dependencies() - _requirements()
    only_txt = _requirements() - _pyproject_dependencies()
    assert not only_toml and not only_txt, (
        f"in pyproject.toml only: {sorted(only_toml)}; in requirements.txt only: {sorted(only_txt)}")
