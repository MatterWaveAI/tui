"""The agent's pure pieces: a stored run read back as a prompt, the MCP
HTTP transport, web fetch, Markdown front matter and the settings."""

from __future__ import annotations

import json

import httpx
import pytest
from matterwave_sdk.models import Conversation

from matterwave_tui.agent import settings as agent_settings
from matterwave_tui.agent.context import parse_front_matter
from matterwave_tui.agent.extras import html_to_text, web_fetch
from matterwave_tui.agent.mcp import HttpServer, McpManager, qualified_name
from matterwave_tui.agent.settings import McpServerConfig
from matterwave_tui.agent.wire import UNANSWERED_CALL, stored_run_messages


def message(mid: str, role: str, parts: list[dict], parent: str | None, content: str = "") -> dict:
    return {"id": mid, "conversation_id": "c", "role": role, "parts": parts, "content": content,
            "created_at": mid, "parent_id": parent}


def test_a_stored_run_reads_back_as_rounds_with_a_stand_in_for_an_unanswered_call():
    tree = Conversation.from_dict({
        "id": "c", "title": "t", "created_at": "1", "updated_at": "1",
        "current_leaf_message_id": "m3",
        "messages": [
            message("m1", "user", [{"type": "text", "text": "fix it"}], None, "fix it"),
            message("m2", "assistant", [
                {"type": "text", "text": "Looking."},
                {"type": "tool_call", "id": "a", "name": "read_file", "arguments": '{"path": "x"}'},
                {"type": "tool_call", "id": "b", "name": "grep", "arguments": '{"pattern": "y"}'},
                {"type": "tool_result", "id": "a", "output": "1\tx"},
                {"type": "tool_result", "id": "b", "output": "x:1: y"},
                {"type": "tool_call", "id": "c", "name": "run_shell", "arguments": '{"command": "t"}'},
            ], "m1"),
            message("m3", "user", [{"type": "text", "text": "go on"}], "m2", "go on"),
        ],
    })
    messages = stored_run_messages(tree)
    assert [m["role"] for m in messages] == ["user", "assistant", "tool", "tool", "assistant", "tool", "user"]
    assert messages[1]["content"] == "Looking."
    assert [c["id"] for c in messages[1]["tool_calls"]] == ["a", "b"]
    assert messages[2] == {"role": "tool", "tool_call_id": "a", "content": "1\tx"}
    assert messages[4]["content"] is None and messages[4]["tool_calls"][0]["id"] == "c"
    assert messages[5]["content"] == UNANSWERED_CALL


def test_a_compaction_summary_stands_in_for_what_it_covers():
    tree = Conversation.from_dict({
        "id": "c", "title": "t", "created_at": "1", "updated_at": "1", "current_leaf_message_id": "m4",
        "messages": [
            message("m1", "user", [{"type": "text", "text": "old"}], None, "old"),
            message("m2", "assistant", [{"type": "text", "text": "old answer"}], "m1", "old answer"),
            message("m3", "system", [{"type": "summary", "text": "Earlier: things.", "covers_from": "m1",
                                      "covers_to": "m2"}], "m2"),
            message("m4", "user", [{"type": "text", "text": "new"}], "m3", "new"),
        ],
    })
    messages = stored_run_messages(tree)
    assert [m["role"] for m in messages] == ["user", "user"]
    assert "Earlier: things." in messages[0]["content"]
    assert messages[1]["content"] == "new"


@pytest.mark.asyncio
async def test_the_http_transport_reads_json_and_sse_answers_and_keeps_the_session():
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        if body["method"] == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": {}},
                                  headers={"mcp-session-id": "s-1"})
        if body["method"] == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"],
                                             "result": {"tools": [{"name": "echo"}, {"name": "hidden"}]}})
        event = json.dumps({"jsonrpc": "2.0", "id": body["id"],
                            "result": {"content": [{"type": "text", "text": "echoed"}]}})
        return httpx.Response(200, text=f"event: message\ndata: {event}\n\n",
                              headers={"content-type": "text/event-stream"})

    config = McpServerConfig(name="web", url="https://mcp.test/mcp", headers={"Authorization": "Bearer ${MW_T}"},
                             tools=("echo",))
    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    manager = McpManager((config,), cwd=".", factory=lambda c: HttpServer(c, client))
    notices = await manager.start()
    assert notices == []
    assert list(manager.tools) == ["mcp__web__echo"]
    assert await manager.call("mcp__web__echo", {"x": 1}) == ("echoed", False)
    assert seen[-1].headers["mcp-session-id"] == "s-1"
    assert seen[-1].headers["accept"] == "application/json, text/event-stream"
    await manager.close()


@pytest.mark.asyncio
async def test_a_server_that_fails_to_start_is_a_notice_not_a_crash():
    config = McpServerConfig(name="broken", command="definitely-not-a-program-mw")
    manager = McpManager((config,), cwd=".")
    notices = await manager.start()
    assert len(notices) == 1 and "broken is unavailable" in notices[0]
    assert manager.tools == {}


def test_tool_names_are_qualified_and_safe():
    assert qualified_name("my server", "do.it") == "mcp__my_server__do_it"
    assert len(qualified_name("s" * 80, "t")) == 64


@pytest.mark.asyncio
async def test_web_fetch_reduces_html_to_text_and_refuses_binary():
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, text="<html><script>x()</script><h1>Title</h1><p>Body &amp; more</p></html>",
                                  headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"\x89PNG", headers={"content-type": "image/png"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    text, failed = await web_fetch("https://site.test/page", client=client)
    assert not failed and text.endswith("Title\nBody & more") and "x()" not in text
    text, failed = await web_fetch("https://site.test/image", client=client)
    assert failed and "not text" in text
    text, failed = await web_fetch("ftp://site.test/x", client=client)
    assert failed and "http:// or https://" in text
    assert html_to_text("<p>a</p><p>b</p>") == "a\nb"


def test_front_matter_takes_strings_and_both_list_forms():
    meta, body = parse_front_matter("---\nname: x\ntools: [a, b]\nmore:\n  - c\n  - d\n---\nBody\n")
    assert meta == {"name": "x", "tools": ["a", "b"], "more": ["c", "d"]}
    assert body == "Body\n"
    assert parse_front_matter("no header") == ({}, "no header")


def test_project_settings_load_only_when_trusted(tmp_path):
    project = tmp_path / ".matterwave"
    project.mkdir()
    (project / "settings.json").write_text(json.dumps({
        "agent": {"max_turns": 7},
        "mcpServers": {"p": {"command": "x", "tools": ["a"], "trust": True}},
        "sandbox": "podman",
    }), encoding="utf-8")
    agent_settings.user_settings_path().parent.mkdir(parents=True, exist_ok=True)
    agent_settings.user_settings_path().write_text(json.dumps({
        "agent": {"max_tokens": 9000},
        "mcpServers": {"u": {"url": "https://u.test"}},
        "matterwaveMcp": {"enabled": False},
    }), encoding="utf-8")
    untrusted = agent_settings.load_settings(tmp_path, trusted=False)
    assert untrusted.max_turns == agent_settings.DEFAULT_MAX_TURNS and untrusted.max_tokens == 9000
    assert [s.name for s in untrusted.mcp_servers] == ["u"] and untrusted.sandbox.engine == "none"
    assert untrusted.matterwave_mcp is False
    trusted = agent_settings.load_settings(tmp_path, trusted=True)
    assert trusted.max_turns == 7 and trusted.sandbox.engine == "podman"
    server = {s.name: s for s in trusted.mcp_servers}["p"]
    assert server.tools == ("a",) and server.trust and server.source == "project"
