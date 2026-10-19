"""
Thin sync wrapper around a real Playwright MCP server (github.com/microsoft/
playwright-mcp), used by utils/dom_inspector.py in place of driving Playwright
directly in-process — this is the FIRST-CHOICE mechanism
("Playwright MCP server, or direct Playwright browser automation if no MCP
server is available"); dom_inspector.py's own sync_playwright() session was
the fallback, used only when this isn't configured.

Requires the `mcp` package (pip install mcp) and a reachable Playwright MCP
server, in either of two shapes:
  - PLAYWRIGHT_MCP_URL set  -> connects over SSE/HTTP to an already-running
    server (e.g. `npx @playwright/mcp@latest --port 8931`, run separately).
  - PLAYWRIGHT_MCP_URL unset -> launches one itself as a stdio subprocess via
    PLAYWRIGHT_MCP_COMMAND/PLAYWRIGHT_MCP_ARGS (defaults to
    `npx @playwright/mcp@latest`).

Gated behind USE_PLAYWRIGHT_MCP=true — unset/false means dom_inspector.py
keeps using its existing direct-Playwright path unchanged, so this is purely
additive.

The MCP session is a lazy-initialized, process-wide singleton (same
reuse-across-calls design as dom_inspector.py's own Playwright session) —
one login pays its cost once, not per inspect() call. The session lives on a
dedicated background event loop thread since the `mcp` SDK is async-only and
every other agent module in this pipeline is plain sync code.
"""

import asyncio
import atexit
import concurrent.futures
import contextlib
import os
import shlex
import threading

_loop = None
_loop_thread = None
_session = None
_stop_event = None
_runner_future = None
#: Consecutive tool-call failures; see call_tool.
_runtime_failures = 0
_MAX_RUNTIME_FAILURES = 5
_init_lock = threading.Lock()

#: Set once connecting has definitively failed, so the failure is paid ONCE
#: per process instead of once per inspect() call — the exact fix
#: dom_inspector._launch_failed already applies to the DIRECT Playwright
#: path ("74 cases meant 74 failed launches and 74 identical warnings").
#: This path had no equivalent: dom_inspector.inspect() catches the
#: exception and falls back per CASE, so with USE_PLAYWRIGHT_MCP=true and an
#: unreachable server, every case re-ran the whole 30-second connect —
#: leaking a half-initialized stdio subprocess each time (see _connect).
_connect_failed = False
_connect_error = ""


def available() -> bool:
    """Whether the MCP backend should be TRIED. Goes false once a connect
    has failed in this process, so dom_inspector stops re-attempting it for
    every remaining case and uses its direct-Playwright fallback instead."""
    if _connect_failed:
        return False
    return os.environ.get("USE_PLAYWRIGHT_MCP", "").strip().lower() in ("1", "true", "yes")


def _ensure_loop():
    global _loop, _loop_thread
    if _loop is not None:
        return _loop
    _loop = asyncio.new_event_loop()

    def _run():
        asyncio.set_event_loop(_loop)
        _loop.run_forever()

    _loop_thread = threading.Thread(target=_run, daemon=True, name="playwright-mcp-loop")
    _loop_thread.start()
    return _loop


def _run_coro(coro, timeout: float = 30):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        # Without this the coroutine kept running after the caller gave up:
        # a timed-out connect could still spawn an unowned stdio subprocess,
        # and a timed-out tool call could interleave with the NEXT call.
        future.cancel()
        raise


async def _session_runner(ready: "concurrent.futures.Future"):
    """Owns the whole MCP session lifecycle in ONE task.

    anyio-based transports (stdio_client) require their context to be exited
    by the same task that entered it; the previous code entered them in one
    run_coroutine_threadsafe task and exited them from other tasks in close(),
    which raises "exit cancel scope in a different task" (swallowed, leaking
    the subprocess). Here the AsyncExitStack is entered, held and unwound in
    this one task; close() just sets `stop`. If this task is cancelled (a
    connect timeout) the stack still unwinds, so nothing is left unowned.
    """
    from contextlib import AsyncExitStack

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    try:
        async with AsyncExitStack() as stack:
            mcp_url = os.environ.get("PLAYWRIGHT_MCP_URL", "").strip()
            if mcp_url:
                from mcp.client.sse import sse_client
                transport_cm = sse_client(mcp_url)
            else:
                command = os.environ.get("PLAYWRIGHT_MCP_COMMAND", "npx")
                args = shlex.split(os.environ.get("PLAYWRIGHT_MCP_ARGS", "@playwright/mcp@latest"))
                transport_cm = stdio_client(StdioServerParameters(command=command, args=args))
            read, write = await stack.enter_async_context(transport_cm)
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            stop = asyncio.Event()
            if not ready.done():
                ready.set_result((session, stop))
            await stop.wait()
    except BaseException as exc:
        if not ready.done():
            ready.set_exception(exc if isinstance(exc, Exception)
                                else RuntimeError(f"MCP session task ended: {type(exc).__name__}"))
        if not isinstance(exc, Exception):
            raise


