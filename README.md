# BeConnected 2026 — Onboarding Agents into Agent 365

Sample code for the **BeConnected 2026** conference session on how to build and
onboard AI agents into **Microsoft Agent 365**.

This repository contains **two implementations of the same idea** — a simple
conversational agent — built on two different stacks, so you can see what Agent
365 onboarding looks like regardless of the technology you use:

| Project | Stack | Host | Agent 365 focus |
|---------|-------|------|-----------------|
| [`basic-agent-copilot`](./basic-agent-copilot) | .NET 10 / C# — **Microsoft 365 Agents SDK** | Microsoft 365 Copilot / Teams | Full onboarding **with Agent 365 Observability** (OBO / agentic-identity path) |
| [`basic-agent-python`](./basic-agent-python) | Python — **LangChain + LangGraph + FastAPI** | Standalone web app | Same agent experience on a non-Microsoft framework |

Both agents call **MCP (Model Context Protocol)** tools and chat through
**Azure OpenAI**, authenticating with `DefaultAzureCredential` / managed
identity rather than API keys.

---

## Why two samples?

The goal of the session is to show that **onboarding an agent into Agent 365 is
not tied to a single SDK or language**. The two projects deliberately mirror
each other so the audience can compare:

- **`basic-agent-copilot`** — the "first-party" path. Built with the Microsoft
  365 Agents SDK, hosted inside Microsoft 365 Copilot/Teams, and fully wired
  into **Agent 365 Observability** so every user turn shows up in the Microsoft
  Admin Center → Agents traces view. This is the reference for the recommended
  identity and telemetry setup.
- **`basic-agent-python`** — the "bring your own framework" path. The same agent
  behaviour implemented with LangChain + LangGraph and served by FastAPI,
  showing how a non-.NET agent is structured before/while being onboarded.

---

## `basic-agent-copilot` (.NET / Microsoft 365 Agents SDK)

A weather-style agent hosted in Microsoft 365 Copilot and Teams, built with the
M365 Agents SDK. It demonstrates the **end-to-end Agent 365 onboarding**,
including:

- **Agent identity** via an Agent 365 blueprint and agentic identity
  (`a365.config.json`).
- **Agent 365 Observability** using the **OBO (on-behalf-of) agentic-identity
  path** — the correct path for user-initiated Copilot/Teams agents.
- Azure OpenAI chat client instrumented with OpenTelemetry so `gen_ai.*` spans
  are exported to Agent 365.

See [`basic-agent-copilot/docs/agent365-observability.md`](./basic-agent-copilot/docs/agent365-observability.md)
for the decision guide on wiring observability correctly (and the common
pitfalls that silently drop spans).

### Run it

```powershell
cd basic-agent-copilot
dotnet run
```

> Requires the .NET 10 SDK, an Azure OpenAI deployment, and `az login` for
> `DefaultAzureCredential`. Provisioning details live in `a365.config.json`.

---

## `basic-agent-python` (LangChain + LangGraph)

A Python port of the same agent: a FastAPI web app with a chat UI that uses a
**LangGraph ReAct agent** on top of **Azure OpenAI**, calling MCP tools.
Conversation state is kept per session with LangGraph's `MemorySaver`.

### Run it

```powershell
cd basic-agent-python
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```

Then open <http://localhost:5000>.

See [`basic-agent-python/README.md`](./basic-agent-python/README.md) for full
details, API reference, and configuration.

---

## Prerequisites (both projects)

- An **Azure OpenAI** resource with a `gpt-4.1` deployment
- **Azure CLI** — run `az login` so `DefaultAzureCredential` can authenticate
- .NET 10 SDK (for `basic-agent-copilot`)
- Python 3.10+ (for `basic-agent-python`)

---

## Repository layout

```
BeConnected2026-Agent365/
├── basic-agent-copilot/   # .NET / M365 Agents SDK agent + Agent 365 Observability
├── basic-agent-python/    # Python / LangChain + LangGraph agent
├── .gitignore
└── README.md
```

---

## A note on secrets

Local environment files (`.env`), the .NET app settings
(`appsettings.json`), and Agent 365 config (`a365.config.json` and
`a365.generated.config.json`) contain tenant-specific IDs and secrets, so they
are excluded via [`.gitignore`](./.gitignore). For the .NET sample, copy
[`basic-agent-copilot/appsettings.sample.json`](./basic-agent-copilot/appsettings.sample.json)
to `appsettings.json` and fill in your own values. The Agent 365 config is
tenant-specific too — to run these samples you must register the agent on
**your own tenant** using the Agent 365 CLI, which generates those files for
you. Do not commit credentials or tenant secrets; both samples authenticate via
managed identity / `az login` where possible.

---

_Built for the BeConnected 2026 conference._
