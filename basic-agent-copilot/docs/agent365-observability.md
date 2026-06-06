# Adding Agent 365 Observability — a Decision Guide

This document captures the **proven** approach for wiring an agent into Agent 365 Observability (the Microsoft Admin Center → Agents traces view) and — more importantly — the pitfalls that quietly cause spans to be **dropped** before they ever leave the process.

Reference: <https://aka.ms/agent365enable>

---

## 1. Pick the right auth path FIRST

The exporter pipeline is the same in all cases; what changes is **how the per‑agent token is obtained and registered**. Picking the wrong path will produce HTTP 200s that never appear in the portal, or `AADSTS82001` token failures.

| Agent type | Host | Trigger | Auth path | Token shape |
|---|---|---|---|---|
| **Copilot / Teams agent** built with M365 Agents SDK (`Microsoft.Agents.Hosting.AspNetCore`) | Microsoft 365 | A user turn on `msteams` / `msteamschat` | **OBO (agentic user token)** | `AgenticTokenStruct` from `UserAuthorization` |
| **Autonomous / background agent** (timer, queue, webhook, server‑to‑server) | Any host (Functions, Container Apps, AKS, …) | No user in the loop | **S2S (FMI 2‑step exchange)** | Custom `TokenResolver` returning AT for the agent identity |
| Hybrid (interactive + background work in the same process) | Mixed | Both | Both — register per turn | Both |

> ⚠️ **The single biggest trap**: a Copilot / Teams agent is *always* user‑initiated. Even though it feels "server‑side", it is **not** S2S. Wiring it with the S2S path will give you HTTP 200 against `agent365.svc.cloud.microsoft` but the activity will never show up in MAC because the FMI access token doesn't carry the user context the portal joins on.

---

## 2. Required NuGet packages (both paths)

```xml
<PackageReference Include="Microsoft.Agents.A365.Observability.Hosting" Version="1.0.0" />
<PackageReference Include="Microsoft.Agents.A365.Observability.Runtime" Version="1.0.0" />
```

Do **not** also reference `Microsoft.OpenTelemetry.*` standalone packages — the A365 distro brings its own OpenTelemetry chain. Mixing them causes `CS0433` duplicate‑type errors.

---

## 3. The OBO path (Copilot / Teams agents) — what actually works

### 3.1 `Program.cs`

```csharp
// 1. Register the A365 exporter + tracing pipeline
builder.Services.AddAgenticTracingExporter();   // registers IExporterTokenCache<AgenticTokenStruct>
builder.AddA365Tracing();                       // wires the Agent365Exporter

// 2. Give telemetry a real service name (otherwise spans are `unknown_service:...`)
builder.Services
    .AddOpenTelemetry()
    .ConfigureResource(r => r
        .AddService(serviceName: "my-agent", serviceVersion: "1.0.0")
        .AddAttributes(new Dictionary<string, object>
        {
            ["deployment.environment"] = builder.Environment.EnvironmentName,
            ["service.namespace"] = "Microsoft.Agents",
        }));

// 3. Register IChatClient as a singleton wrapped with UseOpenTelemetry HERE,
//    not inside the agent. See §3.4 — this is the single most important rule.
builder.Services.AddSingleton<IChatClient>(sp =>
{
    var cfg  = sp.GetRequiredService<ConfigOptions>();
    var cred = sp.GetRequiredService<DefaultAzureCredential>();
    return new AzureOpenAIClient(new Uri(cfg.Azure.OpenAIEndpoint), cred)
        .GetChatClient(cfg.Azure.OpenAIDeploymentName)
        .AsIChatClient()
        .AsBuilder()
        .UseOpenTelemetry(sourceName: null, configure: c => c.EnableSensitiveData = true)
        .Build();
});
```

### 3.2 Register the OBO token **every turn** in the `AgentApplication`

```csharp
public class WeatherAgentBot : AgentApplication
{
    private readonly IExporterTokenCache<AgenticTokenStruct>? _agentTokenCache;

    public WeatherAgentBot(AgentApplicationOptions options,
                           IExporterTokenCache<AgenticTokenStruct>? agentTokenCache = null) : base(options)
    {
        _agentTokenCache = agentTokenCache;

        //  ⚠ autoSignInHandlers is required — it forces the user OBO token to be
        //  exchanged BEFORE MessageActivityAsync runs, so RegisterObservability
        //  below has a real token (not a placeholder) to cache.
        OnActivity(ActivityTypes.Message,
                   MessageActivityAsync,
                   autoSignInHandlers: [UserAuthorization.DefaultHandlerName],
                   rank: RouteRank.Last);
    }

    private async Task MessageActivityAsync(ITurnContext turnContext, ITurnState state, CancellationToken ct)
    {
        var resolvedAgentId  = turnContext.Activity.GetAgenticInstanceId();   // per-instance Entra agent id
        var resolvedTenantId = turnContext.Activity.Conversation?.TenantId;

        // Register the OBO token in the exporter cache, keyed on (agentId, tenantId).
        _agentTokenCache?.RegisterObservability(
            resolvedAgentId,
            resolvedTenantId,
            new AgenticTokenStruct(UserAuthorization,
                                   turnContext,
                                   UserAuthorization.DefaultHandlerName ?? string.Empty),
            EnvironmentUtils.GetObservabilityAuthenticationScope());

        // Open the outer invoke_agent span with proper baggage so every child span
        // inherits the right (gen_ai.agent.id, gen_ai.tenant.id).
        using var scope = InvokeAgentScope.Start(
            agentId:   resolvedAgentId,
            tenantId:  resolvedTenantId,
            caller:    new CallerDetails(
                          turnContext.Activity.From?.AadObjectId,
                          turnContext.Activity.From?.Name));

        // … run your inner agent here …
    }
}
```

