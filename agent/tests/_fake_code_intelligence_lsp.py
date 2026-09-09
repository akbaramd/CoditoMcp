"""Deterministic real-stdio LSP fixture; never used by production code."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname


def path_from_uri(uri: str) -> Path:
    return Path(url2pathname(urlparse(uri).path))


def send(payload: dict[str, object]) -> None:
    body = json.dumps(payload, ensure_ascii=True).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    sys.stdout.buffer.flush()


def read() -> dict[str, object] | None:
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line == b"\r\n":
            break
        key, _, value = line.partition(b":")
        if key.lower() == b"content-length":
            length = int(value)
    if length is None or length > 2_000_000:
        raise ValueError("Invalid fixture request size")
    return json.loads(sys.stdin.buffer.read(length))


def symbols(documents: dict[str, str], query: str = "") -> list[dict[str, object]]:
    found = []
    for uri, text in sorted(documents.items()):
        for number, line in enumerate(text.splitlines()):
            match = re.search(r"(?:def|class|function)\s+(\w+)", line)
            if match and query.lower() in match[1].lower():
                start = len(line[: match.start(1)].encode("utf-16-le")) // 2
                end = start + len(match[1])
                found.append(
                    {
                        "name": match[1],
                        "kind": 12,
                        "location": {
                            "uri": uri,
                            "range": {
                                "start": {"line": number, "character": start},
                                "end": {"line": number, "character": end},
                            },
                        },
                    }
                )
    return found


def hierarchy_item(uri: str, name: str, line: int, kind: int = 12) -> dict[str, object]:
    source_range = {
        "start": {"line": line, "character": 0},
        "end": {"line": line, "character": 1},
    }
    return {
        "name": name,
        "kind": kind,
        "uri": uri,
        "range": source_range,
        "selectionRange": source_range,
    }


def main() -> None:
    documents: dict[str, str] = {}
    versions: dict[str, int] = {}

    def publish(uri: str, text: str, version: int) -> None:
        if "--push-diagnostics" not in sys.argv:
            return
        emitted_uri = uri
        if "--alternate-uri-diagnostics" in sys.argv and emitted_uri.startswith("file:///"):
            suffix = emitted_uri[len("file:///") :]
            if len(suffix) >= 2 and suffix[1] == ":":
                emitted_uri = "file:///" + suffix[0].swapcase() + suffix[1:]
        diagnostics = []
        if "BAD" in text:
            diagnostics.append(
                {
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 3},
                    },
                    "severity": 1,
                    "code": "fixture-bad",
                    "source": "fixture",
                    "message": "Fixture diagnostic",
                }
            )
        params: dict[str, object] = {"uri": emitted_uri, "diagnostics": diagnostics}
        if "--versionless-diagnostics" not in sys.argv:
            params["version"] = version
        send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": params,
            }
        )

    while (message := read()) is not None:
        method = message.get("method")
        params = message.get("params") or {}
        identity = message.get("id")
        result: object = None
        if method == "initialize":
            root = path_from_uri(params["rootUri"])
            documents = {
                p.as_uri(): p.read_text(encoding="utf-8")
                for p in root.rglob("*")
                if p.is_file() and p.suffix in {".py", ".ts", ".java", ".cs"}
            }
            capabilities: dict[str, object] = {
                "positionEncoding": "utf-16",
                "textDocumentSync": {"openClose": True, "change": 1},
                "definitionProvider": True,
                "referencesProvider": True,
                "hoverProvider": True,
                "documentSymbolProvider": True,
                "workspaceSymbolProvider": True,
                "callHierarchyProvider": True,
                "typeHierarchyProvider": True,
            }
            if "--push-diagnostics" not in sys.argv:
                capabilities["diagnosticProvider"] = {
                    "interFileDependencies": True,
                    "workspaceDiagnostics": False,
                }
            result = {
                "capabilities": capabilities,
                "serverInfo": {"name": "codito-stdio-fixture", "version": "1"},
            }
        elif method == "textDocument/didOpen":
            doc = params["textDocument"]
            documents[doc["uri"]] = doc["text"]
            versions[doc["uri"]] = doc["version"]
            publish(doc["uri"], doc["text"], doc["version"])
            continue
        elif method == "textDocument/didChange":
            document = params["textDocument"]
            text = params["contentChanges"][-1]["text"]
            documents[document["uri"]] = text
            versions[document["uri"]] = document["version"]
            publish(document["uri"], text, document["version"])
            continue
        elif method == "textDocument/didClose":
            uri = params["textDocument"]["uri"]
            versions.pop(uri, None)
            continue
        elif method == "workspace/didChangeWatchedFiles":
            for event in params["changes"]:
                path = path_from_uri(event["uri"])
                if event["type"] == 3:
                    documents.pop(event["uri"], None)
                else:
                    documents[event["uri"]] = path.read_text(encoding="utf-8")
            continue
        elif method == "workspace/symbol":
            result = symbols(documents, params["query"])
        elif method == "textDocument/documentSymbol":
            uri = params["textDocument"]["uri"]
            result = symbols({uri: documents.get(uri, "")})
        elif method == "textDocument/hover":
            if "--hang-hover" in sys.argv:
                continue
            text = documents.get(params["textDocument"]["uri"], "")
            result = {
                "contents": {
                    "kind": "plaintext",
                    "value": "sha256=" + hashlib.sha256(text.encode()).hexdigest(),
                }
            }
        elif method == "textDocument/definition":
            result = [item["location"] for item in symbols(documents)][:1]
        elif method == "textDocument/references":
            result = [item["location"] for item in symbols(documents)]
        elif method == "textDocument/prepareCallHierarchy":
            uri = params["textDocument"]["uri"]
            result = [hierarchy_item(uri, "root", 0)]
        elif method == "callHierarchy/incomingCalls":
            item = params["item"]
            uri = item["uri"]
            mapping = {"root": ("caller1", 1), "caller1": ("caller2", 2)}
            child = mapping.get(item["name"])
            result = (
                [
                    {
                        "from": hierarchy_item(uri, child[0], child[1]),
                        "fromRanges": [hierarchy_item(uri, "range", child[1])["range"]],
                    }
                ]
                if child
                else []
            )
        elif method == "callHierarchy/outgoingCalls":
            item = params["item"]
            uri = item["uri"]
            mapping = {"root": ("callee1", 3), "callee1": ("callee2", 4)}
            child = mapping.get(item["name"])
            result = (
                [
                    {
                        "to": hierarchy_item(uri, child[0], child[1]),
                        "fromRanges": [
                            hierarchy_item(uri, "range", item["range"]["start"]["line"])["range"]
                        ],
                    }
                ]
                if child
                else []
            )
        elif method == "textDocument/prepareTypeHierarchy":
            uri = params["textDocument"]["uri"]
            result = [hierarchy_item(uri, "TypeRoot", 0, 5)]
        elif method == "typeHierarchy/supertypes":
            item = params["item"]
            uri = item["uri"]
            mapping = {"TypeRoot": ("Super1", 1), "Super1": ("Super2", 2)}
            child = mapping.get(item["name"])
            result = [hierarchy_item(uri, child[0], child[1], 5)] if child else []
        elif method == "typeHierarchy/subtypes":
            item = params["item"]
            uri = item["uri"]
            mapping = {"TypeRoot": ("Sub1", 3), "Sub1": ("Sub2", 4)}
            child = mapping.get(item["name"])
            result = [hierarchy_item(uri, child[0], child[1], 5)] if child else []
        elif method == "textDocument/diagnostic":
            if params.get("previousResultId") == "fixture":
                result = {"kind": "unchanged", "resultId": "fixture"}
            else:
                result = {"kind": "full", "resultId": "fixture", "items": []}
        elif method == "shutdown":
            result = None
        elif method == "exit":
            return
        elif identity is None:
            continue
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identity,
                    "error": {"code": -32601, "message": "Method not supported by fixture"},
                }
            )
            continue
        if identity is not None:
            send({"jsonrpc": "2.0", "id": identity, "result": result})


if __name__ == "__main__":
    main()
