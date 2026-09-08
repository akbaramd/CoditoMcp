using Windows.Data.Xml.Dom;
using Windows.System;
using Windows.UI.Notifications;

namespace Codito.Broker;

internal static class ApprovalToast
{
    internal const string AppId = "Codito.DeviceMcp";

    internal static void ClearStale() =>
        ToastNotificationManager.History.RemoveGroup("approvals", AppId);

    internal static async Task<bool> IsProtocolAvailableAsync()
    {
        // A successful legacy Shell lookup does not prove WinRT toast activation works.
        // Querying support never launches the URI and carries no approval token.
        var support = await Launcher.QueryUriSupportAsync(
            new Uri("codito-approval://decision"), LaunchQuerySupportType.Uri);
        return support == LaunchQuerySupportStatus.Available;
    }

    internal static void Show(ToastSpecification specification)
    {
        if (specification.Xml.Length > 32768 || specification.Tag.Length is < 1 or > 16
            || specification.ExpiresAt <= DateTimeOffset.UtcNow
            || specification.ExpiresAt > DateTimeOffset.UtcNow.AddMinutes(6))
        {
            throw new InvalidDataException("Invalid approval notification bounds");
        }
        var document = new XmlDocument();
        document.LoadXml(specification.Xml);
        var notification = new ToastNotification(document)
        {
            Tag = specification.Tag,
            Group = "approvals",
            ExpirationTime = specification.ExpiresAt,
        };
        var notifier = ToastNotificationManager.CreateToastNotifier(AppId);
        if (notifier.Setting != NotificationSetting.Enabled)
        {
            throw new InvalidOperationException("notification_disabled");
        }
        notifier.Show(notification);
    }
}
