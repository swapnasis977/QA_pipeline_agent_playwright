"""
Shared error classification — distinguishes an infra/billing/quota-shaped
exception (auth failure, rate limit, insufficient credits) from a genuine
content failure (bad JSON, missing fields, etc). An "infra" failure means
retrying the SAME chunk/case later (once the account/quota issue is fixed)
would likely succeed unchanged; a "content" failure means the model's output
itself was the problem.

Used by agents/coding_agent.py and agents/testcase_writer.py when recording
unresolved chunks/rows, so a systemic outage doesn't look identical to a
one-off bad generation in the unresolved log.
"""

import re

#: Case-insensitive substrings observed in practice in infra/billing/quota
#: shaped exception messages (across Anthropic/OpenAI/Azure/OpenRouter SDKs).
#:
#: Every marker here must be specific enough to survive being matched
#: against str(exc), because the exceptions this classifies are frequently
#: ValueErrors whose message QUOTES MODEL OUTPUT — parse_json_response
#: embeds the first 200 chars of the raw response, and coding_agent's chunk
#: handler stores `f"...failed: {exc}"`. A bare "insufficient" was the
#: counter-example: it matched an ordinary content failure whose text said
#: something like "insufficient detail in the test case to generate a
#: locator", labelling a genuine bad-generation as an account/quota outage —
#: the exact distinction this module exists to draw. It was also redundant,
#: since "insufficient_quota" and "quota" below already catch the real
#: billing shape.
#: "api key" was the last remaining marker with the same weakness as
#: "insufficient": generated Playwright code and test-case prose legitimately
#: mention API keys (a test that sets an X-API-Key header, a case step
#: reading "enter a valid api key"), and that text reaches str(exc) through
#: the quoted-model-output paths above. Replaced with the specific wordings
#: the SDKs actually emit for a BAD key, so a content failure that merely
#: talks about API keys is no longer reported as a credentials outage.
#: "usage limit"/"spend limit"/"regain access" cover a workspace SPEND CAP,
#: which none of the markers above matched. Anthropic refuses a capped
#: workspace with an HTTP 400 whose message is "You have reached your
#: specified workspace API usage limits. You will regain access on
#: <date>." — no "quota", no "billing", no "rate limit" anywhere in it, and
#: the class is BadRequestError, so it missed _INFRA_CLASS_MARKERS too. It
#: was therefore classified "content", which is the worst possible reading:
#: the stage reported a modelling failure, wrote no infra-failures file,
#: printed no 🚨, and exited 0 — so a hard billing wall looked like the
#: model simply declining to generate. Observed on a real run where all 18
#: chunks failed this way and the pipeline reported success.
#:
#: These stay specific for the reason the note above gives: a bare "limit"
#: would match generated prose ("verify the character limit"), but a test
#: case has no reason to say "usage limit" or "regain access".
_INFRA_MARKERS = (
    "credit balance", "insufficient_quota", "billing",
    "quota", "rate limit", "ratelimit", "authenticationerror",
    "permissiondeniederror", "unauthorized",
    "invalid api key", "incorrect api key", "no api key",
    "api key is not set", "api key not set", "missing api key",
    "usage limit", "spend limit", "regain access",
)

#: Exception class names that are themselves a reliable infra signal
#: regardless of message content.
_INFRA_CLASS_MARKERS = (
    "authenticationerror", "permissiondeniederror", "ratelimiterror",
)


def _normalize(text: str) -> str:
    """Lowercases and collapses `_`/`-` to spaces so one spelling of a marker
    matches every separator these messages actually use.

    Without this, the markers were separator-sensitive and missed the most
    common case in this very codebase: llm_client._require_env raises
    "ANTHROPIC_API_KEY is not set — add it to your .env", which the old
    "api key" marker never matched (underscores, not spaces), so a missing
    provider key — the most unambiguous infra failure there is — was logged
    against every affected case as a content failure.
    """
    return re.sub(r"[_\-]+", " ", (text or "").lower())


def classify_error_kind(exc: Exception) -> str:
    """Returns "infra" for an auth/billing/quota/rate-limit-shaped exception,
    "content" otherwise."""
    class_name = type(exc).__name__.lower()
    if any(marker in class_name for marker in _INFRA_CLASS_MARKERS):
        return "infra"
    text = _normalize(f"{class_name} {exc}")
    if any(marker in text for marker in _INFRA_MARKERS):
        return "infra"
    return "content"
