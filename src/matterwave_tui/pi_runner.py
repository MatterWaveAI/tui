"""Pi 1.0 (`pi` / `pi --mode rpc`) runner and async JSON-Lines RPC bridge.

Pre-configures Pi 1.0 with the active MatterWave deployment URL, bearer
credential, model, preset, and temporary-session state so `pi` can run
interactively or as a headless JSON-Lines subprocess driven by the TUI.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

PI_MODES = ("interactive", "rpc", "json", "print")


@dataclass(frozen=True)
class PiRpcEvent:
    """A normalized event emitted by `pi --mode rpc` over stdout JSON-Lines."""

    type: str
    text: str = ""
    tool_name: str | None = None
    tool_call_id: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: Any = None
    is_error: bool = False
    error: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            val = getattr(self, key)
            return default if val is None else val
        return self.raw.get(key, default)


def find_pi_binary(environ: Mapping[str, str] | None = None) -> str | None:
    """Locate the `pi` executable via `MATTERWAVE_PI_BIN` or `PATH`."""
    env = environ if environ is not None else os.environ
    override = env.get("MATTERWAVE_PI_BIN", "").strip()
    if override:
        return override
    return shutil.which("pi", path=env.get("PATH"))


def build_pi_launch_env(
    api_key: str,
    base_url: str,
    *,
    preset_id: str | None = None,
    model: str | None = None,
    temporary: bool = False,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build environment variables so `pi` authenticates against MatterWave."""
    env = dict(base_env if base_env is not None else os.environ)
    env["MATTERWAVE_API_KEY"] = api_key
    env["MATTERWAVE_BASE_URL"] = base_url

    if preset_id:
        env["MATTERWAVE_PRESET_ID"] = preset_id
    else:
        env.pop("MATTERWAVE_PRESET_ID", None)

    if model:
        env["MATTERWAVE_MODEL"] = model
    else:
        env.pop("MATTERWAVE_MODEL", None)

    if temporary:
        env["MATTERWAVE_TEMPORARY"] = "1"
    else:
        env.pop("MATTERWAVE_TEMPORARY", None)

    return env


