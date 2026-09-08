using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Codito.Broker;

internal static class ToastActivationHost
{
    internal const string ClassId = "0D673CF7-8613-4C9C-8D35-8A3F66BCE1A9";
    private static readonly Guid ActivatorId = new(ClassId);

    internal static bool IsValidActivation(string appId, string argument) =>
        appId == ApprovalToast.AppId && argument.Length is > 0 and <= 2048
        && argument.StartsWith("codito-approval://decision?", StringComparison.Ordinal)
        && !argument.Any(char.IsControl);

    internal static int Run(bool listenUntilParentExit = false)
    {
        // COM owns dispatch; no graphical window, shell invocation or URI association.
        var initialized = CoInitializeEx(IntPtr.Zero, 0);
        Marshal.ThrowExceptionForHR(initialized);
        uint cookie = 0;
        try
        {
            using var completed = new ManualResetEventSlim(false);
            var factory = new ToastClassFactory(new ToastActivator(completed));
            var classId = ActivatorId;
            Marshal.ThrowExceptionForHR(CoRegisterClassObject(ref classId, factory, 4, 1, out cookie));
            if (listenUntilParentExit)
            {
                // The tray owns the pipe. EOF on tray exit/crash ends this listener;
                // no detached background process or windows service is installed.
                Console.WriteLine("ready");
                Console.Out.Flush();
                using var input = Console.OpenStandardInput();
                _ = input.ReadByte();
            }
            else
            {
                completed.Wait(TimeSpan.FromSeconds(30));
            }
            // Give COM the chance to return the successful callback before exiting.
            Thread.Sleep(100);
            GC.KeepAlive(factory);
            return 0;
        }
        finally
        {
            if (cookie != 0)
            {
                _ = CoRevokeClassObject(cookie); // Best-effort shutdown after callback/timeout.
            }
            CoUninitialize();
        }
    }

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int CoInitializeEx(IntPtr reserved, uint flags);

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern void CoUninitialize();

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int CoRegisterClassObject(
        ref Guid classId, [MarshalAs(UnmanagedType.IUnknown)] object factory,
        uint context, uint flags, out uint cookie);

    [DllImport("ole32.dll", ExactSpelling = true)]
    private static extern int CoRevokeClassObject(uint cookie);
}

[ComVisible(true)]
[Guid("53E31837-6600-4A81-9395-75CFFE746F94")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IToastNotificationActivationCallback
{
    [PreserveSig]
    int Activate(
        [MarshalAs(UnmanagedType.LPWStr)] string appUserModelId,
        [MarshalAs(UnmanagedType.LPWStr)] string invokedArgs,
        IntPtr data,
        uint count);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
internal sealed class ToastActivator(ManualResetEventSlim completed)
    : IToastNotificationActivationCallback
{
    public int Activate(string appUserModelId, string invokedArgs, IntPtr data, uint count)
    {
        if (!ToastActivationHost.IsValidActivation(appUserModelId, invokedArgs) || count != 0)
        {
            return unchecked((int)0x80070057); // E_INVALIDARG; never forward foreign activation.
        }
        var tray = Path.GetFullPath(Path.Combine(
            AppContext.BaseDirectory, "..", "tray", "codito-agent-tray.exe"));
        if (!File.Exists(tray))
        {
            return unchecked((int)0x80070002);
        }
        try
        {
            var start = new ProcessStartInfo(tray)
            {
                UseShellExecute = false,
                CreateNoWindow = true,
                WorkingDirectory = Path.GetDirectoryName(tray)!,
            };
            start.ArgumentList.Add("--toast-activation");
            start.ArgumentList.Add(invokedArgs);
            using var process = Process.Start(start);
            if (process is null)
            {
                return unchecked((int)0x80004005);
            }
            // The tray forwards to authenticated daemon IPC, validates one-use tokens,
            // and exits. This host never treats a callback as an authorization itself.
            completed.Set();
            return 0;
        }
        catch (Exception exception) when (
            exception is System.ComponentModel.Win32Exception or InvalidOperationException)
        {
            return unchecked((int)0x80004005);
        }
    }
}

[ComVisible(true)]
[Guid("00000001-0000-0000-C000-000000000046")]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
public interface IToastClassFactory
{
    [PreserveSig]
    int CreateInstance(IntPtr outer, ref Guid interfaceId, out IntPtr instance);

    [PreserveSig]
    int LockServer([MarshalAs(UnmanagedType.Bool)] bool locked);
}

[ComVisible(true)]
[ClassInterface(ClassInterfaceType.None)]
internal sealed class ToastClassFactory(ToastActivator activator) : IToastClassFactory
{
    public int CreateInstance(IntPtr outer, ref Guid interfaceId, out IntPtr instance)
    {
        instance = IntPtr.Zero;
        if (outer != IntPtr.Zero)
        {
            return unchecked((int)0x80040110); // CLASS_E_NOAGGREGATION
        }
        var unknown = Marshal.GetIUnknownForObject(activator);
        try
        {
            return Marshal.QueryInterface(unknown, ref interfaceId, out instance);
        }
        finally
        {
            Marshal.Release(unknown);
        }
    }

    public int LockServer(bool locked) => 0;
}
