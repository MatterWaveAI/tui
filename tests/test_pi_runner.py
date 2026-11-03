"""Tests for Pi 1.0 (`pi` / `pi --mode rpc`) runner, environment/argv builders,
`PiRpcBridge` JSON-Lines subprocess communication, and the `matterwave-tui pi`
CLI subcommand."""

from __future__ import annotations

import subprocess
import sys
from typing import Any, cast

import pytest
from textual.widgets import Markdown, OptionList, Static, TextArea

from matterwave_tui import app as app_mod
from matterwave_tui import cli
from matterwave_tui.api import ChatAPI
from matterwave_tui.app import AGENT_ENGINES, SLASH_COMMANDS, MatterWaveApp
from matterwave_tui.pi_runner import (
    PiRpcBridge,
    build_pi_argv,
    build_pi_launch_env,
    find_pi_binary,
)
from matterwave_tui.screens import HelpScreen
from matterwave_tui.widgets import MessageView

from .fake_api import FakeAPI

MOCK_PI_RPC_SCRIPT = """
import json
import sys

current_model = "glm-4.6"

for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    cmd = json.loads(line)
    ctype = cmd.get("type")
    if ctype == "prompt":
        msg = cmd.get("message", "")
        if "error" in msg:
            sys.stdout.write(json.dumps({"type": "error", "error": "boom"}) + "\\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"type": "response", "command": "prompt", "success": True}) + "\\n")
        sys.stdout.write(json.dumps({
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": "Checking "}
        }) + "\\n")
        sys.stdout.write(json.dumps({
            "type": "tool_execution_start",
            "toolCallId": "tc_1",
            "toolName": "read",
            "args": {"path": "README.md"}
        }) + "\\n")
        sys.stdout.write(json.dumps({
            "type": "tool_execution_end",
            "toolCallId": "tc_1",
            "toolName": "read",
            "result": {"lines": 10},
            "isError": False
        }) + "\\n")
        sys.stdout.write(json.dumps({
            "type": "text_delta",
            "delta": f"with {current_model}: {msg}"
        }) + "\\n")
        sys.stdout.write(json.dumps({"type": "agent_end"}) + "\\n")
        sys.stdout.flush()
    elif ctype == "set_model":
        current_model = cmd.get("modelId") or cmd.get("model") or current_model
        sys.stdout.write(json.dumps({
            "type": "response",
            "command": "set_model",
            "success": True,
            "model": current_model,
            "provider": cmd.get("provider", "matterwave"),
        }) + "\\n")
        sys.stdout.flush()
    elif ctype == "abort":
        sys.stdout.write(json.dumps({"type": "response", "command": "abort", "success": True}) + "\\n")
        sys.stdout.write(json.dumps({"type": "turn_end", "aborted": True}) + "\\n")
        sys.stdout.flush()
"""


def test_find_pi_binary_respects_matterwave_pi_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MATTERWAVE_PI_BIN", "/custom/bin/pi-1.0")
    assert find_pi_binary() == "/custom/bin/pi-1.0"
    assert find_pi_binary({"MATTERWAVE_PI_BIN": "C:\\Tools\\pi.exe"}) == "C:\\Tools\\pi.exe"


def test_find_pi_binary_falls_back_to_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MATTERWAVE_PI_BIN", raising=False)
    monkeypatch.setattr(
        "matterwave_tui.pi_runner.shutil.which", lambda name, path=None: "/usr/local/bin/pi"
    )
    assert find_pi_binary({}) == "/usr/local/bin/pi"


def test_build_pi_launch_env_populates_and_clears_optional_vars() -> None:
    base = {
        "PATH": "/usr/bin",
        "MATTERWAVE_PRESET_ID": "stale-preset",
        "MATTERWAVE_MODEL": "stale-model",
        "MATTERWAVE_TEMPORARY": "1",
    }
    env = build_pi_launch_env(
        "mw_secret_key",
        "https://matterwave.example/api",
        preset_id="preset-research",
        model="glm-4.6",
        temporary=True,
        base_env=base,
    )
    assert env["MATTERWAVE_API_KEY"] == "mw_secret_key"
    assert env["MATTERWAVE_BASE_URL"] == "https://matterwave.example/api"
    assert env["MATTERWAVE_PRESET_ID"] == "preset-research"
    assert env["MATTERWAVE_MODEL"] == "glm-4.6"
    assert env["MATTERWAVE_TEMPORARY"] == "1"

    minimal = build_pi_launch_env(
        "mw_secret_key",
        "https://matterwave.example",
        base_env=base,
    )
    assert minimal["MATTERWAVE_API_KEY"] == "mw_secret_key"
    assert minimal["MATTERWAVE_BASE_URL"] == "https://matterwave.example"
    assert "MATTERWAVE_PRESET_ID" not in minimal
    assert "MATTERWAVE_MODEL" not in minimal
    assert "MATTERWAVE_TEMPORARY" not in minimal


