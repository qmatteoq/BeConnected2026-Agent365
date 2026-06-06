# Basic Agent (Python / LangChain)

A Python port of the [`basic-agent`](../basic-agent) sample. Same agent, same
chat UI, same configuration — implemented with **LangChain + LangGraph** on
top of **Azure OpenAI**, served by **FastAPI**.

> **Note** — Agent 365 observability / instrumentation is intentionally left
> out of this sample and will be added later.

## Stack

| Component | Package |
|-----------|---------|
| Agent framework | `langchain`, `langgraph` (ReAct agent) |
| LLM client | `langchain-openai` — `AzureChatOpenAI` |
| MCP integration | `langchain-mcp-adapters` |
| Authentication | `azure-identity` — `DefaultAzureCredential` |
| Web server | `FastAPI` + `uvicorn` |

## Prerequisites

- Python 3.10+
- An Azure OpenAI resource with a **gpt-4.1** deployment
- The Azure CLI (`az login`) — `DefaultAzureCredential` will use your current CLI session

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate         # PowerShell on Windows
pip install -r requirements.txt
```

## Configuration

Edit `.env` (already pre-filled):

```
AZURE_OPENAI_ENDPOINT=https://agentcon2025-resource.openai.azure.com/
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4.1
AZURE_OPENAI_API_VERSION=2024-10-21
```

## Run locally

```bash
python main.py
```

Then open your browser at **http://localhost:5000**.

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET`  | `/` | Chat web UI |
| `POST` | `/api/sessions` | Create a new conversation session |
| `POST` | `/api/chat` | Send a message; starts a new session if `sessionId` is omitted |

### Chat request body

```json
{ "message": "Hello!", "sessionId": null }
```

### Chat response body

```json
{
  "reply": "Hi there! How can I help you?",
  "sessionId": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
  "userId": "...",
  "userName": "...",
  "userEmail": "..."
}
```

Pass the returned `sessionId` back on the next request to maintain
conversation context (LangGraph `MemorySaver` keyed by `thread_id`).

## Project structure

```
basic-agent-python/
├── main.py             # FastAPI app + LangGraph ReAct agent
├── user_pool.py        # Simulated user pool loader
├── requirements.txt
├── users.csv           # Tenant users for simulated caller identity
├── wwwroot/
│   └── index.html      # Chat web UI (same as the .NET sample)
├── .env                # Local environment variables (not committed)
└── README.md
```

## Authentication notes

`DefaultAzureCredential` tries the following in order:
1. Environment variables (`AZURE_CLIENT_ID`, etc.)
2. Azure CLI (`az login`)
3. Managed Identity (when deployed to Azure)

For local development, running `az login` is the easiest way to authenticate.
