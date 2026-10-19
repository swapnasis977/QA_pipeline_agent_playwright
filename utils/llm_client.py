"""
Thin wrapper around AI provider APIs used by every agent.

SUPPORTS MULTIPLE PROVIDERS — set AI_PROVIDER in .env to switch. Every agent
in every skill/prompt is provider-agnostic; only this file knows the wire
format for each vendor.

Providers implemented:
  - "anthropic"      (default) — Claude models directly via api.anthropic.com
  - "openai"         — GPT models via api.openai.com
  - "azure_openai"   — GPT models via your Azure OpenAI deployment
  - "github_models"  — RETIRED 2026-07-30, no longer usable. Left in place only
                        so old .env files produce a clear error instead of a
                        confusing one.
  - "openrouter"     — OpenRouter (openrouter.ai), single API key, OpenAI-
                        compatible API, no cloud account needed — routes to
                        many underlying models (Claude, GPT, Llama, etc.)
                        through one endpoint. Good fallback when you don't
                        have direct Anthropic/OpenAI/Azure credentials yet.
  - "ollama"         — Local model via a running Ollama server (default
                        http://localhost:11434), OpenAI-compatible API, NO
                        API key/billing/network dependency at all. Quality
                        is noticeably lower than Claude/GPT-4o-class models
                        for this pipeline's structured-JSON-heavy prompts,
                        especially on small (<8B param) models — treat this
                        as a way to exercise the pipeline end-to-end locally,
                        not a like-for-like substitute.

IMPORTANT — GitHub Copilot clarification:
GitHub Copilot's in-editor model picker (where you choose "Claude Sonnet" in
VS Code Copilot Chat) is NOT an API you can call from external scripts like
this pipeline. Copilot Chat and this pipeline are separate execution contexts
— there is no key you can extract from Copilot to plug in here. If you want
this pipeline to use Claude, use the "anthropic" provider directly with your
own Anthropic API key (this is what MODEL_ROUTING defaults to). "github_models"
below is a genuinely separate, real product (GitHub's own hosted model
catalog, callable via GITHUB_TOKEN) — it is not the same thing as Copilot's
chat model picker, even though both are made by GitHub.

Three token-saving mechanisms on top of the base design:

1. SKILL CACHING (Anthropic only — see note in _call_anthropic) — each
   agent's standing instructions live in skills/<agent>.skill.md and are sent
   with cache_control="ephemeral", so repeat calls within Anthropic's ~5 min
   cache window are billed at cache-read rates for that block.
   CAVEAT: Anthropic only caches a prefix that clears a per-model minimum
   (~1024 tokens for Sonnet-class, ~4096 for Haiku-class). Most skill files
   here are well under that on their own — see mechanism 3 below, which
   exists specifically to give that prefix enough shared, stable content to
   clear the floor. Only agents that call more than once per run
   (self_healer's per-case loop, testcase_writer's per-story loop) can
   benefit at all from the cache actually being hit.

2. CONDENSED CONTEXT FILE — context/<feature>/summary.md, a short running
   summary agents read/extend instead of re-sending full raw payloads.

3. SHARED INSTRUCTIONS PREFIX — instructions/conventions.md, house-style
   content common to every agent (naming, tone, what "done" means for a
   test case), loaded once and placed as its OWN cached system block ahead
   of the agent-specific skill file. Two effects: (a) shared conventions are
   edited in ONE file instead of copy-pasted into five skill files, and
   (b) skill_text + conventions together are far more likely to clear the
   per-model cache-eligibility floor than skill_text alone, so mechanism 1
   actually engages instead of being a no-op for agents that call once.
"""

import base64
import json
import mimetypes
import os
import re
import threading
import time
from pathlib import Path

# The same hardened I/O every agent artifact goes through — explicit UTF-8 in
# both directions, atomic replace, corrupt file treated as absent. This module
# writes token_log.json and summary.md, which are artifacts exactly like the
# ones in agents/, and were the last two still going through the platform
# locale codepage. See artifact_io's own module docstring.
from agents.artifact_io import read_json, read_text_file, write_json, write_text_file

# Model selection lives in utils/model_router.py — kept as its own module
# (routing table + per-attempt downgrade rule) so it's a decision you can read
# and test on its own, not inline in the provider-dispatch code below.
from utils.model_router import resolve_model as _router_resolve_model
from utils.run_layout import artifact_path
from utils.timestamps import utc_iso, utc_stamp

# Per-CALL transient retry. Retry used to exist only at stage granularity, which
# is the wrong altitude for a per-request fault — see utils/transient.py's
# docstring for the two concrete failures that caused.
from utils.transient import call_with_retry

