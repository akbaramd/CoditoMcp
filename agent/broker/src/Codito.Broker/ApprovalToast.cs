using System.Runtime.InteropServices;
using Windows.Data.Xml.Dom;
using Windows.System;
using Windows.UI.Notifications;

namespace Codito.Broker;

internal static class ApprovalToast
{
    internal const string AppId = "Codito.DeviceMcp";

    internal static void ClearStale() =>
        ToastNotificationManager.History.RemoveGroup("approvals", AppId);

    internal static bool ValidTag(string tag) => tag.Length == 16
        && tag.All(character => character is >= '0' and <= '9' or >= 'a' and <= 'f');

    internal static void Remove(string tag)
    {
        if (!ValidTag(tag))
            throw new InvalidDataException("Invalid notification tag");
        // Only our own fixed application/group can be addressed, never other apps.
        ToastNotificationManager.History.Remove(tag, "approvals", AppId);
    }

    internal static object GetHistoryDiagnostics()
    {
        var notifier = ToastNotificationManager.CreateToastNotifier(AppId);
        var notifications = ToastNotificationManager.History.GetHistory(AppId);
        var stateResult = SHQueryUserNotificationState(out var userState);
        return DescribeHistory(notifier.Setting.ToString(), stateResult, userState.ToString(), notifications);
    }

    internal static object DescribeHistory(
        string setting, int stateResult, string userState, IEnumerable<ToastNotification> notifications)
    {
        // Never return notification XML: it contains one-use approval credentials.
        var entries = notifications.Select(notification => new
        {
            tag = notification.Tag,
            group = notification.Group,
            expires_at = notification.ExpirationTime,
            suppress_popup = notification.SuppressPopup,
            priority = notification.Priority.ToString(),
        }).ToArray();
        return new
        {
            ok = true,
            app_id = AppId,
            setting,
            user_state_hresult = stateResult,
            user_state = userState,
            count = entries.Length,
            notifications = entries,
        };
    }

    private enum UserNotificationState
    {
        NotPresent = 1,
        Busy = 2,
        RunningDirect3DFullScreen = 3,
        PresentationMode = 4,
        AcceptsNotifications = 5,
        QuietTime = 6,
        RunningWindowsStoreApp = 7,
    }

    [DllImport("shell32.dll", ExactSpelling = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern int SHQueryUserNotificationState(out UserNotificationState state);

    internal static async Task<bool> IsProtocolAvailableAsync()
    {
        // A successful legacy Shell lookup does not prove WinRT toast activation works.
        // Querying support never launches the URI and carries no approval token.
        var support = await Launcher.QueryUriSupportAsync(
            new Uri("codito-approval://decision"), LaunchQuerySupportType.Uri);
        return support == LaunchQuerySupportStatus.Available;
    }

    internal static async Task<object> ShowAsync(ToastSpecification specification)
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
        var stateResult = SHQueryUserNotificationState(out var userState);
        var failed = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        void OnFailed(ToastNotification _, ToastFailedEventArgs args) =>
            failed.TrySetResult(args.ErrorCode.HResult);
        notification.Failed += OnFailed;
        try
        {
            notifier.Show(notification);
            // Show() accepts a submission, not proof of a visible banner. Observe
            // immediate platform rejection without leaking XML or activation data.
            if (await Task.WhenAny(failed.Task, Task.Delay(250)) == failed.Task)
            {
                throw new InvalidOperationException($"notification_failed:0x{await failed.Task:X8}");
            }
            return DescribeSubmission(
                notifier.Setting.ToString(), stateResult, userState.ToString(), notification.SuppressPopup);
        }
        finally
        {
            notification.Failed -= OnFailed;
        }
    }

    internal static object DescribeSubmission(
        string setting, int stateResult, string userState, bool suppressPopup) => new
    {
        ok = true,
        delivery = "submitted",
        setting,
        user_state_hresult = stateResult,
        user_state = userState,
        suppress_popup = suppressPopup,
        banner_visible = (bool?)null,
    };
}