def _get_session():
    global _session, _stop_event, _runner_future, _connect_failed, _connect_error
    with _init_lock:
        if _session is not None:
            return _session
        if _connect_failed:
            raise RuntimeError(f"Playwright MCP unavailable: {_connect_error}")
        ready = concurrent.futures.Future()
        loop = _ensure_loop()
        runner = asyncio.run_coroutine_threadsafe(_session_runner(ready), loop)
        try:
            _session, _stop_event = ready.result(timeout=30)
            _runner_future = runner
        except BaseException as exc:
            runner.cancel()  # unwinds the exit stack inside its own task
            # Remembered so available() goes false and the REMAINING cases
            # skip straight to the direct-Playwright fallback instead of each
            # paying this timeout again.
            _connect_failed = True
            _connect_error = f"{type(exc).__name__}: {exc}"
            print(f"⚠️  Playwright MCP: could not connect ({_connect_error}). "
                  f"Falling back to direct Playwright for the rest of this run — "
                  f"check PLAYWRIGHT_MCP_URL / PLAYWRIGHT_MCP_COMMAND, or unset "
                  f"USE_PLAYWRIGHT_MCP.")
            raise
        return _session


async def _call_tool_async(session, name: str, arguments: dict) -> str:
    result = await session.call_tool(name, arguments=arguments)
    # MCP tool results are a list of content blocks — text ones carry the
    # actual payload (a snapshot, a confirmation message, etc).
    texts = [c.text for c in result.content if getattr(c, "type", "") == "text"]
    text = "\n".join(texts)

    # A tool-level failure comes back as a NORMAL result carrying isError,
    # not as a raised exception — so this function returned the error text as
    # if it were the payload, and this module's own docstring's promise
    # ("Raises on connection/tool failure") did not hold for the second half.
    # It mattered most for the click helpers: dom_inspector._mcp_click_matching
    # reports "a click happened" purely on the absence of an exception, so a
    # browser_click that actually failed ("ref not found" after the page moved
    # on) counted as successful navigation, and the snapshot taken next was
    # attributed to a screen that was never reached.
    if getattr(result, "isError", False):
        raise RuntimeError(
            f"Playwright MCP tool '{name}' failed: {text or '(no detail returned)'}")
    return text


def call_tool(name: str, arguments: dict = None, timeout: float = 30) -> str:
    """Calls one Playwright MCP tool (e.g. "browser_navigate",
    "browser_snapshot", "browser_click", "browser_type") and returns its
    text content. Raises on connection/tool failure — callers (dom_inspector)
    are responsible for catching and falling back.

    After `_MAX_RUNTIME_FAILURES` CONSECUTIVE failures the backend is marked
    unavailable (available() goes false), so a dead session stops being
    retried by every remaining case. Any success resets the count."""
    global _runtime_failures, _connect_failed, _connect_error
    session = _get_session()
    try:
        text = _run_coro(_call_tool_async(session, name, arguments or {}), timeout=timeout)
    except Exception as exc:
        _runtime_failures += 1
        if _runtime_failures >= _MAX_RUNTIME_FAILURES and not _connect_failed:
            _connect_failed = True
            _connect_error = (f"{_runtime_failures} consecutive tool failures, last: "
                              f"{type(exc).__name__}: {exc}")
            print(f"⚠️  Playwright MCP: disabled after repeated failures ({_connect_error}). "
                  f"Falling back to direct Playwright for the rest of this run.")
        raise
    _runtime_failures = 0
    return text


def close():
    """Best-effort cleanup — registered via atexit, not required to be
    called manually."""
    global _session, _stop_event, _runner_future, _loop, _loop_thread
    if _loop is not None and _stop_event is not None and _runner_future is not None:
        try:
            _loop.call_soon_threadsafe(_stop_event.set)  # the runner task unwinds itself
            _runner_future.result(timeout=10)
        except Exception:
            with contextlib.suppress(Exception):
                _runner_future.cancel()
    if _loop is not None:
        _loop.call_soon_threadsafe(_loop.stop)
    # _loop must be cleared too, not just stopped: _ensure_loop() returns any
    # non-None _loop, so leaving the STOPPED loop in place meant a call_tool()
    # after close() submitted work to a loop that would never run it and then
    # blocked for the full timeout before failing. Cleared, so a later call
    # starts a fresh loop instead.
    _loop = _loop_thread = None
    _session = _stop_event = _runner_future = None


atexit.register(close)