SKILLS_DIR = Path(__file__).resolve().parent.parent / "skills"
INSTRUCTIONS_DIR = Path(__file__).resolve().parent.parent / "instructions"

AI_PROVIDER = os.environ.get("AI_PROVIDER", "anthropic").lower()

#: Plain-language description of what each agent's LLM call is actually
#: doing, printed alongside (not instead of) the technical detail line —
#: so someone watching the console who isn't familiar with this pipeline's
#: internals still knows what's happening at each step.
AGENT_FRIENDLY_ACTION = {
    "testcase_writer": "reading through the test cases and writing them up in our standard format",
    "automation_planner": "figuring out how to turn each reviewed test case into automated browser steps",
    "coding_agent": "writing the actual Playwright test code for these cases",
    "reviewer": "reviewing the test run results to tell real bugs apart from flaky failures",
    "self_healer": "trying to auto-fix a test that broke, without changing what it's meant to check",
    "doc_ingestor": "reading your design docs and turning them into facts the other agents can look up",
}

_SKILL_CACHE: dict = {}
_INSTRUCTIONS_CACHE: dict = {}


def _load_skill(agent_name: str) -> str:
    """Loads skills/<agent_name>.skill.md. A missing skill file is a HARD
    failure (raises FileNotFoundError below) by design — an agent must never
    run without its own standing instructions, unlike the shared conventions
    file (see _load_shared_instructions), which degrades silently since it's
    optional context, not per-agent-critical. This asymmetry is intentional,
    not an oversight."""
    if agent_name in _SKILL_CACHE:
        return _SKILL_CACHE[agent_name]
    skill_path = SKILLS_DIR / f"{agent_name}.skill.md"
    if not skill_path.exists():
        raise FileNotFoundError(
            f"No skill file for '{agent_name}' at {skill_path}. "
            f"Every agent needs skills/<agent_name>.skill.md."
        )
    # Explicit UTF-8: the skill files are prose with em dashes and quoted
    # example output, and this content is sent verbatim as the system prompt.
    content = skill_path.read_text(encoding="utf-8")
    _SKILL_CACHE[agent_name] = content
    return content


def _load_shared_instructions() -> str:
    """Loads instructions/conventions.md — house style shared by every
    agent (naming, tone, what "done" means). Returns "" if the file doesn't
    exist so this stays optional rather than a hard dependency; agents work
    identically without it, they just lose the shared cache-prefix benefit
    described in the module docstring's mechanism 3. This is the deliberate
    opposite of _load_skill's behavior: a missing skill file is a hard
    FileNotFoundError since an agent can't run without its own instructions,
    while conventions.md is shared/optional context that's fine to lose."""
    if "conventions" in _INSTRUCTIONS_CACHE:
        return _INSTRUCTIONS_CACHE["conventions"]
    path = INSTRUCTIONS_DIR / "conventions.md"
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    _INSTRUCTIONS_CACHE["conventions"] = content
    return content


def _resolve_model(agent_name: str, attempt: int = 1) -> str:
    """Per-agent env override wins; otherwise the Model Router's decision
    for the active provider, adjusted for retry attempt (see
    utils/model_router.resolve_model)."""
    return _router_resolve_model(agent_name, AI_PROVIDER, attempt=attempt)


#: Usage from the most recent call_agent() call ON THE CALLING THREAD.
#:
#: Thread-local, not a plain module global: testcase_writer (per story) and
#: coding_agent (per generation chunk) call call_agent() concurrently, and
#: both read this straight afterward to decide whether the response was cut
#: off at max_tokens. With one shared dict, a sibling thread finishing its
#: own call in between overwrote the value, so a chunk could see another
#: chunk's truncation flag — silently splitting a batch that was fine, or
#: (worse) missing the split on the batch that actually truncated.
#:
#: Exposed as `last_usage` still, via a module __getattr__ below, so the
#: existing `llm_client.last_usage.get("truncated")` call sites and
#: parse_json_response's own read are unchanged.
_thread_state = threading.local()


def _get_last_usage() -> dict:
    return getattr(_thread_state, "last_usage", None) or {}


