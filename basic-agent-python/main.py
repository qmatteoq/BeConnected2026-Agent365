"""Basic AI agent using LangChain + LangGraph with Azure OpenAI.

Mirrors the .NET basic-agent sample: a FastAPI web app that exposes
POST /api/sessions and POST /api/chat and serves a chat UI from wwwroot/.
The agent calls Microsoft Learn MCP tools to answer questions about
Microsoft technologies.
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_openai import AzureChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel
from urllib.parse import urlparse

from user_pool import SimulatedUser, UserPool

# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------
load_dotenv()

# Configure logging early so all [A365] INFO messages appear in the console.
# uvicorn only initialises the "uvicorn"/"uvicorn.error"/"uvicorn.access" loggers;
# without basicConfig, every other logger inherits root-level WARNING and our
# observability diagnostics stay invisible.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)

# A365 Observability — best-effort instrumentation (verify against official sample)
# Initialise the unified Microsoft OpenTelemetry distro BEFORE any LLM/framework
# imports do real work. authMode: S2S — 3-hop FMI token chain via background
# ObservabilityTokenService; no per-turn user OBO refresh.
from microsoft.opentelemetry import use_microsoft_opentelemetry  # noqa: E402
from microsoft.opentelemetry.a365.core import (  # noqa: E402
    InvokeAgentScope,
    InvokeAgentScopeDetails,
    Request,
    ServiceEndpoint,
)

from observability import obs_context, token_cache  # noqa: E402
from observability.a365_callback import A365TracingCallback  # noqa: E402
from observability.tool_tracing import current_tool_tracer, wrap_tools_for_a365  # noqa: E402
from observability.observability_token_service import (  # noqa: E402
    acquire_initial_token,
    run_token_service,
)

logger = logging.getLogger(__name__)

# Surface A365 exporter activity (HTTP send results, retries, errors) so we can
# tell whether spans actually reach the Observability API. The exporter logs the
# successful POST + URL at DEBUG, so use DEBUG for the exporter package.
logging.getLogger("microsoft.opentelemetry").setLevel(logging.INFO)
logging.getLogger("microsoft.opentelemetry.a365.core.exporters").setLevel(logging.DEBUG)

# Narrowly silence ONLY the optional OpenAI Agents SDK auto-instrumentor — it probes
# the `agents` module (OpenAI Agents SDK) which this LangChain agent does not use.
# We add a filter on the _distro logger instead of muting the entire module so that
# exporter failures (401, network errors) remain visible.
class _DropOpenAIAgentsTrace(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        return "OpenAI Agents instrumentation" not in msg

logging.getLogger("microsoft.opentelemetry._distro").addFilter(_DropOpenAIAgentsTrace())

# Idempotency guard: `python main.py` invokes uvicorn with the string "main:app",
# which re-imports this module under the name "main" while the script itself runs
# as "__main__". Without the guard, use_microsoft_opentelemetry() runs twice and
# trips "Overriding of current TracerProvider is not allowed" plus duplicate
# instrumentation warnings.
import sys as _sys  # noqa: E402

if not getattr(_sys.modules[__name__], "_A365_OTEL_INITIALIZED", False):
    use_microsoft_opentelemetry(
        enable_a365=True,
        enable_azure_monitor=False,
        enable_console=True,
        a365_use_s2s_endpoint=obs_context.USE_S2S_ENDPOINT,
        a365_enable_observability_exporter=True,
        a365_token_resolver=lambda aid, tid: token_cache.get_cached_token(aid, tid) or "",
    )
    _A365_OTEL_INITIALIZED = True

    # Custom OTLP exporter — the Distro auto-wires this when OTEL_EXPORTER_OTLP_*
    # env vars are present (multi-backend: A365 + OTLP + console at the same time).
    _otlp_endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if _otlp_endpoint:
        logger.info(
            "[A365] 📡 Custom OTLP exporter enabled  endpoint=%s protocol=%s",
            _otlp_endpoint,
            os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc"),
        )

AZURE_OPENAI_ENDPOINT = os.environ.get(
    "AZURE_OPENAI_ENDPOINT", "https://agentcon2025-resource.openai.azure.com/"
)
AZURE_OPENAI_DEPLOYMENT_NAME = os.environ.get(
    "AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1"
)
AZURE_OPENAI_API_VERSION = os.environ.get(
    "AZURE_OPENAI_API_VERSION", "2024-10-21"
)

MCP_ENDPOINT = "https://learn.microsoft.com/api/mcp"

SYSTEM_PROMPT = (
    "You are a helpful AI assistant with access to Microsoft Learn documentation. "
    "When answering questions about Microsoft technologies, Azure, .NET, or other "
    "Microsoft products, use the Microsoft Learn search tools to find accurate, "
    "up-to-date information from the official documentation. "
    "Be concise, clear, and helpful in your responses."
)

BASE_DIR = Path(__file__).resolve().parent


# ----------------------------------------------------------------------------
# Session storage
# ----------------------------------------------------------------------------
@dataclass
class SessionData:
    thread_id: str
    user: SimulatedUser


sessions: dict[str, SessionData] = {}
checkpointer = MemorySaver()


# ----------------------------------------------------------------------------
# Lifespan: build the LangGraph agent (with MCP tools) once at startup
# ----------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # DefaultAzureCredential -> bearer token provider for Azure OpenAI.
    credential = DefaultAzureCredential()
    token_provider = get_bearer_token_provider(
        credential, "https://cognitiveservices.azure.com/.default"
    )

    llm = AzureChatOpenAI(
        azure_endpoint=AZURE_OPENAI_ENDPOINT,
        azure_deployment=AZURE_OPENAI_DEPLOYMENT_NAME,
        api_version=AZURE_OPENAI_API_VERSION,
        azure_ad_token_provider=token_provider,
    )

    # Connect to the Microsoft Learn MCP server (streamable HTTP transport).
    mcp_client = MultiServerMCPClient(
        {
            "microsoft_learn": {
                "url": MCP_ENDPOINT,
                "transport": "streamable_http",
            }
        }
    )
    tools = await mcp_client.get_tools()

    # Wrap each tool so its execution opens a real A365 ExecuteToolScope, since
    # LangGraph's ToolNode does not reliably propagate config callbacks to tools.
    tools = wrap_tools_for_a365(tools)

    agent = create_react_agent(
        model=llm,
        tools=tools,
        prompt=SystemMessage(content=SYSTEM_PROMPT),
        checkpointer=checkpointer,
    )

    app.state.agent = agent
    app.state.user_pool = UserPool.load(BASE_DIR / "users.csv")
    app.state.mcp_client = mcp_client
    # Cache tool metadata for ExecuteToolScope spans.
    app.state.tools_by_name = {t.name: t for t in tools}

    # A365 Observability — log effective configuration so startup issues are visible.
    _log_a365_config()

    # Acquire the first observability token BEFORE serving requests so the exporter
    # does not fire with an empty token (causes HTTP 400 TenantIdInvalid).
    token_task: asyncio.Task | None = None
    if obs_context.has_a365_credentials():
        try:
            await acquire_initial_token(
                tenant_id=obs_context.TENANT_ID,
                agent_id=obs_context.AGENT_ID,
                blueprint_client_id=obs_context.CLIENT_ID,
                blueprint_client_secret=obs_context.CLIENT_SECRET,
                use_managed_identity=obs_context.USE_MANAGED_IDENTITY,
            )
            logger.info("[A365] ✅ Initial observability token acquired successfully.")
        except Exception:
            logger.warning(
                "[A365] ⚠️  Initial A365 observability token acquisition failed; continuing.",
                exc_info=True,
            )
        token_task = asyncio.create_task(
            run_token_service(
                tenant_id=obs_context.TENANT_ID,
                agent_id=obs_context.AGENT_ID,
                blueprint_client_id=obs_context.CLIENT_ID,
                blueprint_client_secret=obs_context.CLIENT_SECRET,
                use_managed_identity=obs_context.USE_MANAGED_IDENTITY,
            )
        )
    else:
        logger.warning(
            "[A365] ⚠️  Agent365 credentials not configured — skipping observability token service. "
            "Run 'a365 setup all' and populate AGENT365_* env vars to enable export."
        )

    try:
        yield
    finally:
        if token_task is not None:
            token_task.cancel()
            try:
                await token_task
            except (asyncio.CancelledError, Exception):
                pass


app = FastAPI(lifespan=lifespan)


# ----------------------------------------------------------------------------
# Models
# ----------------------------------------------------------------------------
class ChatRequest(BaseModel):
    message: str
    sessionId: str | None = None


# ----------------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------------
@app.post("/api/sessions")
async def create_session() -> dict[str, Any]:
    session_id = str(uuid.uuid4())
    user: SimulatedUser = app.state.user_pool.pick_random()
    sessions[session_id] = SessionData(thread_id=session_id, user=user)
    return {
        "sessionId": session_id,
        "userId": user.user_id,
        "userName": user.user_name,
        "userEmail": user.user_email,
    }


@app.post("/api/chat")
async def chat(request: ChatRequest) -> dict[str, Any]:
    if not request.message or not request.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    if request.sessionId and request.sessionId in sessions:
        data = sessions[request.sessionId]
        session_id = request.sessionId
    else:
        session_id = str(uuid.uuid4())
        user = app.state.user_pool.pick_random()
        data = SessionData(thread_id=session_id, user=user)
        sessions[session_id] = data

    config = {"configurable": {"thread_id": data.thread_id}}

    # A365 Observability — per-session caller identity, mirrors C# sample.
    caller_details = obs_context.caller_details_for(
        user_id=data.user.user_id,
        user_name=data.user.user_name,
        user_email=data.user.user_email,
    )

    a365_request = Request(
        content=request.message,
        session_id=session_id,
        conversation_id=data.thread_id,
    )

    # Use the real Azure OpenAI hostname so MAC can group traces by service.
    parsed_endpoint = urlparse(AZURE_OPENAI_ENDPOINT)
    llm_endpoint = ServiceEndpoint(
        hostname=parsed_endpoint.hostname or "localhost",
        port=parsed_endpoint.port or (443 if parsed_endpoint.scheme == "https" else 80),
    )
    invoke_details = InvokeAgentScopeDetails(endpoint=llm_endpoint)

    with InvokeAgentScope.start(
        a365_request,
        invoke_details,
        obs_context.agent_details,
        caller_details,
    ) as invoke_scope:
        logger.info("[A365] → InvokeAgentScope started  session=%s user=%s", session_id, data.user.user_id)
        invoke_scope.record_input_messages([request.message])

        # LangChain async callback handler — opens InferenceScope around the real LLM
        # call and ExecuteToolScope around each tool call, matching the C# pattern.
        # Pass the InvokeAgentScope's OTel context as the explicit parent so child
        # scopes nest correctly (LangGraph runs the ToolNode in a step where the
        # agent span is not ambient, which otherwise orphans tool spans and the
        # A365 exporter drops them).
        a365_callback = A365TracingCallback(
            a365_request=a365_request,
            agent_details=obs_context.agent_details,
            caller_details=caller_details,
            tools_by_name=app.state.tools_by_name,
            llm_endpoint=llm_endpoint,
            default_model=AZURE_OPENAI_DEPLOYMENT_NAME,
            parent_context=invoke_scope.get_context(),
        )

        # Make this request's tracer visible to the wrapped MCP tools (the context
        # var propagates through LangGraph's async task tree). Reset afterwards so
        # it never leaks across requests.
        tracer_token = current_tool_tracer.set(a365_callback)
        try:
            result = await app.state.agent.ainvoke(
                {"messages": [HumanMessage(content=request.message)]},
                config={**config, "callbacks": [a365_callback]},
            )
        finally:
            current_tool_tracer.reset(tracer_token)

        messages = (result or {}).get("messages") or []
        reply_text = ""
        if messages:
            last = messages[-1]
            reply_text = getattr(last, "content", "") or ""

        # Diagnostic: surface whether the model actually requested any tool calls.
        # If this logs 0 tool calls, the model answered without using MCP tools, so
        # on_tool_start (and ExecuteToolScope) will not fire — that is expected.
        tool_call_count = 0
        tool_names: list[str] = []
        for m in messages:
            for tc in getattr(m, "tool_calls", None) or []:
                tool_call_count += 1
                name = tc.get("name") if isinstance(tc, dict) else getattr(tc, "name", None)
                if name:
                    tool_names.append(name)
        logger.info(
            "[A365] tool calls requested by model: %s%s",
            tool_call_count,
            f" ({', '.join(tool_names)})" if tool_names else "",
        )

        invoke_scope.record_output_messages([reply_text])
        logger.info("[A365] ← InvokeAgentScope ended    session=%s", session_id)

    return {
        "reply": reply_text,
        "sessionId": session_id,
        "userId": data.user.user_id,
        "userName": data.user.user_name,
        "userEmail": data.user.user_email,
    }


# ----------------------------------------------------------------------------
# A365 Observability — startup config diagnostics
# ----------------------------------------------------------------------------
def _log_a365_config() -> None:
    """Print an A365 observability config summary so startup issues are immediately visible."""
    from observability import token_cache as _tc

    enabled = os.environ.get("ENABLE_A365_OBSERVABILITY", "false")
    exporter = os.environ.get("ENABLE_A365_OBSERVABILITY_EXPORTER", "false")
    logger.info(
        "[A365] ── Observability configuration ──────────────────────────────\n"
        "       ENABLE_A365_OBSERVABILITY         : %s\n"
        "       ENABLE_A365_OBSERVABILITY_EXPORTER: %s\n"
        "       AGENT365_TENANT_ID               : %s\n"
        "       AGENT365_AGENT_ID                : %s\n"
        "       AGENT365_BLUEPRINT_ID            : %s\n"
        "       AGENT365_CLIENT_ID               : %s\n"
        "       AGENT365_CLIENT_SECRET           : %s\n"
        "       AGENT365_USE_MANAGED_IDENTITY    : %s\n"
        "       AGENT365_USE_S2S_ENDPOINT        : %s\n"
        "       AGENT365_SPONSOR_USER_ID         : %s\n"
        "       credentials_ok                   : %s\n"
        "   ──────────────────────────────────────────────────────────────────",
        enabled,
        exporter,
        obs_context.TENANT_ID or "(not set)",
        obs_context.AGENT_ID or "(not set)",
        obs_context.BLUEPRINT_ID or "(not set)",
        obs_context.CLIENT_ID or "(not set)",
        ("***" + obs_context.CLIENT_SECRET[-4:]) if obs_context.CLIENT_SECRET else "(not set)",
        obs_context.USE_MANAGED_IDENTITY,
        obs_context.USE_S2S_ENDPOINT,
        os.environ.get("AGENT365_SPONSOR_USER_ID", "(not set)"),
        obs_context.has_a365_credentials(),
    )


# ----------------------------------------------------------------------------
# Static chat UI (wwwroot/index.html)
# ----------------------------------------------------------------------------
wwwroot = BASE_DIR / "wwwroot"


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(wwwroot / "index.html")


app.mount("/", StaticFiles(directory=str(wwwroot), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    # Pass the app object (not the "main:app" import string) so uvicorn does not
    # re-import this module under a different name and re-run A365 OTel setup.
    uvicorn.run(app, host="0.0.0.0", port=5000, reload=False)
