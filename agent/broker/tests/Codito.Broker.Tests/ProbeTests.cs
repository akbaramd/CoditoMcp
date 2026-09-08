using Xunit;

namespace Codito.Broker.Tests;

public sealed class ProbeTests
{
    [Fact]
    public void ProbeFailsClosedUntilEveryIsolationPropertyIsVerified()
    {
        var response = CapabilityProbe.Probe();
        Assert.False(response.Capabilities.IsolationProven);
        Assert.False(response.Capabilities.FilesystemAcl);
        Assert.False(response.Capabilities.NetworkDenial);
        Assert.NotNull(response.Reason);
    }

    [Fact]
    public void NativeRunnerRejectsWorkingDirectoryOutsideRoot()
    {
        var root = Path.Combine(Path.GetTempPath(), "codito-root");
        Directory.CreateDirectory(root);
        var specification = new RunSpecification(
            root,
            "fingerprint",
            Environment.SystemDirectory,
            new CommandSpecification("exec", "cmd.exe", [], null, null),
            [],
            30,
            1024,
            "nonce");
        Assert.Throws<InvalidDataException>(() => NativeRunner.ValidateSpecification(specification));
    }

    [Fact]
    public void StructuredExecRejectsWindowsGuiSubsystem()
    {
        var root = Path.Combine(Path.GetTempPath(), "codito-root-gui");
        Directory.CreateDirectory(root);
        var environment = new Dictionary<string, string>
        {
            ["PATH"] = Environment.SystemDirectory,
            ["PATHEXT"] = ".EXE",
        };
        var specification = new RunSpecification(
            root,
            "fingerprint",
            root,
            new CommandSpecification("exec", "notepad.exe", [], null, null),
            environment,
            30,
            1024,
            "nonce");
        Assert.Equal(
            PeSubsystem.WindowsGui,
            PortableExecutable.ReadSubsystem(Path.Combine(Environment.SystemDirectory, "notepad.exe")));
        Assert.Throws<InvalidDataException>(
            () => NativeRunner.ResolveConsoleExecutable(specification, "notepad.exe"));
        Assert.EndsWith(
            "cmd.EXE",
            NativeRunner.ResolveConsoleExecutable(specification, "cmd"),
            StringComparison.OrdinalIgnoreCase);
    }
}
