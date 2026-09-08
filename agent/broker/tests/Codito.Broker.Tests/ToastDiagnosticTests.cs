using System.Text.Json;
using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;
using Xunit;

namespace Codito.Broker.Tests;

public sealed class ToastDiagnosticTests
{
    [Theory]
    [InlineData("0123456789abcdef", true)]
    [InlineData("other-app", false)]
    [InlineData("0123456789abcde*", false)]
    [InlineData("0123456789abcdef0", false)]
    [InlineData("", false)]
    public void NotificationRemovalAcceptsOnlyFixedLengthHashedTags(string tag, bool valid)
        => Assert.Equal(valid, ApprovalToast.ValidTag(tag));

    [Theory]
    [InlineData("Busy")]
    [InlineData("AcceptsNotifications")]
    public void AcceptedToastNeverClaimsItsBannerWasVisible(string state)
    {
        using var result = JsonDocument.Parse(JsonSerializer.Serialize(
            ApprovalToast.DescribeSubmission("Enabled", 0, state, false)));
        Assert.True(result.RootElement.GetProperty("ok").GetBoolean());
        Assert.Equal("submitted", result.RootElement.GetProperty("delivery").GetString());
        Assert.Equal(JsonValueKind.Null, result.RootElement.GetProperty("banner_visible").ValueKind);
        Assert.Equal(state, result.RootElement.GetProperty("user_state").GetString());
    }

    [Fact]
    public void HistoryDiagnosticsNeverExposeNotificationBodyOrActivationCredentials()
    {
        var document = new XmlDocument();
        document.LoadXml("<toast launch=\"synthetic-private-token\"><visual>"
            + "<binding template=\"ToastGeneric\"><text>private-request-body</text>"
            + "</binding></visual></toast>");
        // Construct only: this test never displays a notification or requests approval.
        var notification = new ToastNotification(document)
        {
            Tag = "diagnostic-tag",
            Group = "approvals",
        };
        var serialized = JsonSerializer.Serialize(
            ApprovalToast.DescribeHistory("Enabled", 0, "Busy", [notification]));
        using var json = JsonDocument.Parse(serialized);
        Assert.Equal("Busy", json.RootElement.GetProperty("user_state").GetString());
        Assert.Equal(1, json.RootElement.GetProperty("count").GetInt32());
        Assert.Contains("diagnostic-tag", serialized, StringComparison.Ordinal);
        Assert.DoesNotContain("synthetic-private-token", serialized, StringComparison.Ordinal);
        Assert.DoesNotContain("private-request-body", serialized, StringComparison.Ordinal);
    }
}
