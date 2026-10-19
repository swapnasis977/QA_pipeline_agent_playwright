"""Ceilings. Three of them, because one was not enough.

The original pipeline had `--max-run-tokens`, checked after every stage so a
runaway story is caught before the NEXT stage starts. That is the right shape
and it is kept. What it does not bound is BLAST RADIUS: an agent pipeline
that can modify an unlimited number of files in a repository is one nobody
will approve for CI, regardless of what it costs in tokens.

The observed version of this is worth recording, because it is what a missing
file ceiling actually looks like in practice. A run where the Coding Agent
legitimately produced zero files fell through to a full-suite execution and
ran all 39 unrelated spec files accumulated in the repo from previous
features — headed, one worker, with slowMo and retries, against the LIVE app,
for hours, to verify code that run had never produced. The token budget was
never touched, because almost none of it was model work.

So: tokens, files, and crawls.
"""

from __future__ import annotations

from utils.llm_client import summarize_run_cost
from utils.run_layout import artifact_path


class BudgetExceeded(Exception):
    """Raised to halt the run. Caught by the runner, which reports it as a
    deliberate stop rather than a crash — a budget halt is the system working,
    not failing."""


def tokens_spent(ctx) -> int:
    # Measured from the LIVE RUN's start (persisted in the manifest), not from
    # this invocation's, so a resume does not get its whole budget back.
    started = ctx.run_started() if hasattr(ctx, "run_started") else ctx.started
    totals = summarize_run_cost(ctx.feature_dir, since=started).get("_total", {})
    return totals.get("input_tokens", 0) + totals.get("output_tokens", 0)


def over_budget(ctx) -> str:
    """The reason the token cap is already exceeded, or "" - non-raising, for
    a repair loop to stop paying for another attempt mid-stage."""
    cap = token_cap(ctx)
    if not cap:
        return ""
    spent = tokens_spent(ctx)
    return f"token budget exhausted ({spent:,}/{cap:,})" if spent > cap else ""


def token_cap(ctx) -> int:
    """The effective ceiling: a CLI override wins over the profile.

    `None` means "not overridden"; `0` means "explicitly uncapped". Those are
    different answers, which is why this cannot be a plain `or`.
    """
    override = getattr(ctx, "max_run_tokens", None)
    if override is not None:
        return int(override)
    return int(ctx.profile.get("budgets.max_run_tokens", 0) or 0)


def check_tokens(ctx, stage: str = ""):
    cap = token_cap(ctx)
    if not cap:
        return
    spent = tokens_spent(ctx)
    if spent > cap:
        raise BudgetExceeded(
            f"This run has used {spent:,} tokens, over the budget of {cap:,}. "
            f"Stopping before the next stage"
            + (f" ('{stage}')." if stage else ".")
            + f"\n  Raise budgets.max_run_tokens in the profile, or investigate why "
              f"this story is more expensive than expected — see "
              f"{artifact_path(ctx.feature_dir, 'token_log.json')}.")


def check_files(ctx, generated_files: list):
    cap = int(ctx.profile.get("budgets.max_files_per_run", 0) or 0)
    if not cap:
        return
    count = len(generated_files or [])
    if count > cap:
        raise BudgetExceeded(
            f"This run generated {count} files, over the blast-radius ceiling of "
            f"{cap}. Stopping before anything is executed.\n"
            f"  A run this large is usually a sign the story was too broad or a "
            f"case was expanded into many files unintentionally. Raise "
            f"budgets.max_files_per_run if it is genuinely this big.")


def report(ctx) -> str:
    spent = tokens_spent(ctx)
    cap = token_cap(ctx)
    crawl_cap = int(ctx.profile.get("budgets.max_crawls_per_run", 25) or 0)
    return (f"tokens {spent:,}" + (f"/{cap:,}" if cap else "")
            + f"; crawls {ctx.crawl_budget_used}" + (f"/{crawl_cap}" if crawl_cap else ""))