def build_pi_argv(
    pi_bin: str = "pi",
    *,
    mode: str = "interactive",
    model: str | None = None,
    provider: str = "matterwave",
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build the command line for interactive `pi` or `pi --mode rpc`."""
    if mode not in PI_MODES:
        raise ValueError(f"Unsupported pi mode {mode!r}; expected one of {PI_MODES}")

    argv = [pi_bin]
    if mode != "interactive":
        argv.extend(["--mode", mode])
    if provider:
        argv.extend(["--provider", provider])
    if model:
        argv.extend(["--model", model])
    if extra_args:
        argv.extend(extra_args)
    return argv


def parse_pi_rpc_event(payload: dict[str, Any]) -> PiRpcEvent | None:
    """Normalize a raw JSON object from `pi --mode rpc` into a `PiRpcEvent`."""
    event_type = str(payload.get("type") or "")
    if not event_type:
        return None

    # Direct or native Pi RPC text delta
    if event_type == "text_delta":
        delta = str(payload.get("delta") or payload.get("text") or "")
        return PiRpcEvent(type="text_delta", text=delta, raw=payload)

    if event_type == "message_update":
        sub = payload.get("assistantMessageEvent")
        if isinstance(sub, dict):
            sub_type = str(sub.get("type") or "")
            if sub_type == "text_delta":
                return PiRpcEvent(
                    type="text_delta",
                    text=str(sub.get("delta") or sub.get("text") or ""),
                    raw=payload,
                )
            if sub_type == "error":
                err_msg = str(sub.get("error") or sub.get("message") or "stream error")
                return PiRpcEvent(type="error", is_error=True, error=err_msg, raw=payload)
        return None

    # Tool execution start
    if event_type in ("tool_start", "tool_execution_start"):
        tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
        call_id = payload.get("tool_call_id") or payload.get("toolCallId") or payload.get("id")
        args = payload.get("args") or payload.get("tool_args") or payload.get("arguments")
        return PiRpcEvent(
            type="tool_start",
            tool_name=tool_name or None,
            tool_call_id=str(call_id) if call_id is not None else None,
            tool_args=dict(args) if isinstance(args, Mapping) else None,
            raw=payload,
        )

    # Tool execution end
    if event_type in ("tool_end", "tool_execution_end"):
        tool_name = str(payload.get("tool_name") or payload.get("toolName") or "")
        call_id = payload.get("tool_call_id") or payload.get("toolCallId") or payload.get("id")
        is_err = bool(payload.get("is_error") or payload.get("isError"))
        return PiRpcEvent(
            type="tool_end",
            tool_name=tool_name or None,
            tool_call_id=str(call_id) if call_id is not None else None,
            tool_result=payload.get("result"),
            is_error=is_err,
            raw=payload,
        )

    # Turn / agent completion
    if event_type in ("turn_end", "agent_end"):
        return PiRpcEvent(type="turn_end", raw=payload)

    # Command response acknowledgement or RPC error
    if event_type == "response":
        if payload.get("success") is False:
            err_msg = str(payload.get("error") or "RPC command failed")
            return PiRpcEvent(type="error", is_error=True, error=err_msg, raw=payload)
        return PiRpcEvent(type="response", raw=payload)

    if event_type == "error":
        err_msg = str(payload.get("error") or payload.get("message") or "unknown error")
        return PiRpcEvent(type="error", is_error=True, error=err_msg, raw=payload)

    return PiRpcEvent(type=event_type, raw=payload)


class PiRpcBridge:
    """Async JSON-Lines (`stdin`/`stdout`) bridge for `pi --mode rpc`."""

    def __init__(
        self,
        argv: Sequence[str] | None = None,
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
    ) -> None:
        if argv is None:
            pi_bin = find_pi_binary(env) or "pi"
            argv = build_pi_argv(pi_bin, mode="rpc")
        self._argv = list(argv)
        self._env = dict(env) if env is not None else None
        self._cwd = str(cwd) if cwd is not None else None
        self._process: asyncio.subprocess.Process | None = None

    @property
    def process(self) -> asyncio.subprocess.Process | None:
        return self._process

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> None:
        """Spawn the underlying `pi --mode rpc` process if not already running."""
        if self.running:
            return
        self._process = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            cwd=self._cwd,
        )

    async def __aenter__(self) -> PiRpcBridge:
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def send_command(self, command: Mapping[str, Any]) -> None:
        """Write a single JSON-Lines command to the child's stdin."""
        if not self.running:
            await self.start()
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("pi --mode rpc process stdin is not available")
        line = json.dumps(dict(command), separators=(",", ":")) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

    async def read_event(self) -> PiRpcEvent | None:
        """Read the next parsed `PiRpcEvent` from the child's stdout, or `None` on EOF."""
        if self._process is None or self._process.stdout is None:
            return None
        while True:
            raw_line = await self._process.stdout.readline()
            if not raw_line:
                return None
            text = raw_line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            event = parse_pi_rpc_event(data)
            if event is not None:
                return event

    async def prompt(self, message: str) -> AsyncIterator[PiRpcEvent]:
        """Send `{"type": "prompt", "message": ...}` and yield events until `turn_end` or `error`."""
        await self.send_command({"type": "prompt", "message": message})
        while True:
            event = await self.read_event()
            if event is None:
                break
            if event.type == "response":
                continue
            yield event
            if event.type in ("turn_end", "error"):
                break

    async def abort(self) -> None:
        """Send `{"type": "abort"}` to interrupt an active turn."""
        if not self.running:
            return
        await self.send_command({"type": "abort"})

    async def _await_rpc_response(self, *extra_types: str) -> PiRpcEvent | None:
        accepted = {"response", "error", *extra_types}
        while True:
            event = await self.read_event()
            if event is None or event.type in accepted:
                return event

    async def set_model(self, model: str, *, provider: str = "matterwave") -> PiRpcEvent | None:
        """Send `{"type": "set_model", ...}` to switch the active model in `pi --mode rpc`."""
        await self.send_command(
            {
                "type": "set_model",
                "provider": provider,
                "modelId": model,
                "model": model,
            }
        )
        return await self._await_rpc_response("model_changed")

    async def compact(self, custom_instructions: str | None = None) -> PiRpcEvent | None:
        """Send `{"type": "compact", ...}` to compact the active Pi 1.0 RPC session."""
        command: dict[str, Any] = {"type": "compact"}
        if custom_instructions:
            command["customInstructions"] = custom_instructions
            command["instructions"] = custom_instructions
        await self.send_command(command)
        return await self._await_rpc_response("compacted", "auto_compaction_end")

    async def get_state(self) -> PiRpcEvent | None:
        """Send `{"type": "get_state"}` to inspect the active Pi 1.0 RPC session state."""
        await self.send_command({"type": "get_state"})
        return await self._await_rpc_response("state")

    async def new_session(self) -> PiRpcEvent | None:
        """Send `{"type": "new_session"}` to reset the active Pi 1.0 RPC session."""
        await self.send_command({"type": "new_session"})
        return await self._await_rpc_response("session_reset")

    async def steer(self, message: str) -> PiRpcEvent | None:
        """Send `{"type": "steer", "message": ...}` to steer an active Pi 1.0 turn."""
        await self.send_command({"type": "steer", "message": message})
        return await self._await_rpc_response()

    async def follow_up(self, message: str) -> PiRpcEvent | None:
        """Send `{"type": "follow_up", "message": ...}` to queue a follow-up prompt in Pi 1.0."""
        await self.send_command({"type": "follow_up", "message": message})
        return await self._await_rpc_response()

    async def close(self) -> None:
        """Close stdin and terminate the subprocess cleanly."""
        proc = self._process
        self._process = None
        if proc is None:
            return
        if proc.stdin is not None:
            with contextlib.suppress(Exception):
                proc.stdin.close()
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
