namespace basic_agent_copilot
{
    public class ConfigOptions
    {
        public AzureConfigOptions Azure { get; set; }
    }

    /// <summary>
    /// Options for Azure OpenAI and Azure Content Safety
    /// </summary>
    public class AzureConfigOptions
    {
        public string OpenAIEndpoint { get; set; }
        public string OpenAIDeploymentName { get; set; }
    }
}