def test_build_pi_argv_modes_and_options() -> None:
    assert build_pi_argv() == ["pi", "--provider", "matterwave"]
    assert build_pi_argv(
        "/opt/pi",
        mode="rpc",
        model="glm-4.6",
        provider="matterwave",
        extra_args=["--no-session"],
    ) == [
        "/opt/pi",
        "--mode",
        "rpc",
        "--provider",
        "matterwave",
        "--model",
        "glm-4.6",
        "--no-session",
    ]
    with pytest.raises(ValueError, match="Unsupported pi mode"):
        build_pi_argv(mode="invalid-mode")


@pytest.mark.asyncio
async def test_pi_rpc_bridge_prompt_tools_set_model_abort_and_shutdown() -> None:
    argv = [sys.executable, "-u", "-c", MOCK_PI_RPC_SCRIPT]
    async with PiRpcBridge(argv) as bridge:
        assert bridge.running

        events = [ev async for ev in bridge.prompt("hello world")]
        types = [ev.type for ev in events]
        assert types == ["text_delta", "tool_start", "tool_end", "text_delta", "turn_end"]
        assert events[0].text == "Checking "
        assert events[1].tool_name == "read"
        assert events[1].tool_call_id == "tc_1"
        assert events[1].tool_args == {"path": "README.md"}
        assert events[2].tool_name == "read"
        assert events[2].tool_result == {"lines": 10}
        assert events[2].is_error is False
        assert events[3].text == "with glm-4.6: hello world"

        ack = await bridge.set_model("kimi-k2.5")
        assert ack is not None
        assert ack.type == "response"
        assert ack.raw.get("model") == "kimi-k2.5"

        events_after = [ev async for ev in bridge.prompt("second turn")]
        assert [ev.text for ev in events_after if ev.type == "text_delta"] == [
            "Checking ",
            "with kimi-k2.5: second turn",
        ]

        await bridge.abort()
        abort_ack = await bridge.read_event()
        abort_end = await bridge.read_event()
        assert abort_ack is not None and abort_ack.type == "response"
        assert abort_end is not None and abort_end.type == "turn_end"

    assert not bridge.running


