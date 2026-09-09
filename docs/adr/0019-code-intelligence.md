# ADR 0019 – Language-neutral Code Intelligence

## Status

Accepted and implemented for Codito 0.2.0.

## Context

File reads, text search and native shell commands let an MCP client inspect source code,
but they do not provide IDE-level navigation or a repository-wide relationship model.
Codito must work across heterogeneous repositories: C#/.NET, Java, Python,
JavaScript/TypeScript/React/Next.js, Go, Rust, C/C++ and additional languages without
adding language-specific MCP schemas.

The two useful sources of code knowledge have different precision:

- Language Server Protocol (LSP) can provide exact semantic information when a trusted
  server for the language is locally available and actually returns the requested data.
- A structural code graph can cover many languages and repository-wide relationships,
  but it must not be presented as compiler-semantic truth.

## Decision

Codito exposes 16 focused, language-neutral `code_*` tools. They map to one internal
`project_code` wire operation and return normalized typed results with provider,
precision, snapshot/freshness and completeness metadata.

The public tools are:

`code_intelligence_status`, `code_workspace_summary`, `code_symbol_search`,
`code_definition`, `code_references`, `code_implementations`, `code_diagnostics`,
`code_hover`, `code_context`, `code_call_hierarchy`, `code_type_hierarchy`,
`code_impact`, `code_architecture`, `code_dependencies`, `code_related_tests`, and
`code_reindex`.

The architecture is deliberately provider-neutral:

```text
ChatGPT / MCP client
        |
        v
focused code_* tools
        |
        v
project_code wire operation
        |
        v
CodeIntelligenceManager
   |                |
   v                v
LSP router      Graph provider
(exact when     (structural,
available)       broad coverage)
```

### Semantic provider

Codito uses `pygls==2.1.1` as the generic LSP client transport. Language servers are
trusted local executables, never commands supplied by the repository or MCP caller.
Built-in discovery recognizes common servers such as Pyright/BasedPyright,
TypeScript Language Server, csharp-ls, gopls, rust-analyzer, clangd and jdtls. New
languages can be added through trusted per-project local profiles without changing the
MCP tool schemas.

LSP sessions negotiate actual capabilities. Unsupported operations return explicit
unavailable/partial state instead of a success-shaped empty result. An empty semantic
reference result is cross-checked with structural graph evidence when available.

Workspace-wide semantic results also track provider work-done progress. Exact precision
means returned locations/types are semantic; it does not claim the provider exhaustively
indexed every workspace reference. TypeScript Automatic Type Acquisition is disabled so
analysis does not download packages as a hidden side effect.

Public coordinates are 1-based Unicode-scalar lines/columns with end-exclusive ranges.
The LSP adapter converts them to negotiated UTF-16 offsets and rejects invalid offsets,
including offsets that split an astral character.

### Structural graph provider

`GraphProvider` is an internal interface. The first real implementation adapts
`codebase-memory-mcp` CLI semantics behind Codito; the backend is never exposed as a
separate MCP server to ChatGPT. Results are always marked structural/heuristic where
appropriate.

The graph process indexes only a private, snapshot-verified source mirror under the
Codito data directory. It uses a canonical root-derived identity, `persistence=false`,
a private cache and an allowed-root boundary. It never writes `.codebase-memory` or
`.gitattributes` into the user's repository and never exposes raw backend paths or
Cypher as public MCP arguments.

The graph runtime is optional because the current self-contained executable is large.
Codito first looks for a managed runtime at
`<data-directory>/runtimes/codebase-memory-mcp.exe` and then a trusted executable on
the user's PATH. Missing graph runtime is reported honestly by status/setup metadata;
Codito does not silently download one.

## Freshness contract

Correctness does not depend on Git HEAD, file watchers or a manual reindex.

1. Every query passes a disk reconciliation barrier. Eligible files are content hashed;
   additions, changes, deletions and renames—including untracked and same-size/
   same-mtime edits—change the workspace snapshot.
