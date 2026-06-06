using basic_agent_copilot.Bot.Agents;
using Microsoft.Agents.A365.Observability.Hosting.Caching;
using Microsoft.Agents.A365.Observability.Runtime.Common;
using Microsoft.Agents.A365.Observability.Runtime.Tracing.Contracts;
using Microsoft.Agents.A365.Observability.Runtime.Tracing.Scopes;
using Microsoft.Agents.Builder;
using Microsoft.Agents.Builder.App;
using Microsoft.Agents.Builder.State;
using Microsoft.Agents.Core.Models;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.Logging;


namespace basic_agent_copilot.Bot;

public class WeatherAgentBot : AgentApplication
{
    private readonly WeatherForecastAgent _weatherAgent;
    private readonly ILogger<WeatherAgentBot> _logger;
    private readonly IConfiguration _configuration;
    // OBO path: injected by Microsoft.Agents.A365.Observability.Hosting via
    // AddAgenticTracingExporter() in Program.cs. May be null if the host hasn't
    // wired observability yet (e.g., during early bootstrap or in tests).
    private readonly IExporterTokenCache<AgenticTokenStruct>? _agentTokenCache;

    public WeatherAgentBot(
        AgentApplicationOptions options,
        WeatherForecastAgent weatherAgent,
        ILogger<WeatherAgentBot> logger,
        IConfiguration configuration,
        IExporterTokenCache<AgenticTokenStruct>? agentTokenCache = null)
        : base(options)
    {
        _weatherAgent = weatherAgent ?? throw new ArgumentNullException(nameof(weatherAgent));
        _logger = logger ?? throw new ArgumentNullException(nameof(logger));
        _configuration = configuration ?? throw new ArgumentNullException(nameof(configuration));
        _agentTokenCache = agentTokenCache;

        OnConversationUpdate(ConversationUpdateEvents.MembersAdded, WelcomeMessageAsync);
        OnActivity(ActivityTypes.Message, MessageActivityAsync, autoSignInHandlers: [UserAuthorization.DefaultHandlerName], rank: RouteRank.Last);
    }

