"""
Transient-fault classification and retry — ONE definition, used everywhere.

WHY THIS MODULE EXISTS. Retry lived only at STAGE granularity
(`qa_pipeline/orchestrator/runner.run_stage`), and that is the wrong altitude
for a fault that belongs to a single HTTP request. Two concrete consequences,
both observed in the shape of the code:

  1. The Coding Agent catches every chunk exception and records those cases as
     `unresolved` (see its ThreadPoolExecutor completion loop). A 429 or a
     dropped connection on chunk 7 of 10 therefore NEVER reached run_stage's
     transient retry — it was swallowed, the affected cases silently lost
     their code, and the stage returned successfully. A run that reports
     "generated 18 files, 6 unresolved" because of a two-second rate limit is
     the worst kind of failure: it looks like a result.

  2. The Automation Planner does NOT catch per batch, so the opposite is true
     there: a blip on batch 5 of 6 propagates, run_stage retries THE WHOLE
     STAGE, and batches 1-4 are re-planned and re-billed for nothing.

Retrying at the call site fixes both, because a transient fault is a property
of one request, not of the workflow wrapped around it. Stage-level retry stays
exactly as it is — it is the correct backstop for a fault that outlives the
per-call budget (a real outage, an expired key) — but it should now rarely be
reached.

POLICY. Exponential backoff with FULL JITTER (AWS's "Exponential Backoff and
Jitter"): sleep is drawn uniformly from [0, base * 2**attempt] rather than
being exactly that value. The un-jittered version is actively harmful here —
the Coding Agent fires up to `_MAX_PARALLEL_CHUNKS` concurrent calls on ONE
API key, so a rate limit hits all of them at once, and a fixed delay marches
every retry back into the provider in the same instant that earned the 429.
Jitter is what spreads them out.

`Retry-After`, when the provider sends one, always wins over the computed
delay — it is the server stating the actual reset window, and backing off for
less than it asks is how a soft rate limit becomes a hard one.
"""

import os
import random
import time

#: Failure modes that go away on their own with a short wait, matched against
#: the exception's CLASS NAME (not its message).
#:
#: Class name only, deliberately: `utils/error_classify` documents at length
#: how message-substring matching misfires in this pipeline, because agent
#: exceptions routinely QUOTE MODEL OUTPUT (parse_json_response embeds the
#: first 200 chars of the raw response). A generated test whose prose mentions
#: a timeout must not be retried as if the API had timed out.
TRANSIENT_MARKERS = (
    "RateLimitError", "APITimeoutError", "APIConnectionError",
    "InternalServerError", "ServiceUnavailableError", "Timeout",
    "ConnectionError", "ConnectionResetError", "OverloadedError",
    # Anthropic/OpenAI SDKs surface a 5xx that is not one of the named
    # classes above as APIStatusError; 529 (Overloaded) arrives this way.
    "APIStatusError",
)

#: Retries per LLM call, and the backoff base. Tunable for the same reason
#: every other operational knob in this repo is (EXECUTOR_TIMEOUT_SECONDS,
#: ALLURE_TIMEOUT_SECONDS): a shared corporate API key with a low per-minute
#: ceiling needs a longer, flatter backoff than a dedicated one.
_DEFAULT_MAX_RETRIES = 4
_DEFAULT_BASE_DELAY_SEC = 2.0

#: Ceiling on any single sleep. Without it, attempt 4 at base 2 already asks
#: for 16s and a raised base runs to minutes — long enough that an operator
#: reasonably concludes the pipeline has hung and kills it, which costs more
#: than the failure would have.
_MAX_DELAY_SEC = 60.0


def _env_float(name: str, default: float) -> float:
    """os.environ.get + float(), but a malformed value falls back to the
    default instead of raising. A typo'd .env entry must not be able to take
    down every LLM call in the pipeline from inside the retry helper."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def is_transient(exc: Exception) -> bool:
    """Whether `exc` is worth retrying unchanged after a wait."""
    name = type(exc).__name__
    return any(marker in name for marker in TRANSIENT_MARKERS)


def _retry_after_seconds(exc: Exception) -> float:
    """The provider's own Retry-After, in seconds, or 0 if it didn't send one.

    Best-effort by design: the SDKs expose the response differently and a
    missing/garbage header is the norm, not the exception. Anything we cannot
    read confidently returns 0 and lets the computed backoff stand.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not headers:
        return 0.0
    try:
        raw = headers.get("retry-after") or headers.get("Retry-After")
        # Only the delay-seconds form is honored. The HTTP-date form is legal
        # but effectively unused by these providers, and mis-parsing a date
        # into a huge sleep is far worse than ignoring it.
        return max(0.0, min(float(raw), _MAX_DELAY_SEC))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def call_with_retry(fn, *, label: str, max_retries: int = None,
                    base_delay: float = None, sleep=time.sleep):
    """Calls `fn()`, retrying TRANSIENT failures with jittered exponential
    backoff. Returns whatever `fn` returns.

    A non-transient exception is re-raised IMMEDIATELY and untouched — a
    malformed response, a bad prompt or a missing API key does not improve
    with a wait, and retrying it just multiplies the bill. That distinction is
    the whole point; a retry helper that catches `Exception` would convert
    every real bug into a slow real bug.

    The final attempt's exception propagates unchanged, so the stage-level
    policy in `orchestrator.runner` still sees the original error type and can
    make its own (now genuinely last-resort) decision about it.

    `sleep` is injected so tests can assert the backoff schedule without
    actually waiting.
    """
    if max_retries is None:
        max_retries = int(_env_float("LLM_MAX_RETRIES", _DEFAULT_MAX_RETRIES))
    if base_delay is None:
        base_delay = _env_float("LLM_RETRY_BASE_DELAY_SEC", _DEFAULT_BASE_DELAY_SEC)

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised unless transient
            if not is_transient(exc) or attempt >= max_retries:
                raise
            # Full jitter: uniform over [0, capped_window], NOT the window
            # itself. See the module docstring — concurrent chunk calls share
            # one key, so an unjittered delay re-collides them by construction.
            window = min(base_delay * (2 ** attempt), _MAX_DELAY_SEC)
            delay = _retry_after_seconds(exc) or random.uniform(0, window)
            print(f"      ⏳ {label}: {type(exc).__name__} — transient, retrying "
                  f"{attempt + 1}/{max_retries} in {delay:.1f}s.")
            sleep(delay)