def __getattr__(name):
    # Module-level __getattr__ (PEP 562) — only consulted for names not
    # found normally, so this costs nothing for every other attribute.
    if name == "last_usage":
        return _get_last_usage()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _parse_with_repair(text: str, max_repairs: int = 25):
    """json.loads with a bounded auto-repair loop for two observed failure
    modes in generated code content:

    1. Literal unescaped control characters (raw newlines/tabs) INSIDE a
       JSON string value — e.g. a "content" field containing an actual
       newline byte instead of the escaped "\\n" sequence. Strict JSON
       rejects this as "Invalid control character"; strict=False is the
       documented, correct way to accept it (this is not a repair hack,
       json.loads has a real flag for exactly this case).

    2. A mis-encoded punctuation artifact (e.g. an em-dash rendered as
       garbled bytes ending in a literal `"`) INSIDE a JSON string value —
       e.g. "...TC-007 â€\" Flip Horizontal...". That literal `"` prematurely
       closes the JSON string, which json.loads then reports as "Expecting
       ',' delimiter" — but NOT at the bad quote itself; `exc.pos` is
       wherever the parser resumed looking (the start of the next token,
       e.g. "Flip"), a couple characters AFTER the actual offending quote.
       Search backward from exc.pos for the nearest unescaped `"` and
       repair that one; bounded so a genuinely malformed document still
       fails fast instead of looping forever.
    """
    attempt = text
    for _ in range(max_repairs):
        try:
            return json.loads(attempt, strict=False)
        except json.JSONDecodeError as exc:
            if exc.pos is None:
                raise
            q = attempt.rfind('"', 0, exc.pos)
            if q == -1 or attempt[q - 1:q] == "\\":
                raise
            attempt = attempt[:q] + '\\"' + attempt[q + 1:]
    return json.loads(attempt, strict=False)  # let the final attempt's real error surface


def parse_json_response(raw: str, feature_dir: Path, agent_name: str):
    """Parses a model's JSON response, tolerating the ways models wrap it.

    Replaces the `raw.strip("```json").strip("```")` idiom that was repeated
    in all five agents. That idiom was subtly wrong: str.strip takes a SET OF
    CHARACTERS, not a prefix — so `strip("```json")` strips any leading/
    trailing run of {`,j,s,o,n}, which mangles content while leaving real
    prose preambles ("Here are the test cases:") in place.

    On failure the raw response is written to disk and the error names the
    file, so a bad generation is diagnosable instead of vanishing into a
    chained JSONDecodeError.
    """
    text = (raw or "").strip()

    # Try every fenced block, LAST-to-FIRST. A verbose/chain-of-thought
    # response can contain more than one fence — e.g. an illustrative
    # "sketch" of the intended shape (sometimes with literal "..." elisions)
    # before the real, complete answer. The real answer is reliably the LAST
    # fence in that case, so trying fences newest-first and taking the first
    # one that actually parses avoids latching onto an earlier placeholder.
    fences = re.findall(r"```[A-Za-z]*\s*\n(.*?)```", text, re.S)
    for candidate in reversed(fences):
        try:
            return _parse_with_repair(candidate.strip())
        except json.JSONDecodeError:
            continue

    # No fence parsed (or none present) — look for a COMPLETE JSON value
    # embedded anywhere in the text, by asking the decoder to parse from each
    # possible opening delimiter and keeping what actually decodes.
    #
    # This runs before the coarse first-open/last-close slice below because
    # that slice is fooled by JSON-looking punctuation in a prose preamble.
    # A Self-Healer response that explained itself first — "these follow the
    # pattern `[action] button. Click to [description]`" — and THEN emitted a
    # perfectly good patch object was thrown away, because the first `[` in
    # the text is the one inside that sentence and the slice ran from there
    # to the last `]`, which is neither valid JSON nor the answer. The model
    # had done the work; only the extraction failed.
    #
    # Last match wins, for the same reason the fence loop above runs in
    # reverse: when a response contains more than one JSON value, the real
    # answer is the one it finished on.
    # Scanning left to right and JUMPING PAST each value that decodes is what
    # keeps this on top-level values: every nested object also decodes
    # perfectly well from its own opening brace, so a plain "keep the last
    # one that parses" returns the innermost trailing object — for the
    # response described above, the small "locator_memory_update" sub-object
    # instead of the patch that contains it.
    decoder = json.JSONDecoder(strict=False)
    embedded = None
    embedded_start = -1
    i, n = 0, len(text)
    while i < n:
        if text[i] not in "[{":
            i += 1
            continue
        try:
            value, end = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(value, (dict, list)):
            embedded = value
            embedded_start = i
        i = max(end, i + 1)
    if embedded is not None:
        # THE OUTER VALUE FAILED TO DECODE, A FRAGMENT INSIDE IT DID. A response
        # that is almost valid (a mis-encoded quote) or cut off makes the scan
        # step into its string contents, where generated TypeScript is full of
        # decodable `[]` / `{}` ("= []", "test.use({})"). Returning that would
        # yield an empty result with no error and no retry. So when the first
        # opening delimiter is NOT where the accepted value starts, give the
        # repairing parser the outer slice first; the fragment is the fallback.
        first = min((p for p in (text.find("["), text.find("{")) if p != -1), default=-1)
        if first != -1 and first != embedded_start:
            j = max(text.rfind("]"), text.rfind("}"))
            if j > first:
                try:
                    return _parse_with_repair(text[first:j + 1])
                except json.JSONDecodeError:
                    pass
            if first == 0:
                # The response OPENS with JSON that does not decode: it is a
                # broken/cut-off answer, not prose around a good one. A fragment
                # of it is not the answer; fall through so the failure is raised.
                embedded = None
    if embedded is not None:
        return embedded

    # Coarse fallback: the outermost JSON delimiters in the whole text —
    # whichever of [ or { appears FIRST, so an object response like
    # {"files": [...]} isn't sliced to its inner array. Still worth trying,
    # because `_parse_with_repair` can rescue a value that `raw_decode`
    # rejects outright (an unescaped quote inside a string, say).
    opens = [(text.find(c), c) for c in "[{" if text.find(c) != -1]
    if opens:
        i, opener = min(opens)
        j = text.rfind("]" if opener == "[" else "}")
        if j > i:
            text = text[i:j + 1]

    try:
        return _parse_with_repair(text)
    except json.JSONDecodeError as exc:
        dump = artifact_path(feature_dir, f"{agent_name}_raw_response.txt")
        dump.parent.mkdir(parents=True, exist_ok=True)
        dump.write_text(raw or "", encoding="utf-8")
        hint = ""
        if _get_last_usage().get("truncated"):
            hint = (" — the model hit max_tokens and was cut off mid-JSON; "
                    "raise max_tokens for this agent or split the input batch")
        elif not text.rstrip().endswith(("]", "}")):
            hint = " — response looks truncated (does not end with ] or })"
        raise ValueError(
            f"{agent_name}: could not parse model JSON{hint}. "
            f"Raw response saved to {dump}. First 200 chars: {(raw or '')[:200]!r}"
        ) from exc


