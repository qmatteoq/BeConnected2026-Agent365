using System.Text.Json;

namespace basic_agent_copilot.Bot.Plugins;

public class AdaptiveCardPlugin
{
    public string GetAdaptiveCardForForecast(string location, WeatherForecast forecast)
    {
        var card = new
        {
            type = "AdaptiveCard",
            version = "1.5",
            body = new object[]
            {
                new
                {
                    type = "TextBlock",
                    size = "Large",
                    weight = "Bolder",
                    text = $"Weather for {location}"
                },
                new
                {
                    type = "TextBlock",
                    spacing = "Small",
                    isSubtle = true,
                    text = forecast.Date,
                    wrap = true
                },
                new
                {
                    type = "FactSet",
                    facts = new object[]
                    {
                        new { title = "Temp (C)", value = forecast.TemperatureC.ToString() },
                        new { title = "Temp (F)", value = forecast.TemperatureF.ToString() }
                    }
                }
            },
            actions = new object[]
            {
                new
                {
                    type = "Action.OpenUrl",
                    title = "More details",
                    url = $"https://www.msn.com/en-us/weather/forecast/in-{location.Replace(" ", "-")}"
                }
            }
        };

        return JsonSerializer.Serialize(card);
    }
}
