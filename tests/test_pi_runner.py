"""Tests for Pi 1.0 (`pi` / `pi --mode rpc`) runner, environment/argv builders,
`PiRpcBridge` JSON-Lines subprocess communication, and the `matterwave-tui pi`
CLI subcommand."""

from __future__ import annotations

import subprocess
import sys

import pytest

from matterwave_tui import cli
from matterwave_tui.pi_runner import (
    PiRpcBridge,
    build_pi_argv,
    build_pi_launch_env,
    find_pi_binary,
)

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

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
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