#: Hard cap on images sent in one call — a design doc with dozens of
#: extracted screenshots would otherwise balloon both cost and the chance of
#: hitting a provider's own per-request image-count limit. Chosen to comfortably
#: cover "a handful of screens relevant to one story," not "attach the whole design file."
_MAX_IMAGES_PER_CALL = 6

#: Anthropic/OpenAI both reject an image payload above roughly this size
#: (~5MB is the commonly documented ceiling) — skipped rather than sent and
#: rejected, since a rejected image fails the WHOLE call, not just that image.
_MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _prepare_images(image_paths: list) -> list:
    """Reads and base64-encodes up to _MAX_IMAGES_PER_CALL images for a
    multimodal call. Skips (with a warning, not a crash) anything unreadable,
    oversized, or not a recognized image type — a bad screenshot path
    shouldn't take down the whole text-case-generation call. Returns
    [{"media_type": "image/png", "data": "<base64>"}], provider-agnostic;
    _call_anthropic/_call_openai_compatible each wrap this into their own
    wire format."""
    prepared = []
    for raw_path in (image_paths or [])[:_MAX_IMAGES_PER_CALL]:
        path = Path(raw_path)
        media_type, _ = mimetypes.guess_type(str(path))
        if not media_type or not media_type.startswith("image/"):
            print(f"⚠️  Skipping '{path}' as a vision input — not a recognized image type.")
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            print(f"⚠️  Skipping '{path}' as a vision input — could not read it ({exc}).")
            continue
        if len(data) > _MAX_IMAGE_BYTES:
            print(f"⚠️  Skipping '{path}' as a vision input — {len(data)} bytes exceeds the "
                  f"{_MAX_IMAGE_BYTES}-byte cap most providers enforce per image.")
            continue
        prepared.append({"media_type": media_type, "data": base64.b64encode(data).decode("ascii")})
    if len(image_paths or []) > _MAX_IMAGES_PER_CALL:
        print(f"      Only the first {_MAX_IMAGES_PER_CALL} of {len(image_paths)} image(s) "
              f"were sent — see utils/llm_client._MAX_IMAGES_PER_CALL.")
    return prepared


