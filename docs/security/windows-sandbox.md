# Windows execution boundary

## Broker responsibility

The .NET 8 broker is the only component allowed to create AppContainer profiles,
derive capability SIDs, adjust project ACL grants, build restricted process startup
attributes, assign Job Objects, interact with CNG providers, and perform security-
critical handle inspection. Its API is narrow and versioned; arbitrary P/Invoke is
not exposed to Python.

The broker and daemon are per-user. There is no privileged service and no elevation.
The broker validates its caller using a per-install/per-user authenticated IPC
channel and rejects additional clients.

## Isolated process recipe

1. Open and verify the registered root identity and fixed local NTFS/ReFS volume.
2. Resolve the executable under the sanitized executable policy; never rely on a
   caller-controlled current directory or inherited PATH/PATHEXT/COMSPEC.
3. Create/select a deterministic per-project AppContainer profile whose identity is
   bound to the project ID/root fingerprint.
4. Grant only the minimum project-root capability SID access. Do not grant broad
   known-folder/package capabilities or network capability.
5. Construct a sanitized allowlist environment and explicit working directory.
6. Use `STARTUPINFOEX` security-capabilities and an explicit handle list; mark no
   unrelated handle inheritable.
7. Create suspended, assign a Job Object configured kill-on-close/no breakaway,
   then resume. If assignment fails, terminate before any user code runs.
8. Stream bounded output, enforce deadline/cancel, and close the Job Object to kill
   the entire process tree.

## Mandatory self-test

The installed release must run a versioned self-test before silent isolated use and
after upgrade/resume when security context may change. It proves:

- the token is in the expected AppContainer and integrity context;
- allowed create/read/write occurs within a temporary registered test root;
- read/write of representative outside and credential paths fails;
- outbound IPv4, IPv6, DNS, and loopback connections fail;
- inherited sentinel handles are absent;
- child/grandchild processes remain in the Job and die on close;
- timeout and cancellation terminate descendants;
- reparse/hardlink tests fail safely.

The daemon stores only the signed broker version/test-result metadata. Any failed,
skipped, stale, or unverifiable result disables isolated shell execution.

## Filesystem operations

Python lexical validation is defense in depth, not the boundary. The broker/agent
opens roots/components with flags that expose rather than follow reparses, validates
volume/file IDs and attributes, rejects placeholders/reparse traversal, and retains
handles through the operation. Mutation rejects link count greater than one and
uses a validated parent handle plus same-directory temporary and atomic replacement.

## Packaging and signing

The broker publishes self-contained `win-x64`, is bundled with the PyInstaller agent,
and is installed per-user. CI records hashes and an SBOM. Production signing is a
release gate when a certificate is supplied; unsigned test installers display the
expected Windows trust warning and are never described as trusted production builds.
