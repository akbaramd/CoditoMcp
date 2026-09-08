using Windows.Data.Xml.Dom;
using Windows.UI.Notifications;

namespace Codito.Broker;

internal static class ApprovalToast
{
    internal const string AppId = "Codito.DeviceMcp";

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
        ToastNotificationManager.CreateToastNotifier(AppId).Show(notification);
    }
}
