"""MCP servers as tool sources for a run.

Three kinds of server:

- stdio: a program from the settings (`command`, `args`, `env`), spoken to
  in newline-delimited JSON-RPC on its stdin and stdout.
- HTTP: an endpoint (`url`, `headers`) over Streamable HTTP; an answer may
  come as JSON or as an SSE stream.
- MatterWave's own `/mcp`, pre-wired with the user's credential through the
  SDK's `client.mcp`, unless the settings switch it off.

A server's tools are offered to the model as `mcp__<server>__<tool>`, only
the ones its allow-list (`tools`) lets through and its `exclude_tools` does
not name. A tool the server marks `readOnlyHint` is a read; anything else
is a write and goes through the same approval as a file change or a
command: asked for in `ask` and `auto-edit` unless the server is trusted,
never offered in `plan`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .. import __version__
from .settings import McpServerConfig
from .shell import ProcessTree, child_env

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "matterwave-tui", "version": __version__}
RESULT_MAX_CHARS = 64_000
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class McpFailure(RuntimeError):
    pass


@dataclass(frozen=True)
class McpToolInfo:
    server: str
    name: str
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    trusted: bool

    @property
    def qualified(self) -> str:
        return qualified_name(self.server, self.name)

    def to_openai(self) -> dict[str, Any]:
        schema = dict(self.input_schema) if self.input_schema else {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        return {
            "type": "function",
            "function": {
                "name": self.qualified,
                "description": f"[MCP server {self.server}] {self.description}".strip(),
                "parameters": schema,
            },
        }


def qualified_name(server: str, tool: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", f"mcp__{server}__{tool}")
    return clean[:64]


def _expand(value: str) -> str:
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def _result_text(result: dict[str, Any]) -> tuple[str, bool]:
    blocks = result.get("content")
    texts: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                texts.append(block["text"])
            else:
                texts.append(f"[{block.get('type', 'content')} omitted]")
    if not texts and isinstance(result.get("structuredContent"), dict):
        texts.append(json.dumps(result["structuredContent"], indent=1))
    text = "\n".join(texts) or "(no content)"
    if len(text) > RESULT_MAX_CHARS:
        text = text[:RESULT_MAX_CHARS] + f"\n(cut off at {RESULT_MAX_CHARS} characters)"
    return text, result.get("isError") is True


class McpServer(Protocol):
    name: str

    async def start(self) -> None: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]: ...

    async def close(self) -> None: ...


# --- JSON-RPC over stdio -----------------------------------------------------------


class StdioServer:
    def __init__(self, config: McpServerConfig, cwd: str) -> None:
        self.name = config.name
        self.config = config
        self.cwd = config.cwd or cwd
        self._tree: ProcessTree | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next = 0
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        command = self.config.command or ""
        resolved = shutil.which(command) or command
        argv = [resolved, *[_expand(a) for a in self.config.args]]
        if resolved.lower().endswith((".cmd", ".bat")):
            argv = ["cmd", "/c", *argv]
        env = child_env()
        env.update({k: _expand(v) for k, v in self.config.env.items()})
        kwargs: dict[str, Any] = {}
        if os.name != "nt":
            kwargs["start_new_session"] = True
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=16 * 1024 * 1024,
                **kwargs,
            )
        except OSError as exc:
            raise McpFailure(f"{command} could not be started: {exc}") from exc
        self._tree = ProcessTree(proc)
        self._reader = asyncio.create_task(self._read())
        await self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        await self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def _send(self, message: dict[str, Any]) -> None:
        tree = self._tree
        if tree is None or tree.proc.stdin is None:
            raise McpFailure("the server is not running")
        tree.proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await tree.proc.stdin.drain()

    async def _read(self) -> None:
        tree = self._tree
        if tree is None or tree.proc.stdout is None:
            return
        stdout = tree.proc.stdout
        try:
            while True:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if "method" in message and "id" in message:
                    # A request from the server (ping, roots): answered, not served.
                    reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
                    if message["method"] == "ping":
                        reply["result"] = {}
                    else:
                        reply["error"] = {"code": -32601, "message": "not supported"}
                    with contextlib.suppress(McpFailure, OSError):
                        await self._send(reply)
                    continue
                ident = message.get("id")
                future = self._pending.pop(ident, None) if isinstance(ident, int) else None
                if future is not None and not future.done():
                    future.set_result(message)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(McpFailure("the server exited"))
            self._pending.clear()

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._next += 1
            request_id = self._next
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            message = await asyncio.wait_for(future, self.config.timeout_s)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise McpFailure(f"{method} timed out after {self.config.timeout_s:g}s") from exc
        if "error" in message:
            error = message["error"]
            text = error.get("message") if isinstance(error, dict) else str(error)
            raise McpFailure(str(text))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools = result.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        return _result_text(await self._request("tools/call", {"name": tool, "arguments": arguments}))

    async def close(self) -> None:
        if self._tree is not None:
            tree, self._tree = self._tree, None
            if tree.proc.stdin is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    tree.proc.stdin.close()
            await tree.kill()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader


# --- Streamable HTTP ----------------------------------------------------------------


class HttpServer:
    def __init__(self, config: McpServerConfig, client: httpx.AsyncClient | None = None) -> None:
        self.name = config.name
        self.config = config
        self._client = client
        self._owns = client is None
        self._session: str | None = None
        self._next = 0

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.config.timeout_s)
        await self._request(
            "initialize",
            {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
        )
        with contextlib.suppress(McpFailure, httpx.HTTPError):
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
        }
        headers.update({k: _expand(v) for k, v in self.config.headers.items()})
        if self._session:
            headers["Mcp-Session-Id"] = self._session
        return headers

    async def _post(self, body: dict[str, Any]) -> httpx.Response:
        if self._client is None:
            raise McpFailure("the server is not connected")
        response = await self._client.post(_expand(self.config.url or ""), json=body, headers=self._headers())
        session = response.headers.get("mcp-session-id")
        if session:
            self._session = session
        return response

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next += 1
        request_id = self._next
        try:
            response = await self._post(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
            )
        except httpx.HTTPError as exc:
            raise McpFailure(f"{method} failed: {exc}") from exc
        if response.status_code >= 400:
            raise McpFailure(f"{method} failed: HTTP {response.status_code}")
        message = self._message(response, request_id)
        if "error" in message:
            error = message["error"]
            raise McpFailure(str(error.get("message") if isinstance(error, dict) else error))
        result = message.get("result")
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _message(response: httpx.Response, request_id: int) -> dict[str, Any]:
        kind = response.headers.get("content-type", "")
        if "text/event-stream" in kind:
            for block in response.text.split("\n\n"):
                data = "\n".join(
                    line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")
                )
                if not data:
                    continue
                with contextlib.suppress(ValueError):
                    message = json.loads(data)
                    if isinstance(message, dict) and message.get("id") == request_id:
                        return message
            raise McpFailure("the stream ended without an answer")
        try:
            message = response.json()
        except ValueError as exc:
            raise McpFailure("the answer is not JSON") from exc
        if not isinstance(message, dict):
            raise McpFailure("the answer is not a JSON-RPC message")
        return message

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await self._request("tools/list", {})
        tools = result.get("tools")
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        return _result_text(await self._request("tools/call", {"name": tool, "arguments": arguments}))

    async def close(self) -> None:
        if self._owns and self._client is not None:
            await self._client.aclose()
        self._client = None


# --- MatterWave's own /mcp, through the SDK ----------------------------------------------


class MatterWaveMcp(Protocol):
    async def mcp_tools(self) -> list[dict[str, Any]]: ...

    async def mcp_call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]: ...


class MatterWaveServer:
    """The deployment's `/mcp`, called with the user's own credential (the
    SDK's `client.mcp`): the conversation tools, as the key's scopes allow."""

    name = "matterwave"

    def __init__(self, wire: MatterWaveMcp) -> None:
        self.wire = wire

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[dict[str, Any]]:
        return await self.wire.mcp_tools()

    async def call(self, tool: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        return await self.wire.mcp_call(tool, arguments)

    async def close(self) -> None:
        return None


# --- the servers of one session ----------------------------------------------------------


class McpManager:
    """Starts the configured servers, lists their allowed tools, and routes
    a call to the server that owns it."""

    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        *,
        cwd: str,
        matterwave: MatterWaveMcp | None = None,
        matterwave_tools: tuple[str, ...] | None = None,
        factory: Callable[[McpServerConfig], McpServer] | None = None,
    ) -> None:
        self.configs = {c.name: c for c in configs}
        self.cwd = cwd
        self.matterwave = matterwave
        self.matterwave_tools = matterwave_tools
        self.factory = factory
        self.servers: dict[str, McpServer] = {}
        self.tools: dict[str, McpToolInfo] = {}
        self.notices: list[str] = []
        self.started = False

    def _make(self, config: McpServerConfig) -> McpServer | None:
        if self.factory is not None:
            return self.factory(config)
        if config.command:
            return StdioServer(config, self.cwd)
        if config.url:
            return HttpServer(config)
        self.notices.append(f"MCP server {config.name} has neither a command nor a url; skipped.")
        return None

    async def start(self) -> list[str]:
        """Connect to every server once; the notices for those that failed."""
        if self.started:
            return []
        self.started = True
        plan: list[tuple[McpServer, McpServerConfig]] = []
        for config in self.configs.values():
            server = self._make(config)
            if server is not None:
                plan.append((server, config))
        if self.matterwave is not None and "matterwave" not in self.configs:
            builtin = McpServerConfig(name="matterwave", tools=self.matterwave_tools, source="builtin")
            plan.append((MatterWaveServer(self.matterwave), builtin))

        async def one(server: McpServer, config: McpServerConfig) -> None:
            try:
                await server.start()
                listed = await server.list_tools()
            except Exception as exc:  # a server is optional: report, go on
                self.notices.append(f"MCP server {config.name} is unavailable: {exc}")
                with contextlib.suppress(Exception):
                    await server.close()
                return
            self.servers[config.name] = server
            for raw in listed:
                name = raw.get("name")
                if not isinstance(name, str):
                    continue
                if config.tools is not None and name not in config.tools:
                    continue
                if name in config.exclude_tools:
                    continue
                annotations = raw.get("annotations")
                hinted = isinstance(annotations, dict) and annotations.get("readOnlyHint") is True
                # The hint is the server's word about itself: taken from a
                # server the user trusts, and from MatterWave's own.
                read_only = hinted and (config.trust or config.source == "builtin")
                schema = raw.get("inputSchema")
                info = McpToolInfo(
                    server=config.name,
                    name=name,
                    description=str(raw.get("description") or ""),
                    input_schema=dict(schema) if isinstance(schema, dict) else {},
                    read_only=read_only,
                    trusted=config.trust,
                )
                self.tools[info.qualified] = info

        await asyncio.gather(*(one(s, c) for s, c in plan))
        return list(self.notices)

    async def call(self, qualified: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        info = self.tools.get(qualified)
        if info is None:
            return f"Refused: {qualified} is not an available tool.", True
        server = self.servers.get(info.server)
        if server is None:
            return f"MCP server {info.server} is not connected.", True
        try:
            return await server.call(info.name, arguments)
        except Exception as exc:
            return f"MCP tool {info.server}.{info.name} failed: {exc}", True

    async def close(self) -> None:
        servers, self.servers = list(self.servers.values()), {}
        closers: list[Awaitable[None]] = [s.close() for s in servers]
        for closer in closers:
            with contextlib.suppress(Exception):
                await closer
