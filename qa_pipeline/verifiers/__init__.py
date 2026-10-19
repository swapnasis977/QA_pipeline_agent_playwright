"""The gates. Five of them, and not one makes a model call.

That is the property that matters. A verifier cannot hallucinate, never needs
a retry budget, costs nothing to run, and returns the same answer twice for
the same input. Every guarantee this pipeline offers rests on the gates being
deterministic — which is why "add another agent to check the first agent" is
the one extension that would undermine the design rather than strengthen it.

    schema_check     every artifact, on write and on read
    locator_check    generated code and plans, against the ledger
    compile_check    generated code, against the real toolchain
    scaffold_check   plans and code, against the framework layout
    coverage_check   the test case sheet, against the story

`repair_loop` is the control structure that turns a failing gate into another
attempt with a precise instruction, rather than into a decline.
"""

from . import (  # noqa: F401
    compile_check,
    coverage_check,
    data_files,
    locator_check,
    repair_loop,
    scaffold_check,
    schema_check,
    spec_hygiene,
)
from .result import FAIL, PASS, SKIPPED, VerifyResult, combine  # noqa: F401

__all__ = [
    "VerifyResult", "combine", "PASS", "FAIL", "SKIPPED",
    "schema_check", "locator_check", "compile_check",
    "scaffold_check", "coverage_check", "repair_loop",
]


def gate_code(generated_file: dict, ledger, repo_root, *, screen: str = "",
              screens: list = None, test_kind: str = "",
              snapshot_elements: list = None,
              allow_crawl: bool = True, plans: list = None) -> VerifyResult:
    """Every gate that applies to one generated file, in cost order.

    Ordered cheapest-first and short-circuiting: contract, then placement,
    then compile, then locators. There is no point paying for a crawl to
    ground the locators in a file that does not compile, and no point
    compiling a file whose path means the Executor will never collect it.
    """
    checks = [
        schema_check.check("generated_file", generated_file),
        scaffold_check.check_file(generated_file, test_kind=test_kind),
    ]
    if not all(c.ok for c in checks):
        return combine("gate_code", checks)

    checks.append(compile_check.check_file(generated_file, repo_root))
    if not checks[-1].ok:
        return combine("gate_code", checks)

    checks.append(locator_check.check_file(
        generated_file, ledger, screen=screen, screens=screens,
        snapshot_elements=snapshot_elements, allow_crawl=allow_crawl))

    # Did it use the locators the PLANNER grounded? Last because it is the
    # most specific, and free — no ledger lookup, no crawl. This gate was
    # missing entirely: the plan was never passed in, so nothing checked the
    # one thing the whole grounding chain exists to produce. See
    # locator_check.check_against_plan.
    if plans:
        checks.append(locator_check.check_against_plan(generated_file, plans))

    # Logic-level defects the locator gates cannot see: a dropped
    # `{ exact: true }`, two names bound to one locator, branching in a test
    # body. A spec can pass every locator check and still be unable to
    # succeed. See spec_hygiene.
    checks.append(spec_hygiene.check(generated_file, plans))
    # Test data the spec loads must exist (a WARNING: see data_files).
    data_warnings = data_files.warnings_for(generated_file, repo_root)
    if data_warnings:
        checks.append(VerifyResult.passed("data_files", warnings=data_warnings))
    return combine("gate_code", checks)


def gate_plan(plan: dict, ledger, *, allow_crawl: bool = True) -> VerifyResult:
    """Contract, placement, then element resolution — before a line is written.

    This is the cheapest gate in the pipeline and the one that saves the most:
    an element resolved here costs a crawl, while the same element discovered
    after generation costs a generation, a compile, an execution and a heal.
    """
    checks = [
        schema_check.check("automation_plan", plan),
        scaffold_check.check_plan(plan),
    ]
    if not all(c.ok for c in checks):
        return combine("gate_plan", checks)

    checks.append(locator_check.check_plan(plan, ledger, allow_crawl=allow_crawl))
    return combine("gate_plan", checks)


def gate_test_cases(test_cases: list, acceptance_criteria=None, *,
                    strict_coverage: bool = False) -> VerifyResult:
    """Contract, decline protocol, then coverage against the story."""
    return combine("gate_test_cases", [
        schema_check.check("test_case", test_cases, many=True),
        schema_check.check_declines(test_cases, label="test_case"),
        coverage_check.check(test_cases, acceptance_criteria, strict=strict_coverage),
    ])
