# Code Intelligence via LSP

## Status

Implemented for Codito 0.2.0. The final architecture is documented in
[`ADR 0019`](../adr/0019-code-intelligence.md) and combines exact LSP semantics with an
optional structural graph provider behind one language-neutral Codito API.

## Problem

Codito can currently read files, search text, patch code, and execute shell commands, but ChatGPT still sees source code mostly as text. A coding agent needs semantic understanding similar to an IDE: where a symbol is defined, where it is referenced, what the compiler/language server reports, and how code is structurally related.

Text search is useful, but it cannot reliably answer semantic questions such as whether two identical names refer to the same symbol or which overload/implementation a call resolves to.

## Idea

Add a Code Intelligence capability backed primarily by Language Server Protocol integrations rather than implementing parsers independently for every language.

Initial focused MCP tools could be:

- `code_symbols` — list/search workspace or document symbols.
- `code_definition` — resolve the semantic definition of a symbol at a location.
- `code_references` — find semantic references to a symbol.
- `code_diagnostics` — return compiler/language-server diagnostics with file, range, severity, code, and message.

Later capabilities may include:

- hover/type information,
- implementations,
- rename,
- code actions,
- formatting,
- call hierarchy,
- type hierarchy.

## Proposed Direction

Codito should act as a language-intelligence broker:

```text
ChatGPT
  -> Codito MCP
  -> Code Intelligence Broker
  -> Language Server
     -> C# / Roslyn-compatible server
     -> TypeScript language server
     -> Pyright
     -> rust-analyzer
     -> gopls
     -> other supported LSP servers
```

The MCP surface should stay language-neutral and return normalized structured results.

## Why This Matters

This is one of the highest-value improvements for Codito because it changes the product from a safe remote file/shell bridge into a development environment that lets ChatGPT reason about code semantically.

It should improve navigation, debugging, refactoring safety, architectural analysis, and the quality of generated patches while reducing expensive sequences of text searches and file reads.

## Open Questions

- Which languages should be supported first?
- Should Codito manage language-server lifecycle automatically per project?
- How should project/solution initialization and indexing state be represented?
- How should stale diagnostics and changing files be handled?
- Which operations are read-only and which require explicit mutation permissions?
- How should very large reference/symbol result sets be paginated?

## Outcome

The implementation expanded the original proposal into 16 focused `code_*` tools,
automatic content-hash freshness, explicit `code_reindex`, version-aware diagnostics,
bounded provider lifecycle, structural graph fallback and per-call native execution
authorization. The MCP contract remains language-neutral.

