[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Diagnostic only: a foreign app ID MUST be rejected before forwarding to the tray.
# Never use a pending approval ID/token or capture the desktop in this probe.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class CoditoComProbe
{
    [ComImport, Guid("53E31837-6600-4A81-9395-75CFFE746F94")]
    [InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    public interface IActivation
    {
        [PreserveSig]
        int Activate([MarshalAs(UnmanagedType.LPWStr)] string appId,
            [MarshalAs(UnmanagedType.LPWStr)] string args, IntPtr data, uint count);
    }

    [DllImport("ole32.dll", ExactSpelling=true)]
    private static extern int CoCreateInstance(ref Guid clsid, IntPtr outer,
        uint context, ref Guid iid, out IntPtr instance);

    public static bool Check()
    {
        var clsid = new Guid("0D673CF7-8613-4C9C-8D35-8A3F66BCE1A9");
        var iid = typeof(IActivation).GUID;
        IntPtr instance;
        Marshal.ThrowExceptionForHR(CoCreateInstance(ref clsid, IntPtr.Zero, 4, ref iid, out instance));
        object callback = null;
        try
        {
            callback = Marshal.GetObjectForIUnknown(instance);
            int result = ((IActivation)callback).Activate("codito-diagnostic.invalid-app",
                "codito-approval://decision?request_id=diagnostic", IntPtr.Zero, 0);
            return result == unchecked((int)0x80070057);
        }
        finally
        {
            if (callback != null) Marshal.ReleaseComObject(callback);
            Marshal.Release(instance);
        }
    }
}
'@
if (-not [CoditoComProbe]::Check()) {
    throw 'Codito COM activation did not reject the harmless foreign-app probe.'
}
Write-Output 'Codito COM activation works; foreign app probe rejected; no approval granted.'
