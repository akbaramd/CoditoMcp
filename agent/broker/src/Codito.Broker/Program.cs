using System.Text.Json;
using Codito.Broker;

if (args.Length != 1 || args[0] is not ("--json" or "--run-json"))
{
    Console.Error.WriteLine("Codito.Broker accepts only --json or --run-json.");
    return 64;
}

try
{
    var request = await JsonSerializer.DeserializeAsync<BrokerRequest>(Console.OpenStandardInput());
    if (request is null || request.Version != 1)
    {
        Console.Error.WriteLine("Unsupported or malformed broker request.");
        return 65;
    }
    if (args[0] == "--json" && request.Operation == "probe")
    {
        await JsonSerializer.SerializeAsync(Console.OpenStandardOutput(), CapabilityProbe.Probe());
        return 0;
    }
    if (args[0] == "--run-json" && request.Operation == "run" && request.Specification is not null)
    {
        if (request.Mode == "isolated")
        {
            Console.Error.WriteLine("Isolated execution is unavailable until its self-test passes.");
            return 125;
        }
        if (request.Mode != "native")
        {
            return 65;
        }
        return await NativeRunner.RunAsync(request.Specification);
    }
    Console.Error.WriteLine("Unsupported broker operation.");
    return 65;
}
catch (Exception exception) when (
    exception is JsonException or InvalidDataException or InvalidOperationException)
{
    Console.Error.WriteLine(exception.Message);
    return 65;
}