def call_agent(agent_name: str, user_prompt: str, feature_dir: Path,
                max_tokens: int = 4000, extra_system: str = "", attempt: int = 1,
                images: list = None) -> str:
    """Routes to the configured provider. Same signature regardless of
    which provider is active — agents never need to know which one is used.

    `attempt` (1-indexed) lets a caller that retries the SAME logical call
    (currently only Self-Healer, via MAX_HEAL_ATTEMPTS in the heal stage)
    signal which attempt this is, so the Model Router can route a later
    attempt to a cheaper model than the first — see utils/model_router.py.

    `images` (optional) is a list of local file paths — used by
    testcase_writer.py to actually show the model Figma screenshots
    extracted from a design doc, instead of leaving them on disk unseen
    (the previous text-only-call limitation). Silently ignored (not an
    error) by providers/models this file doesn't wire up vision for; the
    call still succeeds text-only in that case, just without the images."""
    skill_text = _load_skill(agent_name)
    shared_instructions = _load_shared_instructions()
    model = _resolve_model(agent_name, attempt=attempt)
    prepared_images = _prepare_images(images) if images else []

    dispatch = {
        "anthropic": _call_anthropic,
        "openai": _call_openai,
        "azure_openai": _call_azure_openai,
        "openrouter": _call_openrouter,
        "ollama": _call_ollama,
    }
    if AI_PROVIDER == "github_models":
        raise ValueError(
            "AI_PROVIDER='github_models' was retired by GitHub on 2026-07-30 and no "
            "longer works. Switch to 'anthropic', 'openai', 'azure_openai', or "
            "'openrouter' in .env."
        )
    if AI_PROVIDER not in dispatch:
        raise ValueError(
            f"Unknown AI_PROVIDER='{AI_PROVIDER}'. Valid options: {list(dispatch.keys())}"
        )

    friendly = AGENT_FRIENDLY_ACTION.get(agent_name, f"running the {agent_name} step")
    print(f"      \U0001f916 {friendly}...")
    print(f"         (technical: calling {model} for '{agent_name}', max_tokens={max_tokens})")
    start = time.time()
    # Retried HERE, at the call site, not only by the stage wrapper. A rate
    # limit or a dropped connection belongs to this one request; recovering it
    # here keeps the Coding Agent from silently degrading a chunk's cases to
    # "unresolved" over a two-second blip, and keeps the Planner from re-paying
    # for five already-completed batches to recover the sixth. Only transient
    # exception CLASSES are retried — a malformed response or a bad key still
    # propagates on the first attempt (see utils/transient.call_with_retry).
    provider_kwargs = {}
    if AI_PROVIDER == "anthropic" and extra_system and agent_name in _STABLE_EXTRA_SYSTEM_AGENTS:
        # Only passed when True so a provider function without the parameter
        # (or a test double) is never handed an argument it does not know.
        provider_kwargs["extra_system_stable"] = True
    text, usage = call_with_retry(
        lambda: dispatch[AI_PROVIDER](
            model, skill_text, extra_system, user_prompt, max_tokens,
            shared_instructions=shared_instructions, images=prepared_images,
            **provider_kwargs,
        ),
        label=f"{agent_name} ({model})",
    )
    elapsed = round(time.time() - start, 2)
    print(f"      ✓ done in {elapsed}s "
          f"({usage.get('output_tokens', '?')} tokens of output)")

    # Exposed as thread-local module state rather than changing call_agent's
    # -> str return type, which five call sites depend on. parse_json_response
    # reads it to tell a truncated response apart from a malformed one.
    # Set BEFORE logging: a logging failure must not lose a paid response or
    # leave the truncation flag stale.
    _thread_state.last_usage = usage

    try:
        _log_usage(feature_dir, agent_name=agent_name, provider=AI_PROVIDER, model=model,
                   elapsed_sec=elapsed, attempt=attempt, **usage)
    except Exception as exc:  # noqa: BLE001 — cost logging is best-effort
        print(f"      ⚠️  Could not write token_log.json ({type(exc).__name__}: {exc}); "
              f"the response is unaffected.")

    return text


