# A365 Observability — best-effort instrumentation (verify against official sample)
"""Shared Agent365 observability identity (AgentDetails + CallerDetails)."""

import os

from microsoft.opentelemetry.a365.core import AgentDetails, CallerDetails, UserDetails

A365_ENABLED = os.environ.get("ENABLE_A365_OBSERVABILITY", "").lower() == "true"
TENANT_ID = os.environ.get("AGENT365_TENANT_ID", "")
AGENT_ID = os.environ.get("AGENT365_AGENT_ID", "")
BLUEPRINT_ID = os.environ.get("AGENT365_BLUEPRINT_ID", "")
CLIENT_ID = os.environ.get("AGENT365_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AGENT365_CLIENT_SECRET", "")
USE_MANAGED_IDENTITY = os.environ.get("AGENT365_USE_MANAGED_IDENTITY", "false").lower() == "true"
USE_S2S_ENDPOINT = os.environ.get("AGENT365_USE_S2S_ENDPOINT", "true").lower() == "true"

AGENT_NAME = os.environ.get("AGENT365_AGENT_NAME", "python-langchain-agent")
AGENT_DESCRIPTION = os.environ.get("AGENT365_AGENT_DESCRIPTION", "")

agent_details = AgentDetails(
    agent_id=AGENT_ID or "local-dev",
    agent_name=AGENT_NAME,
    agent_description=AGENT_DESCRIPTION,
    agent_blueprint_id=BLUEPRINT_ID,
    tenant_id=TENANT_ID or "local-dev",
)

# CallerDetails — for autonomous (S2S) agents, use Blueprint sponsor identity.
# Without CallerDetails, traces will NOT appear in Microsoft Admin Center.
# Fallback used only when no per-session simulated user is available.
caller_details = CallerDetails(
    user_details=UserDetails(
        user_id=os.environ.get("AGENT365_SPONSOR_USER_ID", BLUEPRINT_ID),
        user_email=os.environ.get("AGENT365_SPONSOR_USER_EMAIL", ""),
        user_name=os.environ.get("AGENT365_SPONSOR_USER_NAME", ""),
    ),
)


def caller_details_for(user_id: str, user_name: str = "", user_email: str = "") -> CallerDetails:
    """Build per-session CallerDetails from a SimulatedUser (matches C# sample)."""
    return CallerDetails(
        user_details=UserDetails(
            user_id=user_id,
            user_email=user_email,
            user_name=user_name,
        ),
    )


def has_a365_credentials() -> bool:
    required = [TENANT_ID, AGENT_ID, CLIENT_ID]
    if not all(v and not v.startswith("<<") for v in required):
        return False
    if USE_MANAGED_IDENTITY:
        return True
    return bool(CLIENT_SECRET) and not CLIENT_SECRET.startswith("<<")
