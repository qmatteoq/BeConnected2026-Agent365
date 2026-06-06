using Microsoft.Agents.AI;
using Microsoft.Extensions.AI;
using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace basic_agent_copilot.Bot.Agents;

public class WeatherForecastAgent
{
    private readonly ChatClientAgent _agent;
    private readonly ConcurrentDictionary<string, AgentSession> _sessions = new();

    private const string AgentInstructions = """
        You are a friendly assistant that helps people find a weather forecast for a given time and place.
        You may ask follow-up questions until you have enough information to answer.

        For every response, return JSON only with this schema:
        {
            "contentType": "Text or AdaptiveCard",
            "content": "The response content. If contentType is AdaptiveCard, this must be a valid Adaptive Card v1.5 JSON string."
        }

        When you provide an adaptive card, include an Action.OpenUrl button titled "More details"
        that links to: https://www.msn.com/en-us/weather/forecast/in-{location}
        """;

    // The IChatClient is registered as a DI singleton in Program.cs and is already wrapped with
    // .UseOpenTelemetry() THERE \u2014 NOT here. Wrapping it again on this side would emit a second
    // chain of invoke_agent / gen_ai spans whose gen_ai.agent.id is the ChatClientAgent's
    // auto-generated id, producing a second (agent, tenant) identity in the export batch that
    // the OBO token cache hasn't registered. The Agent365 exporter would then log
    // "No token obtained. Skipping export for this identity." and drop the entire batch.
    //
    // Mirrors the BAF sample (D:\src\samples\agent365\BAF1-complete\src\Agent\ZavaInsuranceAgent.cs).
    public WeatherForecastAgent(IChatClient chatClient)
    {
        ArgumentNullException.ThrowIfNull(chatClient);

        // Positional args match the BAF sample exactly (Microsoft.Agents.AI overload).
        _agent = new ChatClientAgent(
            chatClient,
            AgentInstructions,
            "weather-forecast-agent",
            null);
    }

    public async Task<WeatherForecastAgentResponse> InvokeAgentAsync(
        string input,
        string conversationId,
        string? resolvedAgentId,
        string? resolvedTenantId,
        CancellationToken cancellationToken)
    {
        // Identity parameters are kept on the signature for caller stability but unused here.
        // Spans emitted by the underlying IChatClient inherit the ambient (tenant, agent)
        // baggage set by WeatherAgentBot for the duration of the turn.
        _ = resolvedAgentId;
        _ = resolvedTenantId;

        AgentSession session = _sessions.GetValueOrDefault(conversationId);
        if (session is null)
        {
            session = await _agent.CreateSessionAsync(cancellationToken: cancellationToken);
            _sessions[conversationId] = session;
        }

        AgentResponse response = await _agent.RunAsync(input, session, cancellationToken: cancellationToken);
        string responseText = response.Text ?? string.Empty;

        try
        {
            JsonNode jsonNode = JsonNode.Parse(responseText);
            string content = jsonNode?["content"]?.ToString() ?? responseText;
            string contentType = jsonNode?["contentType"]?.ToString() ?? "Text";

            return new WeatherForecastAgentResponse
            {
                Content = content,
                ContentType = Enum.Parse<WeatherForecastAgentResponseContentType>(contentType, true)
            };
        }
        catch
        {
            return new WeatherForecastAgentResponse
            {
                Content = responseText,
                ContentType = WeatherForecastAgentResponseContentType.Text
            };
        }
    }
}