def preflight() -> tuple:
    """One tiny real request through the configured provider.

    Returns (ok, reason, kind). `kind` is "infra" for a missing/invalid key,
    exhausted quota or billing, rate limit or an unreachable endpoint — things
    no re-run of a stage can fix — and "content" for anything else.

    Why this exists: a run used to discover a dead model API only when a stage
    reached its first call, after minutes of crawling and generation, and then
    spent the stage's whole retry budget confirming it. A one-token request
    costs next to nothing and says so up front.
    """
    from utils.error_classify import classify_error_kind
    dispatch = {
        "anthropic": _call_anthropic, "openai": _call_openai,
        "azure_openai": _call_azure_openai, "openrouter": _call_openrouter,
        "ollama": _call_ollama,
    }
    if AI_PROVIDER not in dispatch:
        return False, (f"AI_PROVIDER={AI_PROVIDER!r} is not one of {sorted(dispatch)}"), "infra"
    try:
        model = _resolve_model("reviewer")
        dispatch[AI_PROVIDER](model, "You are a connectivity check.", "",
                              "Reply with the single word OK.", 8,
                              shared_instructions="", images=[])
    except Exception as exc:  # noqa: BLE001 — any failure is the answer here
        text = f"{type(exc).__name__}: {exc}"
        return False, f"{AI_PROVIDER} ({_safe_model_name()}): {text[:400]}", classify_error_kind(exc)
    return True, "", ""


def _safe_model_name() -> str:
    try:
        return _resolve_model("reviewer")
    except Exception:  # noqa: BLE001
        return "?"


# --------------------------------------------------------------------------
# Provider-specific call implementations
# --------------------------------------------------------------------------

def _require_env(name: str) -> str:
    """Same intent as the plain-message credential checks in
    testcase_writer.py/jira_uploader.py: a missing key should raise a clear,
    labeled error, not an unhandled `KeyError: '<NAME>'` several frames deep
    inside a provider call."""
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is not set — add it to your .env (see .env.example).")
    return value


#: Agents whose `extra_system` is byte-identical across their calls within a run
#: (coding_agent: repo conventions + design doc, see its "byte-identical on every
#: chunk" note), so it is worth a cache breakpoint of its own. For every other
#: agent extra_system varies per call (per-story ledger block, a repair note),
#: and a breakpoint after it would WRITE a cache entry that is never read.
_STABLE_EXTRA_SYSTEM_AGENTS = frozenset({"coding_agent"})

#: One client per API key: building a new one per call discarded the connection
#: pool. max_retries=0 because call_with_retry already owns retrying — leaving
#: the SDK default (2) multiplied the attempts (3 x N) on every transient fault.
_ANTHROPIC_CLIENTS: dict = {}
_ANTHROPIC_CLIENT_LOCK = threading.Lock()


def _anthropic_client(api_key: str):
    import anthropic
    with _ANTHROPIC_CLIENT_LOCK:
        client = _ANTHROPIC_CLIENTS.get(api_key)
        if client is None:
            client = anthropic.Anthropic(api_key=api_key, max_retries=0)
            _ANTHROPIC_CLIENTS[api_key] = client
        return client