### 3.3 `appsettings.json`

```jsonc
{
  "Agent365Observability": {
    "AgentId":          "<per-instance Entra agent id>",
    "AgentName":        "my-agent",
    "AgentBlueprintId": "<blueprint id>",
    "ClientId":         "<app reg client id>",
    "ClientSecret":     "<rotate me — never commit>"
  },
  "UserAuthorization": {
    "Handlers": {
      "agentic": {
        "Settings": {
          "AgenticUserAuthorization": {
            "ServiceConnection": {
              "ClientSecret": "<…>",
              "Scopes": [ "5a807f24-c9de-44ee-a3a7-329e88a00ffc/.default" ]
            }
          }
        }
      }
    }
  }
}
```

### 3.4 The instrumentation rule that costs you days if you get it wrong

> **Wrap `IChatClient` with `.UseOpenTelemetry()` exactly once — in DI in `Program.cs` — and use `ChatClientAgent` directly inside your agent. Never `.AsAIAgent(...)`. Never `.AsBuilder().UseOpenTelemetry()` again inside the agent.**

Why this matters — concrete failure mode:

- `.AsAIAgent(...)` and inner `.AsBuilder().UseOpenTelemetry()` both emit their **own** `invoke_agent` / `gen_ai.*` spans, stamped with the wrapper's auto‑generated id as `gen_ai.agent.id`.
- That id is *not* the per‑instance Entra agent id you registered with `RegisterObservability`.
- The export batch now contains **two** `(agentId, tenantId)` identities — one with a token, one without.
- The exporter logs `Obtained token for agent <real-id>` immediately followed by `No token obtained. Skipping export for this identity.` and drops the **entire batch** without ever making the HTTP POST.
- You see nothing in MAC. No error. No 4xx. Just silence.

Correct inner agent shape:

```csharp
public class WeatherForecastAgent
{
    private readonly ChatClientAgent _agent;

    public WeatherForecastAgent(IChatClient chatClient)   // <- injected, already OTel-wrapped
    {
        _agent = new ChatClientAgent(
            chatClient,
            AgentInstructions,
            "weather-forecast-agent",
            null);
    }
    // … RunAsync as usual …
}
```

---

## 4. The S2S path (autonomous / background agents)

Only use this when there is **no user in the loop** (timer trigger, queue handler, webhook from another service, scheduled job).

### 4.1 Token exchange shape — FMI two‑step (NOT a single MSAL call)

A single‑call MSAL with `fmi_path` in `extraQueryParameters` yields `AADSTS82001`. The correct shape is two requests:

1. **Step 1** — obtain a client assertion for the blueprint principal (using app's client cert/secret).
2. **Step 2** — present that assertion to the token endpoint with `fmi_path` = the per‑instance agent identity to obtain the agent's AT.

### 4.2 Wiring

```csharp
builder.Services.AddAgenticTracingExporter();
builder.AddA365Tracing();
builder.Services.AddSingleton<TokenResolver>(new MyFmiTokenResolver(
    blueprintId:     "<blueprint>",
    perInstanceId:   "<agent>",
    tenantId:        "<tenant>"));
builder.Services.UseS2SEndpoint();   // switches the exporter to the S2S route
```

Per‑run (e.g. in your timer trigger), open `InvokeAgentScope.Start(...)` with the per‑instance agent id and tenant id; the exporter will pull the AT from your `TokenResolver`.

---

## 5. Diagnosing "nothing in MAC" — a checklist

Run through this in order; the first ✗ is almost always the answer.

| # | Check | Look for |
|---|---|---|
| 1 | Are spans being created at all? | An `invoke_agent` Activity in your debugger / Console exporter |
| 2 | Does the exporter log `Obtained token for agent <id>`? | If no → `RegisterObservability` wasn't called or `autoSignInHandlers` is missing |
| 3 | Does it then log `No token obtained. Skipping export`? | If yes → batch contains a second identity (see §3.4) — you are double‑instrumenting |
| 4 | Is there an `HTTP POST https://agent365.svc.cloud.microsoft/observability/...` line? | If no → batch was dropped before send |
| 5 | Does the POST return 200? | 401/403 → token audience/scope wrong; 400 → blueprint or agent id mismatch |
| 6 | Does the per‑instance agent id in the URL match the one in MAC? | Mismatch → wrong env / wrong tenant / stale config |
| 7 | Has the blueprint the `OtelWrite` permission and `inheritablePermissions`? | Verify in Entra |

---

## 6. Things that look like the problem but aren't

These were tested and ruled out during this work — don't waste time on them again:

- Blueprint missing `OtelWrite` permission (it had it).
- Blueprint missing `inheritablePermissions` (it had it).
- Wrong per‑instance id from `Activity.AgentId` vs `GetAgenticInstanceId()` — use `GetAgenticInstanceId()`.
- "Orphan" outer span without `InvokeAgentScope` (we open one explicitly).
- Duplicate inner span from nested `InferenceScope.Start` (removed — the IChatClient OTel wrapper handles it).
- Mixing the A365 distro with standalone OpenTelemetry packages — don't.

---

## 7. Working reference samples

- **OBO (Copilot/Teams)**: this repo (`basic-agent-copilot`).
- **OBO (Bot Framework)**: `../BAF1-complete` — the canonical reference. When in doubt, diff your code against it.

---

## 8. Final hygiene

- **Rotate any secret committed to `appsettings.json`** before sharing the repo.
- Keep `Agent365Observability:AgentId` in sync with the per‑instance id registered in Entra; a stale id silently routes traces nowhere.
