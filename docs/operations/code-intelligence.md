# Code Intelligence operations guide

Codito exposes Code Intelligence through the same MCP connection as file, shell,
screen and browser tools. There is no separate Graph/LSP MCP to connect.

## Tool families

- Discovery: `code_intelligence_status`, `code_workspace_summary`, `code_symbol_search`.
- Semantic navigation: `code_definition`, `code_references`, `code_implementations`,
  `code_hover`, `code_diagnostics`.
- Aggregation/hierarchy: `code_context`, `code_call_hierarchy`, `code_type_hierarchy`.
- Repository structure: `code_impact`, `code_architecture`, `code_dependencies`,
  `code_related_tests`.
- Freshness: `code_reindex` (`incremental` or `full`).

All results identify the snapshot/provider/precision and whether the response is
verified, partial, stale or unavailable. Structural graph output is not labeled exact.

## Language servers

Codito automatically discovers a bounded allowlist of trusted executables on the local
PATH for common languages, including Python, JavaScript/TypeScript, C#, Java, Go, Rust,
C/C++ and several additional ecosystems. Discovery does not download or launch a
provider; launch happens only for an authorized analysis request.

For TypeScript/JavaScript, Codito disables Automatic Type Acquisition so the language
server does not download `@types` packages as an implicit analysis side effect. Providers
that report work-done progress are given a bounded readiness window before workspace-wide
queries; timeout produces an incompleteness warning rather than a false completeness claim.

For another server or custom arguments, create an agent-owned profile:

```text
<Codito data directory>/code-intelligence/lsp-profiles/<project-id>.json
```

Example:

```json
{
  "command": ["C:/Tools/my-language-server.exe", "--stdio"],
  "name": "my-language",
  "version": "1.2.3",
  "initialization_options": {},
  "workspace_configuration": {}
}
```

The executable must resolve outside the registered project. Repository files cannot
inject a language-server command. `workspace/applyEdit` is refused in this release.

## Optional structural graph runtime

The first graph adapter targets the locally installed `codebase-memory-mcp` v0.9 CLI
contract. The graph runtime is intentionally not embedded in the core Windows package
because the current self-contained binary is very large.

Codito searches in this order:

1. `<Codito data directory>/runtimes/codebase-memory-mcp.exe`
2. a trusted `codebase-memory-mcp` executable on the user PATH

Codito does not download the runtime automatically. Without it, semantic LSP features
can still work and manifest-based dependency/architecture information remains
available, while graph-only operations report unavailable/degraded status.

Graph indexing uses a private Codito cache and a private source mirror. It does not
create graph metadata inside the source repository.

## Freshness and reindex

Manual reindex is not required after normal edits. Before every query Codito reconciles
the project files by content, detects add/edit/delete/rename and synchronizes providers.
This includes untracked changes and same-size/same-mtime replacements.

Use `code_reindex` when an explicit refresh is desired:

- `incremental`: force provider synchronization from the current project snapshot.
- `full`: rebuild provider state from the complete current snapshot.

The operation is bounded to 30 seconds at the MCP contract. Completion is not reported
until the requested refresh is actually synchronized; timeout/error fields are explicit.

## Permissions

Status and workspace summary are metadata reads. Other Code Intelligence requests may
start language/graph processes and require the same native execution OAuth capability
used by shell operations. Local Project access therefore prompts/uses the project's
native execution policy; Full device access remains an explicit local opt-in.

Revoking saved native permission while a code-analysis request is running invalidates
the approval generation and cancels that analysis request.

## Interpreting results

- `exact`: result came from a semantic provider for that operation.
- `structural`: result came from static graph/manifests and can miss dynamic/reflection
  behavior.
- `heuristic`: candidate based on conventions rather than proven identity.
- `unavailable`: no configured provider can answer the operation safely.

`precision=exact` applies to the semantic identity of returned items. For workspace-wide
queries such as references/hierarchies, check the independent `complete` flag as well;
some language servers can return exact but non-exhaustive results while indexing lazily.

Diagnostics additionally report `ready`, `partial`, `pending`, `stale` or `unavailable`.
An empty list is evidence of a clean analysis only when the diagnostic state is ready;
it is not a replacement for the project's build/test command.
