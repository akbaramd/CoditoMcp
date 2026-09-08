using System.Diagnostics;

namespace Codito.Broker;

internal static class NativeRunner
{
    internal static async Task<int> RunAsync(RunSpecification specification)
    {
        ValidateSpecification(specification);
        var start = CreateStartInfo(specification);

        // Assign the broker itself before creating the target. Descendants inherit
        // the non-breakaway, kill-on-close job without an assign-after-start race.
        // Intentionally do not dispose this handle: process teardown closes it and
        // terminates any surviving descendants.
        var job = JobObject.CreateKillOnClose();
        job.Assign(Process.GetCurrentProcess());

        using var process = new Process { StartInfo = start, EnableRaisingEvents = true };
        if (!process.Start())
        {
            throw new InvalidOperationException("Process start returned false");
        }

        var stdout = process.StandardOutput.BaseStream.CopyToAsync(Console.OpenStandardOutput());
        var stderr = process.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError());
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(specification.TimeoutSeconds));
        try
        {
            await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync().ConfigureAwait(false);
            return 124;
        }
        await Task.WhenAll(stdout, stderr).ConfigureAwait(false);
        GC.KeepAlive(job);
        return process.ExitCode;
    }

    private static ProcessStartInfo CreateStartInfo(RunSpecification specification)
    {
        var command = specification.Command;
        ProcessStartInfo start;
        if (command.Kind == "exec" && !string.IsNullOrWhiteSpace(command.Executable))
        {
            var executable = ResolveConsoleExecutable(specification, command.Executable);
            start = new ProcessStartInfo(executable);
            foreach (var argument in command.Arguments ?? [])
            {
                start.ArgumentList.Add(argument);
            }
        }
        else if (command.Kind == "script" && command.Shell == "powershell"
                 && command.Script is not null)
        {
            var executable = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.Windows),
                "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
            start = new ProcessStartInfo(executable);
            start.ArgumentList.Add("-NoLogo");
            start.ArgumentList.Add("-NoProfile");
            start.ArgumentList.Add("-NonInteractive");
            start.ArgumentList.Add("-Command");
            start.ArgumentList.Add(command.Script);
        }
        else if (command.Kind == "script" && command.Shell == "cmd" && command.Script is not null)
        {
            start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "cmd.exe"));
            start.ArgumentList.Add("/d");
            start.ArgumentList.Add("/s");
            start.ArgumentList.Add("/c");
            start.ArgumentList.Add(command.Script);
        }
        else
        {
            throw new InvalidDataException("Unsupported command specification");
        }

        start.WorkingDirectory = specification.WorkingDirectory;
        start.UseShellExecute = false;
        start.CreateNoWindow = true;
        start.RedirectStandardOutput = true;
        start.RedirectStandardError = true;
        start.RedirectStandardInput = false;
        start.Environment.Clear();
        foreach (var item in specification.Environment)
        {
            start.Environment[item.Key] = item.Value;
        }
        return start;
    }

    internal static string ResolveConsoleExecutable(
        RunSpecification specification,
        string executable)
    {
        if (Path.IsPathFullyQualified(executable))
        {
            throw new InvalidDataException("Executable must be a command name or project-relative path");
        }

        IEnumerable<string> bases;
        if (executable.Contains(Path.DirectorySeparatorChar)
            || executable.Contains(Path.AltDirectorySeparatorChar))
        {
            var candidate = Path.GetFullPath(Path.Combine(specification.ProjectRoot, executable));
            var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(specification.ProjectRoot));
            if (!candidate.StartsWith(root + Path.DirectorySeparatorChar,
                    StringComparison.OrdinalIgnoreCase))
            {
                throw new InvalidDataException("Executable is outside the registered project");
            }
            bases = [candidate];
        }
        else
        {
            var path = EnvironmentValue(specification.Environment, "PATH") ?? string.Empty;
            bases = path.Split(Path.PathSeparator, StringSplitOptions.RemoveEmptyEntries)
                .Select(directory => Path.Combine(directory, executable));
        }

        var extensions = Path.HasExtension(executable)
            ? [string.Empty]
            : (EnvironmentValue(specification.Environment, "PATHEXT") ?? ".EXE;.COM")
                .Split(';', StringSplitOptions.RemoveEmptyEntries);
        var resolved = bases.SelectMany(candidate => extensions.Select(extension => candidate + extension))
            .FirstOrDefault(File.Exists);
        if (resolved is null || PortableExecutable.ReadSubsystem(resolved) != PeSubsystem.WindowsConsole)
        {
            throw new InvalidDataException(
                "Structured executable must resolve to a Windows console PE; GUI execution is forbidden");
        }
        return resolved;
    }

    private static string? EnvironmentValue(
        IReadOnlyDictionary<string, string> environment,
        string name) => environment.FirstOrDefault(
            item => item.Key.Equals(name, StringComparison.OrdinalIgnoreCase)).Value;

    internal static void ValidateSpecification(RunSpecification specification)
    {
        if (!Path.IsPathFullyQualified(specification.ProjectRoot)
            || !Directory.Exists(specification.ProjectRoot)
            || !Path.IsPathFullyQualified(specification.WorkingDirectory))
        {
            throw new InvalidDataException("Project and working directories must be absolute");
        }
        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(specification.ProjectRoot));
        var working = Path.TrimEndingDirectorySeparator(Path.GetFullPath(specification.WorkingDirectory));
        if (!specification.ExternalWorkingDirectoryAuthorized
            && !working.Equals(root, StringComparison.OrdinalIgnoreCase)
            && !working.StartsWith(root + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
        {
            throw new InvalidDataException("Working directory is outside the registered project");
        }
        if (specification.TimeoutSeconds is < 1 or > 1800
            || specification.OutputLimitBytes is < 1024 or > 10 * 1024 * 1024)
        {
            throw new InvalidDataException("Execution bounds are invalid");
        }
    }
}