def _call_anthropic(model, skill_text, extra_system, user_prompt, max_tokens,
                     shared_instructions="", images=None, extra_system_stable=False):
    client = _anthropic_client(_require_env("ANTHROPIC_API_KEY"))

    # Shared instructions go FIRST, ahead of the agent-specific skill — both
    # are stable across every call for this agent (unlike extra_system, which
    # varies per run: repo conventions, coding standards, known-flaky list).
    # Combining them gives the cached prefix a much better chance of clearing
    # Anthropic's per-model cache-eligibility floor than skill_text alone —
    # see the "SHARED INSTRUCTIONS PREFIX" note in this module's docstring.
    system_blocks = []
    if shared_instructions:
        system_blocks.append({"type": "text", "text": shared_instructions})
    system_blocks.append({"type": "text", "text": skill_text})
    if extra_system:
        system_blocks.append({"type": "text", "text": extra_system})
    # A cache breakpoint marks the END of a cacheable prefix. The stable prefix
    # (shared instructions + skill) gets one always. extra_system is only added
    # to the cached prefix when the caller's agent is known to keep it
    # byte-identical across calls: a breakpoint after content that changes per
    # call writes a new cache entry every time and never reads one, so the
    # stable prefix would be paid for at the write rate without ever being reused.
    skill_index = 1 if shared_instructions else 0
    system_blocks[skill_index]["cache_control"] = {"type": "ephemeral"}
    if extra_system and extra_system_stable:
        system_blocks[-1]["cache_control"] = {"type": "ephemeral"}

    # Images go BEFORE the text block in the user turn — Anthropic's own
    # guidance is that a model attends to an image better when it precedes
    # the text referencing it, rather than the image trailing a long prompt.
    user_content = [
        {"type": "image", "source": {"type": "base64", "media_type": img["media_type"], "data": img["data"]}}
        for img in (images or [])
    ]
    user_content.append({"type": "text", "text": user_prompt})

    response = client.messages.create(
        model=model, max_tokens=max_tokens, system=system_blocks,
        messages=[{"role": "user", "content": user_content}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cache_creation_input_tokens": getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(response.usage, "cache_read_input_tokens", 0) or 0,
        # Surfaced so agents can tell "model produced bad JSON" apart from
        # "model was cut off mid-JSON because max_tokens was too low".
        "truncated": getattr(response, "stop_reason", None) == "max_tokens",
    }
    return text, usage


def _call_openai(model, skill_text, extra_system, user_prompt, max_tokens,
                  shared_instructions="", images=None):
    from openai import OpenAI
    client = OpenAI(api_key=_require_env("OPENAI_API_KEY"))
    return _call_openai_compatible(client, model, skill_text, extra_system, user_prompt, max_tokens,
                                    shared_instructions, images=images)


def _call_azure_openai(model, skill_text, extra_system, user_prompt, max_tokens,
                        shared_instructions="", images=None):
    from openai import AzureOpenAI
    client = AzureOpenAI(
        api_key=_require_env("AZURE_OPENAI_API_KEY"),
        azure_endpoint=_require_env("AZURE_OPENAI_ENDPOINT"),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    return _call_openai_compatible(client, model, skill_text, extra_system, user_prompt, max_tokens,
                                    shared_instructions, images=images)


def _call_openrouter(model, skill_text, extra_system, user_prompt, max_tokens,
                      shared_instructions="", images=None):
    from openai import OpenAI
    # OpenRouter exposes an OpenAI-compatible endpoint, authenticated with a
    # single OpenRouter API key (openrouter.ai/keys) — no cloud account needed.
    client = OpenAI(
        api_key=_require_env("OPENROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
    )
    return _call_openai_compatible(client, model, skill_text, extra_system, user_prompt, max_tokens,
                                    shared_instructions, images=images)


def _call_ollama(model, skill_text, extra_system, user_prompt, max_tokens,
                  shared_instructions="", images=None):
    from openai import OpenAI
    # Ollama exposes an OpenAI-compatible endpoint on a local server — no API
    # key/network dependency. The key value is ignored by Ollama but the
    # OpenAI client requires a non-empty string.
    base_url = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434") + "/v1"
    client = OpenAI(api_key="ollama", base_url=base_url)
    return _call_openai_compatible(client, model, skill_text, extra_system, user_prompt, max_tokens,
                                    shared_instructions, images=images)


def _call_openai_compatible(client, model, skill_text, extra_system, user_prompt, max_tokens,
                             shared_instructions="", images=None):
    """Shared logic for any OpenAI-wire-format provider (openai, azure_openai,
    openrouter, ollama). No native prompt caching on this path today — OpenAI
    auto-caches repeated prefixes server-side for supported models, but it
    isn't a feature you configure explicitly like Anthropic's cache_control.

    `images`, if given, are sent as `image_url` data-URI blocks alongside the
    text — Ollama/OpenRouter routes to whatever underlying model is
    configured, which may or may not itself support vision; a non-vision
    model on this path will error from the provider's own API on an image
    input, not from this code."""
    system_content = (
        (shared_instructions + "\n\n" if shared_instructions else "")
        + skill_text
        + (f"\n\n{extra_system}" if extra_system else "")
    )
    user_content = user_prompt
    if images:
        user_content = [{"type": "text", "text": user_prompt}] + [
            {"type": "image_url", "image_url": {"url": f"data:{img['media_type']};base64,{img['data']}"}}
            for img in images
        ]
    response = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content},
        ],
    )
    text = response.choices[0].message.content
    usage = {
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": getattr(response.usage, "prompt_tokens_details", None)
            and getattr(response.usage.prompt_tokens_details, "cached_tokens", 0) or 0,
        # Same normalization as the Anthropic path — "length" is this wire
        # format's way of saying the response hit max_tokens mid-generation.
        "truncated": response.choices[0].finish_reason == "length",
    }
    return text, usage


# --------------------------------------------------------------------------
# Cost logging (provider-agnostic)
# --------------------------------------------------------------------------

#: token_log.json and summary.md are both read-modify-write: read the whole
#: file, append, write the whole file back. Fine for the previous strictly
#: sequential pipeline, but testcase_writer/coding_agent now call the LLM
#: concurrently across stories/chunks (see their ThreadPoolExecutor usage) —
#: without a lock, two threads finishing at once can each read the same
#: "before" state and one call's log entry silently overwrites the other's
#: instead of both being appended.
_CONTEXT_FILE_LOCK = threading.Lock()


