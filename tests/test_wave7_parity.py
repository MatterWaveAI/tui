"""Unit tests for Wave 7/8 TUI parity: `MatterWaveClient` helpers and interactive slash commands."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from textual.widgets import TextArea

from matterwave_tui.api import ChatAPI
from matterwave_tui.app import MatterWaveApp
from matterwave_tui.client import MatterWaveClient
from matterwave_tui.commands import (
    SLASH_COMMAND_DESCRIPTIONS,
    SLASH_COMMAND_SPECS,
    SLASH_COMMANDS,
)
from matterwave_tui.screens import HelpScreen
from matterwave_tui.widgets import MessageView
from tests.fake_api import FakeAPI


async def _settle(app: MatterWaveApp, pilot: Any) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
async def test_matterwave_client_httpx_fallback_when_sdk_mock_lacks_methods() -> None:
    """When the underlying SDK mock lacks Wave 7/8 methods, `MatterWaveClient` falls back to httpx."""
    recorded: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        recorded.append((request.method, request.url.path, params, body))

        if request.method == "POST" and request.url.path == "/v1/search":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "title": "MatterWave Search Guide",
                            "url": "https://docs.matterwave.test/search",
                            "snippet": "Guide to grounded web search citations.",
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/v1/rag/collections":
            return httpx.Response(
                200,
                json={
                    "collections": [
                        {"id": "col-eng", "name": "Engineering Handbook", "document_count": 42}
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/v1/rag/query":
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": "doc-1",
                            "source": "handbook.md",
                            "score": 0.91,
                            "text": "Deployment checklist and rollback policy.",
                        }
                    ]
                },
            )
        if request.method == "GET" and request.url.path == "/v1/mcp/servers":
            return httpx.Response(
                200,
                json={
                    "servers": [
                        {
                            "id": "srv-linear",
                            "name": "Linear MCP",
                            "status": "connected",
                            "tools": [{"name": "list_issues", "description": "List active issues"}],
                        }
                    ]
                },
            )
        if request.method == "POST" and request.url.path == "/v1/mcp/servers/srv-linear/call":
            return httpx.Response(
                200,
                json={"output": "3 open issues", "is_error": False},
            )
        if request.method == "POST" and request.url.path == "/v1/edit-sessions":
            return httpx.Response(
                200,
                json={
                    "id": "es-1",
                    "goal": body["goal"] if body else "",
                    "files": body["files"] if body else [],
                    "status": "created",
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/runs":
            return httpx.Response(
                200,
                json={
                    "runs": [
                        {
                            "id": "run-77",
                            "agent_id": params.get("agent_id", "agent-main"),
                            "status": params.get("status", "running"),
                            "goal": "Audit repository endpoints",
                        }
                    ]
                },
            )
        return httpx.Response(404, json={"detail": "Not found"})

    class BareSdkMock:
        """Mock SDK object that does not implement any Wave 7/8 methods."""

        base_url = "https://matterwave.test"
        api_key = "mw_test_token"

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = MatterWaveClient(BareSdkMock(), http_client=http_client)

        search_res = await client.search_web("matterwave docs", max_results=3)
        assert search_res["results"][0]["title"] == "MatterWave Search Guide"

        rag_cols = await client.list_rag_collections()
        assert rag_cols["collections"][0]["id"] == "col-eng"

        rag_res = await client.query_rag("col-eng", "rollback policy", top_k=4)
        assert rag_res["results"][0]["source"] == "handbook.md"

        mcp_srvs = await client.list_mcp_servers()
        assert mcp_srvs["servers"][0]["id"] == "srv-linear"

        mcp_call = await client.call_mcp_tool("srv-linear", "list_issues", {"team": "platform"})
        assert mcp_call["output"] == "3 open issues"

        edit_sess = await client.create_edit_session(
            "Update client methods", ["src/matterwave_tui/client.py", "src/matterwave_tui/app.py"]
        )
        assert edit_sess["id"] == "es-1"
        assert edit_sess["files"] == [
            "src/matterwave_tui/client.py",
            "src/matterwave_tui/app.py",
        ]

        runs_res = await client.list_agent_runs(agent_id="agent-ops", status="running")
        assert runs_res["runs"][0]["id"] == "run-77"
        assert runs_res["runs"][0]["agent_id"] == "agent-ops"

    assert recorded == [
        ("POST", "/v1/search", {}, {"query": "matterwave docs", "max_results": 3}),
        ("GET", "/v1/rag/collections", {}, None),
        (
            "POST",
            "/v1/rag/query",
            {},
            {"collection_id": "col-eng", "query": "rollback policy", "top_k": 4},
        ),
        ("GET", "/v1/mcp/servers", {}, None),
        (
            "POST",
            "/v1/mcp/servers/srv-linear/call",
            {},
            {"tool_name": "list_issues", "arguments": {"team": "platform"}},
        ),
        (
            "POST",
            "/v1/edit-sessions",
            {},
            {
                "goal": "Update client methods",
                "files": ["src/matterwave_tui/client.py", "src/matterwave_tui/app.py"],
            },
        ),
        ("GET", "/api/v1/runs", {"agent_id": "agent-ops", "status": "running"}, None),
    ]


@pytest.mark.asyncio
async def test_matterwave_client_delegates_to_sdk_methods_when_present() -> None:
    """When the SDK object provides Wave 7/8 methods, `MatterWaveClient` delegates directly."""
    calls: list[tuple[str, Any]] = []

    class FullSdkMock:
        base_url = "https://matterwave.test"

        async def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
            calls.append(("search_web", (query, max_results)))
            return {"results": [{"title": "From SDK", "url": "https://sdk.test", "snippet": query}]}

        async def query_rag(self, collection_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
            calls.append(("query_rag", (collection_id, query, top_k)))
            return {"results": [{"id": "c1", "text": query, "score": 0.99}]}

        async def list_rag_collections(self) -> dict[str, Any]:
            calls.append(("list_rag_collections", ()))
            return {"collections": [{"id": "col-sdk", "name": "SDK Collection"}]}

        async def list_mcp_servers(self) -> dict[str, Any]:
            calls.append(("list_mcp_servers", ()))
            return {"servers": [{"id": "srv-sdk", "name": "SDK MCP", "tools": []}]}

        async def call_mcp_tool(
            self, server_id: str, tool_name: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            calls.append(("call_mcp_tool", (server_id, tool_name, arguments)))
            return {"output": "ok"}

        async def create_edit_session(self, goal: str, files: list[str]) -> dict[str, Any]:
            calls.append(("create_edit_session", (goal, files)))
            return {"id": "es-sdk", "goal": goal, "files": files}

        async def list_agent_runs(
            self, agent_id: str | None = None, status: str | None = None
        ) -> dict[str, Any]:
            calls.append(("list_agent_runs", (agent_id, status)))
            return {"runs": [{"id": "run-sdk", "agent_id": agent_id, "status": status}]}

    client = MatterWaveClient(FullSdkMock(), token="mw_sdk")
    try:
        assert (await client.search_web("hello", max_results=2))["results"][0][
            "title"
        ] == "From SDK"
        assert (await client.list_rag_collections())["collections"][0]["id"] == "col-sdk"
        assert (await client.query_rag("col-sdk", "architecture", top_k=3))["results"][0][
            "id"
        ] == "c1"
        assert (await client.list_mcp_servers())["servers"][0]["id"] == "srv-sdk"
        assert (await client.call_mcp_tool("srv-sdk", "ping", {"x": 1}))["output"] == "ok"
        assert (await client.create_edit_session("fix bug", ["a.py"]))["id"] == "es-sdk"
        assert (await client.list_agent_runs(agent_id="a1", status="completed"))["runs"][0][
            "id"
        ] == "run-sdk"
    finally:
        await client.aclose()

    assert [name for name, _ in calls] == [
        "search_web",
        "list_rag_collections",
        "query_rag",
        "list_mcp_servers",
        "call_mcp_tool",
        "create_edit_session",
        "list_agent_runs",
    ]


@pytest.mark.asyncio
async def test_interactive_slash_commands_search_rag_mcp_runs_and_help() -> None:
    """Verify `/search`, `/rag`, `/mcp`, `/runs`, and `/help` in `MatterWaveApp`."""
    for cmd in ("/search", "/rag", "/mcp", "/runs", "/help"):
        assert cmd in SLASH_COMMANDS
        assert cmd in SLASH_COMMAND_DESCRIPTIONS
        assert any(spec.name == cmd for spec in SLASH_COMMAND_SPECS)

    api = FakeAPI()
    app = MatterWaveApp(cast(ChatAPI, api))
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        composer = app.query_one("#composer", TextArea)

        # 1. `/search` without query warns with usage
        composer.text = "/search"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        assert any("Usage: /search <query>" in s for s in shown)

        # 2. `/search <query>` calls `search_web` and renders citations/snippets into transcript
        shown.clear()
        composer.text = "/search matterwave terminal citations"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        assert api.web_searches == [("matterwave terminal citations", 5)]
        views = list(app.query(MessageView))
        assert views
        last_text = views[-1].message.content
        assert "MatterWave Docs" in last_text
        assert "https://docs.matterwave.test/search" in last_text
        assert "Documentation for MatterWave web search and citations." in last_text

        # 3. `/rag` with no args lists RAG collections
        shown.clear()
        composer.text = "/rag"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "RAG collections (1):" in last_text
        assert "col-docs: Engineering Docs (12 docs)" in last_text

        # 4. `/rag <collection_id> <query>` queries the RAG collection and renders chunks
        shown.clear()
        composer.text = "/rag col-docs architecture pipeline"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        assert api.rag_queries == [("col-docs", "architecture pipeline", 5)]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "RAG results from col-docs" in last_text
        assert "architecture.md" in last_text
        assert "Architecture overview for the RAG pipeline." in last_text

        # 5. `/mcp` lists registered MCP servers and their tools
        shown.clear()
        composer.text = "/mcp"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "MCP servers (1):" in last_text
        assert "srv-github (GitHub MCP) [connected]" in last_text
        assert "list_issues" in last_text

        # 6. `/mcp <server_id>` filters by server ID
        shown.clear()
        composer.text = "/mcp srv-github"
        app.action_send()
        await _settle(app, pilot)
        views = list(app.query(MessageView))
        assert "srv-github (GitHub MCP)" in views[-1].message.content

        # 7. `/runs` and `/runs <status>` list Agent Command Center runs
        shown.clear()
        composer.text = "/runs"
        app.action_send()
        await _settle(app, pilot)
        assert api.agent_runs_queries[-1] == (None, None)
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "run-101 | agent=agent-coder | status=running" in last_text
        assert "run-100 | agent=agent-reviewer | status=completed" in last_text

        shown.clear()
        composer.text = "/runs running"
        app.action_send()
        await _settle(app, pilot)
        assert api.agent_runs_queries[-1] == (None, "running")
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "run-101 | agent=agent-coder | status=running" in last_text
        assert "run-100" not in last_text

        # 8. `/help` opens HelpScreen and includes all new slash commands in `help_rows()`
        help_entries = {usage: desc for usage, desc, _ in app.help_rows()}
        assert "/search <query>" in help_entries
        assert "/rag [collection_id] <query>" in help_entries
        assert "/mcp [server_id]" in help_entries
        assert "/runs [status]" in help_entries
        assert "/help" in help_entries

        composer.text = "/help"
        app.action_send()
        await _settle(app, pilot)
        assert isinstance(app.screen, HelpScreen)
