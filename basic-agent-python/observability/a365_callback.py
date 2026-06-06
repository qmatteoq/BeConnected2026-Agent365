# A365 Observability — best-effort instrumentation (verify against official sample)
"""LangChain async callback handler that wraps LLM and tool calls in real A365 scopes.

Mirrors the C# basic-agent pattern:
  * ``InferenceScope`` wraps the actual LLM call (real start/end timing).
  * ``ExecuteToolScope`` wraps each tool invocation (real start/end timing).

Without this, post-hoc synthesised spans have zero duration and MAC drops or
hides the trace because parent-child timing is inconsistent.
"""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from langchain_core.callbacks.base import AsyncCallbackHandler
from langchain_core.outputs import LLMResult

from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    CallerDetails,
    ExecuteToolScope,
    InferenceCallDetails,
    InferenceOperationType,
    InferenceScope,
    Request,
    ServiceEndpoint,
    SpanDetails,
    ToolCallDetails,
)

logger = logging.getLogger(__name__)


class A365TracingCallback(AsyncCallbackHandler):
    """Opens InferenceScope/ExecuteToolScope around real LLM and tool invocations.

    Keyed by LangChain ``run_id`` so concurrent sub-runs don't clobber each other.
    """

    # Tell LangChain to keep dispatching even when tracing is "on" elsewhere.
    raise_error: bool = False
    ignore_chain: bool = True
    ignore_agent: bool = True
    ignore_retriever: bool = True
    ignore_chat_model: bool = False
    ignore_llm: bool = False

    def __init__(
        self,
        a365_request: Request,
        agent_details: AgentDetails,
        caller_details: CallerDetails,
        tools_by_name: dict[str, Any],
        llm_endpoint: ServiceEndpoint,
        default_model: str,
        parent_context: Any | None = None,
    ) -> None:
        super().__init__()
        self._request = a365_request
        self._agent_details = agent_details
        self._caller_details = caller_details
        self._user_details = caller_details.user_details
        self._tools_by_name = tools_by_name
        self._llm_endpoint = llm_endpoint
        self._default_model = default_model
        # OTel context of the enclosing InvokeAgentScope. Passed explicitly as the
        # parent of every child scope so InferenceScope/ExecuteToolScope nest under
        # the agent span regardless of how LangGraph propagates the async context.
        # Without this, tool scopes can become orphaned root spans that the A365
        # exporter silently drops (child scopes require a parent InvokeAgentScope).
        self._parent_context = parent_context
        # run_id -> live scope (manually entered)
        self._inference_scopes: dict[UUID, InferenceScope] = {}
        self._tool_scopes: dict[UUID, ExecuteToolScope] = {}

    # ------------------------------------------------------------------
    # LLM / chat model
    # ------------------------------------------------------------------
    async def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[Any]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._start_inference(serialized, run_id, messages_for_input=messages)

    async def on_llm_start(
        self,
        serialized: dict[str, Any],
        prompts: list[str],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        await self._start_inference(serialized, run_id, prompts_for_input=prompts)

    async def _start_inference(
        self,
        serialized: dict[str, Any],
        run_id: UUID,
        *,
        messages_for_input: list[list[Any]] | None = None,
        prompts_for_input: list[str] | None = None,
    ) -> None:
        try:
            model = self._extract_model(serialized) or self._default_model
            details = InferenceCallDetails(
                operationName=InferenceOperationType.CHAT,
                model=model,
                providerName="azure-openai",
                endpoint=self._llm_endpoint,
            )
            scope = InferenceScope.start(
                self._request,
                details,
                self._agent_details,
                self._user_details,
                SpanDetails(parent_context=self._parent_context)
                if self._parent_context is not None
                else None,
            )
            scope.__enter__()
            self._inference_scopes[run_id] = scope

            inputs: list[str] = []
            if messages_for_input:
                for batch in messages_for_input:
                    for m in batch:
                        content = getattr(m, "content", None)
                        if isinstance(content, str) and content:
                            inputs.append(content)
            elif prompts_for_input:
                inputs.extend(prompts_for_input)
            if inputs:
                scope.record_input_messages(inputs)
            logger.info("[A365] → InferenceScope started   model=%s run=%s", model, run_id)
        except Exception:
            logger.warning("[A365] ⚠️  InferenceScope start failed", exc_info=True)

    async def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        scope = self._inference_scopes.pop(run_id, None)
        if scope is None:
            return
        try:
            outputs: list[str] = []
            input_tokens = 0
            output_tokens = 0
            for gen_list in response.generations or []:
                for gen in gen_list:
                    text = getattr(gen, "text", None)
                    if isinstance(text, str) and text:
                        outputs.append(text)
                        continue
                    msg = getattr(gen, "message", None)
                    content = getattr(msg, "content", None) if msg is not None else None
                    if isinstance(content, str) and content:
                        outputs.append(content)
            usage = (response.llm_output or {}).get("token_usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            if outputs:
                scope.record_output_messages(outputs)
            if input_tokens:
                scope.record_input_tokens(input_tokens)
            if output_tokens:
                scope.record_output_tokens(output_tokens)
            logger.info(
                "[A365] ← InferenceScope ended     in=%s out=%s run=%s",
                input_tokens or "?", output_tokens or "?", run_id,
            )
        except Exception:
            logger.warning("[A365] ⚠️  InferenceScope finalize failed", exc_info=True)
        finally:
            scope.__exit__(None, None, None)

    async def on_llm_error(
        self, error: BaseException, *, run_id: UUID, **kwargs: Any
    ) -> None:
        scope = self._inference_scopes.pop(run_id, None)
        if scope is None:
            return
        scope.__exit__(type(error), error, error.__traceback__)

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------
    # NOTE: LangGraph's ToolNode does not reliably propagate ``config["callbacks"]``
    # to tool execution, so on_tool_start/on_tool_end frequently never fire (the
    # LLM node callbacks do). Instead of relying on them, the MCP tools are wrapped
    # (see observability/tool_tracing.py) and call the explicit open/close methods
    # below, guaranteeing an ExecuteToolScope is created under the InvokeAgentScope.
    def open_tool_scope(self, tool: Any, arguments: Any) -> ExecuteToolScope | None:
        try:
            tool_name = getattr(tool, "name", None) or "unknown"
            tool_details = ToolCallDetails(
                tool_name=tool_name,
                tool_type="mcp",
                arguments=arguments,
                description=getattr(tool, "description", "") or "",
                endpoint=ServiceEndpoint(hostname="learn.microsoft.com", port=443),
            )
            scope = ExecuteToolScope.start(
                self._request,
                tool_details,
                self._agent_details,
                self._user_details,
                SpanDetails(parent_context=self._parent_context)
                if self._parent_context is not None
                else None,
            )
            scope.__enter__()
            logger.info("[A365] → ExecuteToolScope start   tool=%s", tool_name)
            return scope
        except Exception:
            logger.warning("[A365] ⚠️  ExecuteToolScope start failed", exc_info=True)
            return None

    def close_tool_scope(self, scope: ExecuteToolScope | None, output: Any) -> None:
        if scope is None:
            return
        try:
            content = getattr(output, "content", output)
            if not isinstance(content, (str, dict)):
                content = str(content)
            scope.record_response(content)
            logger.info("[A365] ← ExecuteToolScope ended")
        except Exception:
            logger.warning("[A365] ⚠️  ExecuteToolScope finalize failed", exc_info=True)
        finally:
            scope.__exit__(None, None, None)

    def fail_tool_scope(self, scope: ExecuteToolScope | None, error: BaseException) -> None:
        if scope is None:
            return
        scope.__exit__(type(error), error, error.__traceback__)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_model(serialized: dict[str, Any] | None) -> str | None:
        if not serialized:
            return None
        kwargs = serialized.get("kwargs") or {}
        for key in ("azure_deployment", "deployment_name", "model_name", "model"):
            val = kwargs.get(key)
            if isinstance(val, str) and val:
                return val
        return None