def _log_usage(feature_dir: Path, **entry):
    feature_dir.mkdir(parents=True, exist_ok=True)
    log_path = artifact_path(feature_dir, "token_log.json")

    with _CONTEXT_FILE_LOCK:
        # read_json/write_json rather than json.loads(read_text()): explicit
        # UTF-8 both ways, an atomic replace (this file is rewritten whole on
        # every single LLM call, so a run killed mid-write left a truncated
        # log that then raised from in here on the NEXT call), and a corrupt
        # log degrades to "start a fresh one" instead of taking down a stage
        # that has nothing to do with cost logging.
        log = read_json(log_path, default=[])
        entry["timestamp"] = utc_iso()
        log.append(entry)
        write_json(log_path, log)


def summarize_run_cost(feature_dir: Path, since: str = None) -> dict:
    """Totals token usage from token_log.json.

    `since` is an ISO-8601 timestamp (as written by _log_usage); entries older
    than it are excluded. token_log.json accumulates across every run against
    a feature, so without `since` this returns LIFETIME totals for that
    feature, not the current run's. The orchestrator stamps a run-start time
    and passes it here so "Token usage this run" means what it says.
    Default stays unfiltered so README's all-time usage example still holds.
    """
    log_path = artifact_path(feature_dir, "token_log.json")
    if not log_path.exists():
        return {}

    log = read_json(log_path, default=[])
    if since:
        log = [e for e in log if e.get("timestamp", "") >= since]
    summary = {}
    totals = {"input_tokens": 0, "output_tokens": 0,
              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

    for entry in log:
        agent = entry["agent_name"]
        summary.setdefault(agent, {
            "provider": entry.get("provider", "?"), "model": entry.get("model", "?"),
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
            "calls": 0,
        })
        for key in ("input_tokens", "output_tokens",
                    "cache_creation_input_tokens", "cache_read_input_tokens"):
            val = entry.get(key, 0)
            summary[agent][key] += val
            totals[key] += val
        summary[agent]["calls"] += 1

    totals["calls"] = len(log)
    summary["_total"] = totals
    return summary


# --------------------------------------------------------------------------
# Condensed context file helpers
# --------------------------------------------------------------------------

def start_run_summary(feature_dir: Path):
    """Archives the previous run's summary.md and starts a fresh one.

    append_context_summary only ever grows the file, and the Automation
    Planner re-sends the whole thing as `prior_context` on every run — so
    without a per-run boundary, every re-run against the same feature pays to
    re-send all previous runs' notes. Archived rather than deleted: the flat
    file store is the deliberate human-readable audit trail.
    """
    path = artifact_path(feature_dir, "summary.md")
    if not path.exists():
        return
    stamp = utc_stamp("%Y%m%d_%H%M%S")
    archive = artifact_path(feature_dir, f"summary.{stamp}.md")
    # The archive is routed to _internal/, which a fresh run folder lacks;
    # Path.replace does not create the destination's parent.
    archive.parent.mkdir(parents=True, exist_ok=True)
    path.replace(archive)


def read_context_summary(feature_dir: Path) -> str:
    # read_text_file already returns "" for a missing/unreadable file, so the
    # existence check the old version needed is folded into the `or`.
    return read_text_file(artifact_path(feature_dir, "summary.md")) or "(no prior context yet)"


def append_context_summary(feature_dir: Path, agent_name: str, note: str):
    """Appends one agent's distilled note to summary.md.

    Explicitly UTF-8 in BOTH directions (via artifact_io), which it was not:
    every note here is either LLM prose or this pipeline's own "⚠️"-prefixed
    warnings, and a bare write_text() encodes through the platform locale
    codepage — cp1252 on Windows. So the moment any agent's note contained an
    em dash, a curly quote, or a warning marker, the WHOLE STAGE died with
    UnicodeEncodeError in here, after its real work (and its LLM spend) was
    already complete. Reading it back through cp1252 mojibaked the prior
    context that then got re-sent to the Planner.

    agents/* were all fixed for this (tests/test_agent_contracts.py has a
    source-inspection guard), but the guard only walked agents/ — and this,
    the one file EVERY agent appends to, lives in utils/.
    """
    path = artifact_path(feature_dir, "summary.md")
    with _CONTEXT_FILE_LOCK:
        existing = read_text_file(path) or "# Feature Context Summary\n\n"
        stamp = utc_stamp("%Y-%m-%d %H:%M UTC")
        existing += f"## {agent_name} ({stamp})\n{note.strip()}\n\n"
        write_text_file(path, existing)
