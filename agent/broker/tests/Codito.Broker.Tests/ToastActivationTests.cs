using System.Runtime.InteropServices;
using Xunit;

namespace Codito.Broker.Tests;

public sealed class ToastActivationTests
{
    [Theory]
    [InlineData("foreign.app", "codito-approval://decision?request_id=diagnostic")]
    [InlineData("Codito.DeviceMcp", "https://example.com/")]
    [InlineData("Codito.DeviceMcp", "codito-approval://decision?x=\n")]
    public void ForeignOrMalformedActivationIsNeverForwarded(string appId, string argument)
    {
        Assert.False(ToastActivationHost.IsValidActivation(appId, argument));
        using var completed = new ManualResetEventSlim(false);
        var activator = new ToastActivator(completed);
        Assert.NotEqual(0, activator.Activate(appId, argument, IntPtr.Zero, 0));
        Assert.False(completed.IsSet);
    }

    [Fact]
    public void ClassFactoryExposesOnlyRequiredComInterfaceWithoutAggregation()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }
        using var completed = new ManualResetEventSlim(false);
        var factory = new ToastClassFactory(new ToastActivator(completed));
        var interfaceId = typeof(IToastNotificationActivationCallback).GUID;
        Assert.NotEqual(0, factory.CreateInstance(new IntPtr(1), ref interfaceId, out var rejected));
        Assert.Equal(IntPtr.Zero, rejected);
        Assert.Equal(0, factory.CreateInstance(IntPtr.Zero, ref interfaceId, out var callback));
        Assert.NotEqual(IntPtr.Zero, callback);
        Marshal.Release(callback);
        var unrelated = Guid.NewGuid();
        Assert.NotEqual(0, factory.CreateInstance(IntPtr.Zero, ref unrelated, out rejected));
        Assert.Equal(IntPtr.Zero, rejected);
    }
}