def test_cli_pi_subcommand_interactive_and_rpc_oneshot(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("MATTERWAVE_PI_BIN", sys.executable)
    recorded: dict[str, object] = {}

    def fake_run(
        argv: list[str], *, env: dict[str, str], check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        recorded["argv"] = argv
        recorded["env"] = env
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("matterwave_tui.cli.subprocess.run", fake_run)
    rc = cli.main(
        [
            "--base-url",
            "https://matterwave.test",
            "--key",
            "mw_k",
            "--temporary",
            "-m",
            "glm-4.6",
            "pi",
            "--preset",
            "preset-1",
        ]
    )
    assert rc == 0
    assert recorded["argv"] == [
        sys.executable,
        "--provider",
        "matterwave",
        "--model",
        "glm-4.6",
    ]
    env = recorded["env"]
    assert isinstance(env, dict)
    assert env["MATTERWAVE_API_KEY"] == "mw_k"
    assert env["MATTERWAVE_BASE_URL"] == "https://matterwave.test"
    assert env["MATTERWAVE_PRESET_ID"] == "preset-1"
    assert env["MATTERWAVE_MODEL"] == "glm-4.6"
    assert env["MATTERWAVE_TEMPORARY"] == "1"

    # Now test headless `matterwave-tui pi -p "..."` via PiRpcBridge
    monkeypatch.setattr(
        cli,
        "build_pi_argv",
        lambda pi_bin, **_: [sys.executable, "-u", "-c", MOCK_PI_RPC_SCRIPT],
    )
    rc2 = cli.main(
        [
            "--base-url",
            "https://matterwave.test",
            "--key",
            "mw_k",
            "pi",
            "-p",
            "from cli",
        ]
    )
    assert rc2 == 0
    assert capsys.readouterr().out == "Checking with glm-4.6: from cli\n"


async def _settle(app: MatterWaveApp, pilot: Any) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
async def test_interactive_tui_pi_command_streams_text_and_tools_into_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_mod,
        "find_pi_binary",
        lambda environ=None: sys.executable,
    )
    monkeypatch.setattr(
        app_mod,
        "build_pi_argv",
        lambda pi_bin, **_: [sys.executable, "-u", "-c", MOCK_PI_RPC_SCRIPT],
    )
    api = FakeAPI()
    app = MatterWaveApp(cast(ChatAPI, api))
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "/pi inspect README.md"
        app.action_send()
        await _settle(app, pilot)

        assert composer.text == ""
        assert api.sent == []

        views = list(app.query(MessageView))
        assert len(views) >= 2
        user_view, assistant_view = views[-2], views[-1]
        assert user_view.message.role == "user"
        assert user_view.message.content == "inspect README.md"

        assert assistant_view.message.role == "assistant"
        assert "Checking " in assistant_view.message.content
        assert "with glm-4.6: inspect README.md" in assistant_view.message.content
        part_types = [p.get("type") for p in assistant_view.message.parts]
        assert part_types == ["text", "tool_call", "tool_result", "text"]
        assert assistant_view.message.parts[1]["name"] == "read"
        assert "README.md" in assistant_view.message.parts[1]["arguments"]
        assert '"lines": 10' in assistant_view.message.parts[2]["output"]

        rendered_statics = [str(s.render()) for s in assistant_view.query(Static)]
        assert any("read(" in s and "README.md" in s for s in rendered_statics)
        assert any('"lines": 10' in s for s in rendered_statics)
        rendered_md = [md.source for md in assistant_view.query(Markdown)]
        assert any("with glm-4.6: inspect README.md" in m for m in rendered_md)

        # Error from Pi RPC is surfaced clearly
        composer.text = "/pi trigger error"
        app.action_send()
        await _settle(app, pilot)
        assert any("boom" in s for s in shown)


@pytest.mark.asyncio
async def test_interactive_tui_pi_command_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod, "find_pi_binary", lambda environ=None: None)
    api = FakeAPI()
    app = MatterWaveApp(cast(ChatAPI, api), environ={"PATH": ""})
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "/pi fix the build"
        app.action_send()
        await _settle(app, pilot)

        assert composer.text == ""
        assert any("pi binary not found on PATH" in s for s in shown)
        views = list(app.query(MessageView))
        assert views
        assert "pi binary not found on PATH" in views[-1].message.content


@pytest.mark.asyncio
async def test_interactive_tui_pi_engine_toggle_and_help_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_mod, "find_pi_binary", lambda environ=None: "/usr/local/bin/pi")
    api = FakeAPI()
    app = MatterWaveApp(cast(ChatAPI, api))
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    assert "/pi" in SLASH_COMMANDS
    assert "/pi-engine" in SLASH_COMMANDS
    assert AGENT_ENGINES == ("auto", "pi", "builtin")

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        assert app.agent_engine == "auto"

        composer = app.query_one("#composer", TextArea)
        composer.text = "/pi-engine"
        app.action_send()
        await _settle(app, pilot)
        assert any("Agent engine: auto (active: pi)" in s for s in shown)

        composer.text = "/pi-engine pi"
        app.action_send()
        await _settle(app, pilot)
        assert app.agent_engine == "pi"
        assert "engine pi" in app.status_text()
        assert any("Agent engine set to pi." in s for s in shown)

        composer.text = "/agent-engine builtin"
        app.action_send()
        await _settle(app, pilot)
        assert app.agent_engine == "builtin"
        assert "engine builtin" in app.status_text()

        composer.text = "/pi-engine invalid"
        app.action_send()
        await _settle(app, pilot)
        assert app.agent_engine == "builtin"
        assert any("Unknown agent engine" in s for s in shown)

        # Help overlay includes /pi and /pi-engine
        await pilot.press("escape", "f1")
        await _settle(app, pilot)
        assert isinstance(app.screen, HelpScreen)
        rows = app.screen.query_one("#help-rows", OptionList)
        lines = [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]
        assert any("/pi <prompt>" in line for line in lines)
        assert any("/pi-engine" in line for line in lines)