2. `.gitignore`/`.coditoignore` and hard exclusions are applied. Secret-like local
   files (`.env`, credentials/key/certificate material), dependency/build caches and
   Codito's own `.run`/analysis artifacts are excluded.
3. Reparse/symlink traversal is rejected and source reads reuse `ProjectPathResolver`
   and project root fingerprints.
4. Query/refresh is serialized per workspace. A generation becomes active only after
   provider synchronization succeeds and a post-sync disk verification matches.
5. The same query is checked again after execution. A repeatedly changing workspace
   returns a retryable `path_race`; Codito does not silently publish stale success.
6. `code_reindex` supports explicit `incremental` and `full` refresh and reports its
   actual generation, files processed, timeout and errors.

## Diagnostics and document synchronization

LSP documents use negotiated open/change/save semantics with monotonically increasing
versions. Pull diagnostics cache `resultId`; unchanged responses are considered ready
only when a prior verified result exists. Push diagnostics are ready only when the
reported document version matches the open document. Versionless pushes are partial,
version mismatches are stale, and absent analysis remains pending. An empty pending or
partial diagnostic list never means a clean build.

Provider-returned `file://` URIs are canonicalized for document identity on Windows while
the original protocol URI is preserved. This handles drive-letter casing and URI encoding
normalization without allowing an external path to bypass project scoping.

## Authorization and process boundary

- `code_intelligence_status` and `code_workspace_summary` are non-executing metadata
  reads and require `projects:read + files:read`.
- Analysis/reindex tools can start trusted native language/graph processes and therefore
  also require `shell:execute`. In Project access they use native-execution consent;
  Full device access is still an explicit local choice.
- Authorization generation, deadline, current project mode/root fingerprint and local
  approval are rechecked while a request is running. Revocation cancels the owned task.
- Provider process trees are agent-owned and bounded; Windows Job Objects terminate
  descendants on cleanup. No kill-by-name is used.
- LSP `workspace/applyEdit` is rejected; Code Intelligence is read-only. Mutations go
  through Codito's existing hash/journal patch path.
- External library URIs are omitted rather than leaking absolute host paths or bypassing
  registered-project file policy.

## Verification

The implementation includes real-stdio LSP regression tests for lifecycle, document
versions, diagnostics state/resultId, Unicode conversion, hierarchy traversal and
timeouts; manager tests for automatic external changes, reindex, query races, identity
isolation and graph fallback; and ProtocolAdapter tests for metadata/native consent and
mid-request revocation.

A real `codebase-memory-mcp` 0.9 smoke indexed a mixed Python/TypeScript/C#/Java fixture
and returned structural symbols, calls, references, impact, dependencies and related
tests without modifying the source repository. A real isolated Pyright 1.1.413 smoke
confirmed exact Definition and Hover. Pyright reference/diagnostic behavior in that
minimal fixture was not promoted to a stronger guarantee: empty references are
cross-checked structurally and unavailable/pending diagnostics remain labeled as such.
A separate isolated TypeScript Language Server 6.0.0 smoke confirmed exact definition
navigation and real push diagnostics, including an intentional type error. Its minimal
fixture also demonstrated that provider reference completeness is not guaranteed; Codito
therefore keeps workspace-wide semantic completeness false and retains graph fallback.

## Alternatives considered

- One public MCP per language: rejected; it makes the tool catalog language-specific.
- A home-grown parser/compiler/graph engine: rejected; it would duplicate maintained
  language infrastructure and still provide weaker semantics.
- Graph-only source of truth: rejected; structural graphs cannot replace LSP/compiler
  identity, overload and type resolution.
- LSP-only: rejected; it provides poor repository-wide coverage when a server is absent
  and cannot by itself express all architecture/impact relationships.
- Exposing Graphy/codebase-memory as another ChatGPT MCP: rejected; Codito must own
  project authorization, freshness, lifecycle and stable public contracts.

## Consequences

Codito now behaves as a language-neutral development-intelligence broker rather than a
text-only file bridge. Exact semantic quality depends on the locally available language
server; broad structural quality depends on the optional graph runtime. Every response
therefore carries provenance and precision rather than pretending all providers have
the same certainty.
