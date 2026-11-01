"""A minimal MCP server over stdio, for the client tests: newline-delimited
JSON-RPC, three tools (one marked read-only), and a log of the calls it ran
written beside it so a test can tell what really executed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else None

TOOLS = [
    {
        "name": "lookup",
        "description": "Look a word up.",
        "inputSchema": {
            "type": "object",
            "properties": {"word": {"type": "string"}},
            "required": ["word"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "deploy",
        "description": "Deploy the service.",
        "inputSchema": {"type": "object", "properties": {"target": {"type": "string"}}},
    },
    {
        "name": "drop_database",
        "description": "Not on the allow-list.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def answer(message_id: object, result: dict) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method = message.get("method")
    if "id" not in message:
        continue
    if method == "initialize":
        answer(
            message["id"],
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "demo", "version": "1"},
            },
        )
    elif method == "tools/list":
        answer(message["id"], {"tools": TOOLS})
    elif method == "tools/call":
        params = message.get("params") or {}
        if LOG is not None:
            with LOG.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(params) + "\n")
        name = params.get("name")
        args = params.get("arguments") or {}
        text = f"{name} ran with {json.dumps(args, sort_keys=True)}"
        answer(message["id"], {"content": [{"type": "text", "text": text}], "isError": False})
    else:
        sys.stdout.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "no such method"},
                }
            )
            + "\n"
        )
        sys.stdout.flush()
