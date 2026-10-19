"""
Model Router — the single place that decides which model handles a given
call. Pulled out of llm_client.py so the decision (routing table + per-attempt
downgrade) is explicit and testable on its own, instead of buried inline in
the provider-dispatch code.

Routing rule:
  1. Per-agent env override (MODEL_<AGENT>) always wins — unchanged behavior.
  2. Otherwise MODEL_ROUTING[provider][agent] — the per-agent tier picked for
     the task's reasoning weight (Sonnet-class for the five reasoning agents,
     Haiku-class for Self-Healer's narrow patch-application task).
  3. attempt > 1 (a retry of the SAME call, e.g. Self-Healer's second heal
     attempt on a case its first attempt didn't fix) drops one tier further
     via DOWNGRADE_ON_RETRY, when the provider defines one for that model.
     A second attempt is re-applying a diagnosis that's already been made
     once (the Reviewer's suggested_fix, or the first attempt's own
     reasoning) — that's cheaper work than the first attempt, so it doesn't
     need the first attempt's model tier.
"""

import os

#: `doc_ingestor` routes to the CHEAP tier alongside self_healer, and for the
#: same reason: its task is narrow extraction, not reasoning. It reads a
#: document chunk and emits typed knowledge cards against a fixed shape — no
#: diagnosis, no code, no judgment call about what the app should do. It also
#: runs the most times of any agent on a first setup (once per chunk of every
#: document in the knowledge directory), so tier choice here dominates the
#: cost of standing a new project up.
MODEL_ROUTING = {
    "anthropic": {
        "testcase_writer": "claude-sonnet-4-6",
        "automation_planner": "claude-sonnet-4-6",
        "coding_agent": "claude-sonnet-4-6",
        "reviewer": "claude-sonnet-4-6",
        "self_healer": "claude-haiku-4-5",
        "doc_ingestor": "claude-haiku-4-5",
    },
    "openai": {
        "testcase_writer": "gpt-4o",
        "automation_planner": "gpt-4o",
        "coding_agent": "gpt-4o",
        "reviewer": "gpt-4o",
        "self_healer": "gpt-4o-mini",
        "doc_ingestor": "gpt-4o-mini",
    },
    "azure_openai": {
        "testcase_writer": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        "automation_planner": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        "coding_agent": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        "reviewer": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        "self_healer": os.environ.get("AZURE_OPENAI_DEPLOYMENT_MINI", "gpt-4o-mini"),
        "doc_ingestor": os.environ.get("AZURE_OPENAI_DEPLOYMENT_MINI", "gpt-4o-mini"),
    },
    "openrouter": {
        "testcase_writer": "anthropic/claude-sonnet-4",
        "automation_planner": "anthropic/claude-sonnet-4",
        "coding_agent": "anthropic/claude-sonnet-4",
        "reviewer": "anthropic/claude-sonnet-4",
        "self_healer": "anthropic/claude-haiku-4",
        "doc_ingestor": "anthropic/claude-haiku-4",
    },
    "ollama": {
        "testcase_writer": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "automation_planner": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "coding_agent": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "reviewer": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
        "self_healer": os.environ.get("OLLAMA_MODEL_SELF_HEALER", "qwen2.5-coder:3b"),
        "doc_ingestor": os.environ.get("OLLAMA_MODEL_DOC_INGESTOR", "qwen2.5-coder:3b"),
    },
}

# Only agents that actually retry the SAME call more than once benefit from
# this — today that's just Self-Healer (MAX_HEAL_ATTEMPTS in the heal stage).
# Maps "the model a retry would otherwise use" -> "a cheaper model to use
# instead". DELIBERATELY EMPTY today: Self-Healer already routes to the
# cheapest tier (Haiku-class / gpt-4o-mini) on attempt 1, so there is no
# cheaper model below it to drop to yet. This is the extension point for
# when a smaller model is added to MODEL_ROUTING — e.g.
# {"claude-haiku-4-5": "<a cheaper haiku tier>"} once that tier exists —
# rather than a place to fake a downgrade that doesn't actually save anything.
DOWNGRADE_ON_RETRY = {}

# Self-Healer's attempt 1 already used the cheap/fast model tier and still
# failed to fix the case — a 2nd/3rd attempt is diagnosing a root cause the
# cheap model missed, not re-applying a diagnosis that's already been made
# (that's what DOWNGRADE_ON_RETRY above is for). So a retry attempt ESCALATES
# to a stronger model instead of dropping further, per agent:
ESCALATE_ON_RETRY = {
    "self_healer": {
        "anthropic": "claude-sonnet-4-6",
        "openai": "gpt-4o",
        "azure_openai": os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4o"),
        "openrouter": "anthropic/claude-sonnet-4",
        "ollama": os.environ.get("OLLAMA_MODEL", "qwen2.5-coder:7b"),
    },
}


def resolve_model(agent_name: str, provider: str, attempt: int = 1) -> str:
    """Returns the model id/deployment name for one call.

    `attempt` is 1-indexed: pass the Self-Healer's current retry number
    (1 = first attempt) so a 2nd/3rd attempt can route differently than the
    first. Agents that never retry can omit it (default 1 = no adjustment).

    On attempt > 1: ESCALATE_ON_RETRY (a stronger model, for agents whose
    retry is a fresh diagnosis attempt) is checked first; DOWNGRADE_ON_RETRY
    (a cheaper model, for agents whose retry re-applies an existing
    diagnosis) only applies when the agent has no escalation entry.
    """
    env_override = os.environ.get(f"MODEL_{agent_name.upper()}")
    if env_override:
        return env_override

    # Fallback is THIS PROVIDER's own reasoning-tier model, not a hardcoded
    # "gpt-4o": a new agent name (or a typo) on the anthropic/ollama providers
    # previously resolved to an OpenAI model id, which those APIs reject with
    # an opaque model-not-found error rather than anything pointing here.
    provider_models = MODEL_ROUTING.get(provider, {})
    model = provider_models.get(agent_name) or provider_models.get("reviewer") or "gpt-4o"
    if attempt > 1:
        # Escalation wins when the agent has one; otherwise downgrade. An
        # empty/None escalation entry falls through to the downgrade, same
        # as no entry at all.
        escalated = ESCALATE_ON_RETRY.get(agent_name, {}).get(provider)
        model = escalated or DOWNGRADE_ON_RETRY.get(model, model)
    return model
