using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Codito.Broker;

internal static class CapabilityProbe
{
    internal static ProbeResponse Probe()
    {
        var job = ProbeJobObject();
        var appContainer = ProbeAppContainerApi();

        // API presence is not a security proof. Until the installer creates the
        // profile, grants only the registered root, and the end-to-end self-test
        // verifies network denial and reparse resistance, isolation stays false.
        var capabilities = new CapabilitySet(
            JobObject: job,
            AppContainerApi: appContainer,
            FilesystemAcl: false,
            NetworkDenial: false,
            ChildContainment: job,
            IsolationProven: false);
        return new ProbeResponse(
            1,
            capabilities,
            "AppContainer ACL and network-denial self-tests are not implemented in this broker build");
    }

    private static bool ProbeJobObject()
    {
        try
        {
            using var job = JobObject.CreateKillOnClose();
            return job.CanQuery();
        }
        catch
        {
            return false;
        }
    }

    private static bool ProbeAppContainerApi()
    {
        if (!NativeLibrary.TryLoad("userenv.dll", out var library))
        {
            return false;
        }
        try
        {
            return NativeLibrary.TryGetExport(library, "CreateAppContainerProfile", out _)
                && NativeLibrary.TryGetExport(library, "DeriveAppContainerSidFromAppContainerName", out _);
        }
        finally
        {
            NativeLibrary.Free(library);
        }
    }
}

