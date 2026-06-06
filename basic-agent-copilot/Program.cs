using basic_agent_copilot;
using basic_agent_copilot.Bot.Agents;
using Azure.AI.OpenAI;
using Azure.Identity;
using Microsoft.Agents.A365.Observability.Hosting;
using Microsoft.Agents.A365.Observability.Runtime;
using Microsoft.Agents.Hosting.AspNetCore;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Storage;
using Microsoft.Extensions.AI;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Resources;

var builder = WebApplication.CreateBuilder(args);

builder.Services.AddControllers();
builder.Services.AddHttpClient("WebClient", client => client.Timeout = TimeSpan.FromSeconds(600));
builder.Services.AddHttpContextAccessor();
builder.Logging.AddConsole();

builder.Services.AddOptions<ConfigOptions>().Bind(builder.Configuration);
builder.Services.AddSingleton(sp => sp.GetRequiredService<IOptions<ConfigOptions>>().Value);
builder.Services.AddSingleton<DefaultAzureCredential>();

// Register the WeatherForecastAgent
builder.Services.AddTransient<WeatherForecastAgent>();

// IChatClient registered as a singleton and wrapped with .UseOpenTelemetry()
// here — NOT inside the WeatherForecastAgent. Mirrors the BAF sample so that
// gen_ai.* spans emitted by the chat client inherit the ambient (tenant, agent)
// baggage set in WeatherAgentBot.MessageActivityAsync. If the agent itself
// wrapped the client with .AsBuilder().UseOpenTelemetry(), or used .AsAIAgent()
// (which adds its own OTel instrumentation), spans would be stamped with the
// agent's auto-generated id as gen_ai.agent.id — producing a SECOND identity
// in the export batch with no registered OBO token, and the exporter aborts
// the whole batch with "No token obtained. Skipping export for this identity."
builder.Services.AddSingleton<IChatClient>(sp =>
{
    var config = sp.GetRequiredService<ConfigOptions>();
    var credential = sp.GetRequiredService<DefaultAzureCredential>();

    if (string.IsNullOrWhiteSpace(config.Azure?.OpenAIEndpoint))
        throw new InvalidOperationException("Azure:OpenAIEndpoint is required.");
    if (string.IsNullOrWhiteSpace(config.Azure?.OpenAIDeploymentName))
        throw new InvalidOperationException("Azure:OpenAIDeploymentName is required.");

    return new AzureOpenAIClient(new Uri(config.Azure.OpenAIEndpoint), credential)
        .GetChatClient(config.Azure.OpenAIDeploymentName)
        .AsIChatClient()
        .AsBuilder()
        .UseOpenTelemetry(sourceName: null, configure: cfg => cfg.EnableSensitiveData = true)
        .Build();
});

// Add AspNet token validation
builder.Services.AddBotAspNetAuthentication(builder.Configuration);

// Register IStorage.  For development, MemoryStorage is suitable.
// For production Agents, persisted storage should be used so
// that state survives Agent restarts, and operate correctly
// in a cluster of Agent instances.
builder.Services.AddSingleton<IStorage, MemoryStorage>();

// Add AgentApplicationOptions from config.
builder.AddAgentApplicationOptions();

// Add AgentApplicationOptions.  This will use DI'd services and IConfiguration for construction.
builder.Services.AddTransient<AgentApplicationOptions>();

// Add the bot (which is transient)
builder.AddAgent<basic_agent_copilot.Bot.WeatherAgentBot>();

// === Agent 365 Observability (OBO / agentic-identity path) ===
// This agent is hosted in Microsoft 365 Copilot via the M365 Agents SDK — every
// turn is user-initiated through the msteams channel, so the supported path is
// OBO (NOT autonomous / S2S, which targets non-user-initiated workloads).
//
// AddAgenticTracingExporter() registers IExporterTokenCache<AgenticTokenStruct>
// in DI; WeatherAgentBot calls RegisterObservability(...) per turn with the
// user's agentic OBO token. AddA365Tracing() wires the Agent365Exporter to
// read per-(agent, tenant) tokens from that cache and post spans to the
// /observability/... route on the Observability API.
// See: https://github.com/microsoft/Agent365-devTools/blob/main/docs/agent365-guided-setup/a365-observability-instructions.md
builder.Services.AddAgenticTracingExporter();
builder.AddA365Tracing();

// Service identity for telemetry — without this, spans show up as `unknown_service:...`.
builder.Services
    .AddOpenTelemetry()
    .ConfigureResource(r => r
        .AddService(serviceName: "zava-weather-agent-AI", serviceVersion: "1.0.0")
        .AddAttributes(new Dictionary<string, object>
        {
            ["deployment.environment"] = builder.Environment.EnvironmentName,
            ["service.namespace"] = "Microsoft.Agents",
        }));

var app = builder.Build();

if (app.Environment.IsDevelopment())
{
    app.UseDeveloperExceptionPage();
}
app.UseStaticFiles();

app.UseRouting();

app.UseAuthentication();
app.UseAuthorization();

app.MapPost("/api/messages", async (HttpRequest request, HttpResponse response, IAgentHttpAdapter adapter, IAgent agent, CancellationToken cancellationToken) =>
{
    await adapter.ProcessAsync(request, response, agent, cancellationToken);
});

if (app.Environment.IsDevelopment() || app.Environment.EnvironmentName == "Playground")
{
    app.MapGet("/", () => "Weather Bot");
    app.UseDeveloperExceptionPage();
    app.MapControllers().AllowAnonymous();
}
else
{
    app.MapControllers();
}

app.Run();

