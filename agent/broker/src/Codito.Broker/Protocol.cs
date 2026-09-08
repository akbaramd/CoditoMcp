using System.Text.Json.Serialization;

namespace Codito.Broker;

internal sealed record BrokerRequest(
    [property: JsonPropertyName("version")] int Version,
    [property: JsonPropertyName("operation")] string Operation,
    [property: JsonPropertyName("mode")] string? Mode,
    [property: JsonPropertyName("specification")] RunSpecification? Specification,
    [property: JsonPropertyName("toast")] ToastSpecification? Toast = null,
    [property: JsonPropertyName("notification_tag")] string? NotificationTag = null);

internal sealed record ToastSpecification(
    [property: JsonPropertyName("xml")] string Xml,
    [property: JsonPropertyName("tag")] string Tag,
    [property: JsonPropertyName("expires_at")] DateTimeOffset ExpiresAt);

internal sealed record RunSpecification(
    [property: JsonPropertyName("project_root")] string ProjectRoot,
    [property: JsonPropertyName("root_fingerprint")] string RootFingerprint,
    [property: JsonPropertyName("working_directory")] string WorkingDirectory,
    [property: JsonPropertyName("command")] CommandSpecification Command,
    [property: JsonPropertyName("environment")] Dictionary<string, string> Environment,
    [property: JsonPropertyName("timeout_seconds")] int TimeoutSeconds,
    [property: JsonPropertyName("output_limit_bytes")] int OutputLimitBytes,
    [property: JsonPropertyName("nonce")] string Nonce,
    [property: JsonPropertyName("external_working_directory_authorized")] bool ExternalWorkingDirectoryAuthorized = false);

internal sealed record CommandSpecification(
    [property: JsonPropertyName("kind")] string Kind,
    [property: JsonPropertyName("executable")] string? Executable,
    [property: JsonPropertyName("arguments")] string[]? Arguments,
    [property: JsonPropertyName("shell")] string? Shell,
    [property: JsonPropertyName("script")] string? Script);

internal sealed record CapabilitySet(
    [property: JsonPropertyName("job_object")] bool JobObject,
    [property: JsonPropertyName("appcontainer_api")] bool AppContainerApi,
    [property: JsonPropertyName("filesystem_acl")] bool FilesystemAcl,
    [property: JsonPropertyName("network_denial")] bool NetworkDenial,
    [property: JsonPropertyName("child_containment")] bool ChildContainment,
    [property: JsonPropertyName("isolation_proven")] bool IsolationProven);

internal sealed record ProbeResponse(
    [property: JsonPropertyName("version")] int Version,
    [property: JsonPropertyName("capabilities")] CapabilitySet Capabilities,
    [property: JsonPropertyName("reason")] string? Reason);
