using System.Diagnostics;
using System.Runtime.InteropServices;

namespace Codito.Broker;

internal sealed class JobObject : IDisposable
{
    private readonly SafeJobHandle handle;

    private JobObject(SafeJobHandle handle) => this.handle = handle;

    internal static JobObject CreateKillOnClose()
    {
        var handle = NativeMethods.CreateJobObject(0, null);
        if (handle.IsInvalid)
        {
            throw NativeMethods.LastError("CreateJobObjectW");
        }

        var information = new NativeMethods.JobObjectExtendedLimitInformation();
        information.BasicLimitInformation.LimitFlags = NativeMethods.JobObjectLimitKillOnJobClose;
        var size = (uint)Marshal.SizeOf<NativeMethods.JobObjectExtendedLimitInformation>();
        if (!NativeMethods.SetInformationJobObject(
                handle, NativeMethods.JobObjectExtendedLimitInformationClass, ref information, size))
        {
            handle.Dispose();
            throw NativeMethods.LastError("SetInformationJobObject");
        }
        return new JobObject(handle);
    }

    internal void Assign(Process process)
    {
        if (!NativeMethods.AssignProcessToJobObject(handle, process.Handle))
        {
            throw NativeMethods.LastError("AssignProcessToJobObject");
        }
    }

    internal bool CanQuery()
    {
        var size = (uint)Marshal.SizeOf<NativeMethods.BasicAccountingInformation>();
        return NativeMethods.QueryInformationJobObject(
            handle,
            NativeMethods.JobObjectBasicAccountingInformationClass,
            out _,
            size,
            out _);
    }

    public void Dispose() => handle.Dispose();
}
