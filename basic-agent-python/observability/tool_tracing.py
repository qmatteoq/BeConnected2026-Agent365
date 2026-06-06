# A365 Observability — tool-execution tracing via coroutine wrapping.
"""Wrap MCP/LangChain tools so each invocation opens a real A365 ExecuteToolScope.

Why this exists
---------------
LangGraph's ``ToolNode`` does not reliably propagate ``config["callbacks"]`` to
tool execution, so an ``AsyncCallbackHandler``'s ``on_tool_start`` frequently
never fires (the LLM/chat node callbacks do). The distro's automatic LangChain
instrumentor still captures tool spans because it is registered as a *global*
handler, but those spans lack the A365 identity attributes carried by the
``Agent365Sdk`` scopes, so the Agent 365 service drops them.

To guarantee a proper, correctly-nested ``ExecuteToolScope`` we wrap each tool's
coroutine. The wrapper reads the per-request tracer from a ``ContextVar`` (which
propagates through the LangGraph async task tree just like the OTel context),
opens the scope around the real tool call, and records the response.
"""
from __future__ import annotations

import functools
import logging
from contextvars import ContextVar
from typing import Any

logger = logging.getLogger(__name__)

# Per-request A365TracingCallback (or None). Set inside the /api/chat handler
# (within the InvokeAgentScope block) before invoking the agent, so wrapped
# tools can open an ExecuteToolScope nested under the active InvokeAgentScope.
current_tool_tracer: ContextVar[Any | None] = ContextVar(
    "a365_current_tool_tracer", default=None
)


def wrap_tools_for_a365(tools: list[Any]) -> list[Any]:
    """Wrap each tool's coroutine in-place to emit an A365 ExecuteToolScope."""
    for tool in tools:
        _wrap_tool_coroutine(tool)
    return tools


def _wrap_tool_coroutine(tool: Any) -> None:
    orig_coroutine = getattr(tool, "coroutine", None)
    if orig_coroutine is None:
        return

    @functools.wraps(orig_coroutine)
    async def traced_coroutine(*args: Any, _orig=orig_coroutine, _tool=tool, **kwargs: Any) -> Any:
        tracer = current_tool_tracer.get()
        if tracer is None:
            return await _orig(*args, **kwargs)

        arguments: Any = kwargs if kwargs else (args[0] if len(args) == 1 else list(args))
        scope = tracer.open_tool_scope(_tool, arguments)
        try:
            result = await _orig(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised after recording
            tracer.fail_tool_scope(scope, exc)
            raise
        tracer.close_tool_scope(scope, result)
        return result

    # StructuredTool is a pydantic model; bypass validation to swap the coroutine.
    object.__setattr__(tool, "coroutine", traced_coroutine)
