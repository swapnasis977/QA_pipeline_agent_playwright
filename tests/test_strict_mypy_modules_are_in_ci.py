"""The modules held to strict typing must be the ones CI actually checks.

The failure this prevents: a module is added to the strict list in pyproject.toml but
not to the CI command (or the reverse), so it is silently never enforced - a "strict"
list that nothing checks is decoration.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_modules() -> set:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[\[tool\.mypy\.overrides\]\]\s*module\s*=\s*\[(.*?)\]", text, re.S).group(1)
    return {m.replace(".", "/") + ".py" for m in re.findall(r'"([^"]+)"', block)}


def _ci_files() -> set:
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    step = re.search(r"Mypy strict modules \(BLOCKING\).*?run: >-\s*(.*?)\n\n", text, re.S).group(1)
    return {tok for tok in step.split() if tok.endswith(".py")}


def test_ci_checks_exactly_the_modules_pyproject_declares_strict():
    declared, checked = _pyproject_modules(), _ci_files()
    assert declared == checked, (
        f"declared strict but not in CI: {sorted(declared - checked)}; "
        f"in CI but not declared: {sorted(checked - declared)}")


def test_every_strict_module_exists():
    missing = [p for p in _pyproject_modules() if not (ROOT / p).is_file()]
    assert not missing, missing
