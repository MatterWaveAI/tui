"""TUI `MatterWaveClient` wrapper with SDK delegation and graceful `httpx` fallback."""

from __future__ import annotations

import inspect
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import httpx

from .api import DEFAULT_BASE_URL, HttpChatAPI, TokenRefresher


def _normalize_result(value: Any) -> dict[str, Any]:
    """Convert an SDK model, dataclass, list, or mapping into a plain dict."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {
            "data": [
                asdict(item) if is_dataclass(item) and not isinstance(item, type) else item
                for item in value
            ]
        }
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        res = to_dict()
        if isinstance(res, dict):
            return res
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        res = model_dump()
        if isinstance(res, dict):
            return res
    return {"result": value}


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class MatterWaveClient(HttpChatAPI):
    """Async TUI client wrapper over the MatterWave SDK with graceful HTTP fallback via `httpx`."""

    def __init__(
        self,
        sdk: Any | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        token: str = "",
        api_key: str | None = None,
        refresh: TokenRefresher | None = None,
        http_client: httpx.AsyncClient | None = None,
        debug_log: Path | None = None,
    ) -> None:
        self._sdk = sdk
        resolved_base_url = base_url
        if sdk is not None and base_url == DEFAULT_BASE_URL:
            sdk_url = getattr(sdk, "base_url", None)
            if isinstance(sdk_url, str) and sdk_url.strip():
                resolved_base_url = sdk_url.strip()
        resolved_token = api_key if api_key is not None else token
        if not resolved_token and sdk is not None:
            sdk_cfg = getattr(sdk, "_config", None)
            cfg_key = getattr(sdk_cfg, "api_key", None) or getattr(sdk, "api_key", None)
            if isinstance(cfg_key, str):
                resolved_token = cfg_key
        resolved_http = http_client
        if resolved_http is None and sdk is not None:
            sdk_http = getattr(sdk, "_client", None) or getattr(sdk, "http_client", None)
            if isinstance(sdk_http, httpx.AsyncClient):
                resolved_http = sdk_http
        super().__init__(
            base_url=resolved_base_url,
            token=resolved_token,
            refresh=refresh,
            http_client=resolved_http,
            debug_log=debug_log,
        )

    def _sdk_callable(self, * dotted_paths: str) -> Any | None:
        """Return the first callable attribute found on `self._sdk`, or None if absent."""
        sdk = self._sdk
        if sdk is None:
            return None
        for dotted in dotted_paths:
            target: Any = sdk
            found = True
            for part in dotted.split("."):
                # Use inspect.getattr_static / vars check on mocks with spec=[] or plain objects
                try:
                    target = getattr(target, part)
                except AttributeError:
                    found = False
                    break
            if found and callable(target):
                return target
        return None

    async def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
        """Run web search via SDK method if available, falling back to `POST /v1/search`."""
        fn = self._sdk_callable("search_web", "search.query", "search.create")
        if fn is not None:
            try:
                res = await _maybe_await(fn(query=query, max_results=max_results))
                return _normalize_result(res)
            except TypeError:
                res = await _maybe_await(fn(query, max_results))
                return _normalize_result(res)
        return await super().search_web(query, max_results=max_results)

    async def query_rag(
        self, collection_id: str, query: str, top_k: int = 5
    ) -> dict[str, Any]:
        """Query a RAG collection via SDK method if available, falling back to `POST /v1/rag/query`."""
        fn = self._sdk_callable("query_rag", "rag.query")
        if fn is not None:
            try:
                res = await _maybe_await(
                    fn(collection_id=collection_id, query=query, top_k=top_k)
                )
                return _normalize_result(res)
            except TypeError:
                res = await _maybe_await(fn(collection_id, query, top_k))
                return _normalize_result(res)
        return await super().query_rag(collection_id, query, top_k=top_k)

    async def list_rag_collections(self) -> dict[str, Any]:
        """List RAG collections via SDK method if available, falling back to `GET /v1/rag/collections`."""
        fn = self._sdk_callable("list_rag_collections", "rag.list_collections", "rag.collections")
        if fn is not None:
            res = await _maybe_await(fn())
            return _normalize_result(res)
        return await super().list_rag_collections()

    async def list_mcp_servers(self) -> dict[str, Any]:
        """List registered MCP servers via SDK if available, falling back to `GET /v1/mcp/servers`."""
        fn = self._sdk_callable("list_mcp_servers", "mcp.list_servers", "mcp_servers.list")
        if fn is not None:
            res = await _maybe_await(fn())
            return _normalize_result(res)
        return await super().list_mcp_servers()

    async def call_mcp_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Call a tool on a registered MCP server via SDK or `POST /v1/mcp/servers/{server_id}/call`."""
        args = arguments if arguments is not None else {}
        fn = self._sdk_callable("call_mcp_tool", "mcp.call_server_tool", "mcp_servers.call_tool")
        if fn is not None:
            try:
                res = await _maybe_await(
                    fn(server_id=server_id, tool_name=tool_name, arguments=args)
                )
                return _normalize_result(res)
            except TypeError:
                res = await _maybe_await(fn(server_id, tool_name, args))
                return _normalize_result(res)
        return await super().call_mcp_tool(server_id, tool_name, args)

    async def create_edit_session(self, goal: str, files: list[str]) -> dict[str, Any]:
        """Create a multi-file edit session via SDK or `POST /v1/edit-sessions`."""
        fn = self._sdk_callable("create_edit_session", "edit_sessions.create")
        if fn is not None:
            try:
                res = await _maybe_await(fn(goal=goal, files=list(files)))
                return _normalize_result(res)
            except TypeError:
                res = await _maybe_await(fn(goal, list(files)))
                return _normalize_result(res)
        return await super().create_edit_session(goal, files)

    async def list_agent_runs(
        self, agent_id: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        """List Agent Command Center runs via SDK or `GET /api/v1/runs`."""
        fn = self._sdk_callable("list_agent_runs", "runs.list")
        if fn is not None:
            try:
                res = await _maybe_await(fn(agent_id=agent_id, status=status))
                return _normalize_result(res)
            except TypeError:
                res = await _maybe_await(fn(agent_id, status))
                return _normalize_result(res)
        return await super().list_agent_runs(agent_id=agent_id, status=status)


__all__ = ["MatterWaveClient"]