    protected async Task MessageActivityAsync(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken)
    {
        _ = turnState;

        // A365 auth mode: OBO (agentic identity on behalf of the signed-in user).
        // This is the supported path for Copilot/Teams-hosted M365 Agents SDK agents.
        // The agent calls _agentTokenCache.RegisterObservability(...) once per turn
        // with the user's agentic OBO token; the Agent365Exporter reads from that
        // cache to authenticate batches to the /observability/... route.
        //
        // Resolve the per-instance agent identity from the inbound activity.
        // GetAgenticInstanceId() returns the per-instance agent id that Defender /
        // M365 admin center index by; Recipient.AgenticAppId returns the blueprint id,
        // which would mismatch gen_ai.agent.id.
        string? resolvedAgentId = turnContext.Activity.IsAgenticRequest()
            ? turnContext.Activity.GetAgenticInstanceId()
            : null;
        string? resolvedTenantId = turnContext.Activity.Conversation?.TenantId
                                ?? turnContext.Activity.Recipient?.TenantId;

        _logger.LogInformation(
            "A365 turn diagnostic: channel={Channel}, isAgentic={IsAgentic}, agenticInstanceId={AgentId}, conversationTenant={ConvTenant}, recipientTenant={RecipTenant}, activityType={ActivityType}",
            turnContext.Activity?.ChannelId,
            turnContext.Activity?.IsAgenticRequest() ?? false,
            resolvedAgentId ?? "(null)",
            turnContext.Activity?.Conversation?.TenantId ?? "(null)",
            turnContext.Activity?.Recipient?.TenantId ?? "(null)",
            turnContext.Activity?.Type ?? "(null)");

        var hasObservabilityIdentity =
            !string.IsNullOrEmpty(resolvedAgentId) &&
            !string.IsNullOrEmpty(resolvedTenantId);

        // A365 Observability — build baggage MANUALLY with the resolved per-instance identity.
        // Do NOT use .FromTurnContext() — it would pull the blueprint id from Recipient.AgenticAppId.
        using IDisposable? baggageScope = hasObservabilityIdentity
            ? new BaggageBuilder()
                .TenantId(resolvedTenantId!)
                .AgentId(resolvedAgentId!)
                .Build()
            : null;

        // OBO: register the agentic token for this (agent, tenant) so the exporter
        // can authenticate the next batch. Same pattern as the BAF sample.
        if (hasObservabilityIdentity && _agentTokenCache != null)
        {
            try
            {
                _agentTokenCache.RegisterObservability(
                    resolvedAgentId!,
                    resolvedTenantId!,
                    new AgenticTokenStruct(
                        userAuthorization: UserAuthorization,
                        turnContext:       turnContext,
                        authHandlerName:   UserAuthorization.DefaultHandlerName ?? string.Empty),
                    EnvironmentUtils.GetObservabilityAuthenticationScope());
            }
            catch (Exception ex)
            {
                _logger.LogWarning(ex, "Failed to register A365 observability token.");
            }
        }
        else if (!hasObservabilityIdentity)
        {
            _logger.LogDebug(
                "A365 observability identity not available for this turn — telemetry will not be exported (expected for Playground / non-agentic channels).");
        }

        // A365 Observability — root scope for this agent turn (required for store validation + MAC portal visibility)
        InvokeAgentScope? invokeScope = null;
        if (hasObservabilityIdentity)
        {
            var obs = _configuration.GetSection("Agent365Observability");
            var agentDetails = new AgentDetails(
                agentId:          resolvedAgentId!,
                agentName:        obs["AgentName"] ?? "weather-agent",
                agentDescription: obs["AgentDescription"] ?? string.Empty,
                agentBlueprintId: obs["AgentBlueprintId"] ?? string.Empty,
                tenantId:         resolvedTenantId!);

            // OBO: CallerDetails describes the human user who triggered the turn.
            // (For autonomous AI Teammates this would be the Sponsor blueprint instead.)
            var from = turnContext.Activity?.From;
            var callerDetails = new CallerDetails(
                userDetails: new UserDetails(
                    userId:    from?.AadObjectId ?? from?.Id ?? "unknown",
                    userName:  from?.Name ?? "unknown",
                    userEmail: string.Empty));

            var requestText = turnContext.Activity?.Text ?? string.Empty;
            var scopeRequest = new Request(
                content:        requestText,
                sessionId:      turnContext.Activity?.Conversation?.Id ?? string.Empty,
                channel:        new Channel(turnContext.Activity?.ChannelId ?? "msteams"),
                conversationId: turnContext.Activity?.Conversation?.Id ?? string.Empty);

            var blueprintForUri = obs["AgentBlueprintId"];
            var endpointUri = !string.IsNullOrEmpty(blueprintForUri)
                ? new Uri($"https://{blueprintForUri}.agent.invalid/")
                : new Uri("https://agent.invalid/");

            invokeScope = InvokeAgentScope.Start(
                request:       scopeRequest,
                scopeDetails:  new InvokeAgentScopeDetails(endpoint: endpointUri),
                agentDetails:  agentDetails,
                callerDetails: callerDetails);

            invokeScope.RecordInputMessages(new[] { requestText });
        }

        try
        {
            // Start a Streaming Process 
            await turnContext.StreamingResponse.QueueInformativeUpdateAsync("Working on a response for you");

            string conversationId = turnContext.Activity.Conversation?.Id
                ?? turnContext.Activity.From?.Id
                ?? Guid.NewGuid().ToString();

            // Invoke the WeatherForecastAgent to process the message. Pass the resolved identity
            // so the nested InferenceScope is created with the same per-instance AgentDetails.
            WeatherForecastAgentResponse forecastResponse = await _weatherAgent.InvokeAgentAsync(
                turnContext.Activity.Text,
                conversationId,
                resolvedAgentId,
                resolvedTenantId,
                cancellationToken);
            if (forecastResponse == null)
            {
                turnContext.StreamingResponse.QueueTextChunk("Sorry, I couldn't get the weather forecast at the moment.");
                await turnContext.StreamingResponse.EndStreamAsync(cancellationToken);
                return;
            }

            // Create a response message based on the response content type from the WeatherForecastAgent
            // Send the response message back to the user. 
            switch (forecastResponse.ContentType)
            {
                case WeatherForecastAgentResponseContentType.Text:
                    turnContext.StreamingResponse.QueueTextChunk(forecastResponse.Content);
                    break;
                case WeatherForecastAgentResponseContentType.AdaptiveCard:
                    turnContext.StreamingResponse.FinalMessage = MessageFactory.Attachment(new Attachment()
                    {
                        ContentType = "application/vnd.microsoft.card.adaptive",
                        Content = forecastResponse.Content,
                    });
                    break;
                default:
                    break;
            }
            await turnContext.StreamingResponse.EndStreamAsync(cancellationToken); // End the streaming response

            // A365 Observability — record final output for the InvokeAgent span
            invokeScope?.RecordOutputMessages(new[] { forecastResponse.Content ?? string.Empty });
        }
        finally
        {
            invokeScope?.Dispose();
        }
    }

    protected async Task WelcomeMessageAsync(ITurnContext turnContext, ITurnState turnState, CancellationToken cancellationToken)
    {
        foreach (ChannelAccount member in turnContext.Activity.MembersAdded)
        {
            if (member.Id != turnContext.Activity.Recipient.Id)
            {
                await turnContext.SendActivityAsync(MessageFactory.Text("Hello and Welcome! I'm here to help with all your weather forecast needs!"), cancellationToken);
            }
        }
    }
}