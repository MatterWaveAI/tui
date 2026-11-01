"""Where agent mode keeps what it remembers, and the settings it reads.

Two directories, both movable by environment for tests and for people who
keep dotfiles elsewhere:

- the state directory (`MATTERWAVE_STATE_DIR`, else `XDG_STATE_HOME`, else
  `~/.local/state`) + `matterwave-tui/`: the trusted-folder answers, the
  runs started per folder (for `--continue` and `/resume`) and the file
  checkpoints. The app's other state (roles, dismissed announcements) is
  there already.
- the config directory (`MATTERWAVE_CONFIG_DIR`, else `XDG_CONFIG_HOME`,
  else `~/.config`) + `matterwave/`: the user's `settings.json`, and the
  `commands/` and `agents/` folders of Markdown definitions.

A project adds `.matterwave/settings.json`, `.matterwave/commands/` and
`.matterwave/agents/` under its root. Those come from the repository, which
anyone may have written, so they are read only in a folder the user trusts:
a project's settings can start an MCP server, which is a program.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from matterwave_sdk import agent as contract

PROJECT_DIR = ".matterwave"
SETTINGS_FILE = "settings.json"
TRUST_FILE = "trusted-folders.json"
RUNS_FILE = "agent-runs.json"

# A run's ceilings when nothing narrower is given. Every run has both: a
# headless run in CI is where a loop would otherwise go unwatched.
DEFAULT_MAX_TURNS = 40
DEFAULT_MAX_TOKENS = 400_000

Trust = Literal["trusted", "untrusted"]


def state_dir() -> Path:
    root = os.environ.get("MATTERWAVE_STATE_DIR")
    if not root:
        xdg = os.environ.get("XDG_STATE_HOME")
        root = xdg if xdg else str(Path.home() / ".local" / "state")
    return Path(root) / "matterwave-tui"


def config_dir() -> Path:
    root = os.environ.get("MATTERWAVE_CONFIG_DIR")
    if not root:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        root = xdg if xdg else str(Path.home() / ".config")
    return Path(root) / "matterwave"


def folder_key(root: Path) -> str:
    """One spelling per folder, so `C:\\X` and `c:/x` are the same answer."""
    return os.path.normcase(os.path.realpath(root))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: Any) -> bool:
    try:
        # The user's alone: which folders they trust, which runs they had.
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        return False
    return True


# --- trusted folders -----------------------------------------------------------


def folder_trust(root: Path) -> Trust | None:
    """The user's answer for this folder, or None when never asked."""
    rows = _read_json(state_dir() / TRUST_FILE)
    if not isinstance(rows, dict):
        return None
    value = rows.get(folder_key(root))
    return value if value in ("trusted", "untrusted") else None


def set_folder_trust(root: Path, trust: Trust) -> bool:
    path = state_dir() / TRUST_FILE
    rows = _read_json(path)
    rows = dict(rows) if isinstance(rows, dict) else {}
    rows[folder_key(root)] = trust
    return _write_json(path, rows)


# --- the runs started in a folder ------------------------------------------------


def recorded_runs(root: Path) -> list[dict[str, Any]]:
    """The stored runs started in this folder, newest first: each a
    `conversation_id`, a `title` and when it `started`."""
    rows = _read_json(state_dir() / RUNS_FILE)
    if not isinstance(rows, dict):
        return []
    runs = rows.get(folder_key(root))
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict) and isinstance(r.get("conversation_id"), str)]


def record_run(root: Path, conversation_id: str, title: str, started: str) -> None:
    path = state_dir() / RUNS_FILE
    rows = _read_json(path)
    rows = dict(rows) if isinstance(rows, dict) else {}
    key = folder_key(root)
    runs = [r for r in recorded_runs(root) if r.get("conversation_id") != conversation_id]
    runs.insert(0, {"conversation_id": conversation_id, "title": title, "started": started})
    rows[key] = runs[:50]
    _write_json(path, rows)


# --- settings -------------------------------------------------------------------


@dataclass(frozen=True)
class McpServerConfig:
    """One MCP server: a program spoken to over stdio (`command`) or an
    endpoint over Streamable HTTP (`url`). `tools` is the allow-list (None
    lets every tool through; a tool outside it is never offered to the
    model); `trust` lets the server's writes run without asking in `ask`
    and `auto-edit` mode."""

    name: str
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    tools: tuple[str, ...] | None = None
    exclude_tools: tuple[str, ...] = ()
    trust: bool = False
    timeout_s: float = 60.0
    source: Literal["user", "project", "builtin"] = "user"


@dataclass(frozen=True)
class SandboxConfig:
    """Where `run_shell` runs. `engine` is `none` (the default: commands
    run as the user), `podman` or `docker` (a container with only the root
    mounted, read-only elsewhere and with no network unless `network`),
    `bwrap` (Linux) or `seatbelt` (macOS)."""

    engine: str = "none"
    image: str = "docker.io/library/python:3.12-slim-bookworm"
    network: bool = False


@dataclass(frozen=True)
class AgentSettings:
    default_mode: contract.ModeName = contract.DEFAULT_MODE
    model: str | None = None
    max_turns: int = DEFAULT_MAX_TURNS
    max_tokens: int = DEFAULT_MAX_TOKENS
    mcp_servers: tuple[McpServerConfig, ...] = ()
    matterwave_mcp: bool = True
    matterwave_mcp_tools: tuple[str, ...] | None = None
    sandbox: SandboxConfig = SandboxConfig()
    web_fetch: bool = True
    todo: bool = True
    notices: tuple[str, ...] = ()


def _str_tuple(value: Any) -> tuple[str, ...] | None:
    if not isinstance(value, list):
        return None
    return tuple(str(v) for v in value if isinstance(v, str))


def _str_map(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(v, (str, int, float))}


def _server(name: str, raw: Mapping[str, Any], source: Literal["user", "project"]) -> McpServerConfig:
    timeout = raw.get("timeout_s")
    return McpServerConfig(
        name=name,
        command=raw["command"] if isinstance(raw.get("command"), str) else None,
        args=_str_tuple(raw.get("args")) or (),
        env=_str_map(raw.get("env")),
        cwd=raw["cwd"] if isinstance(raw.get("cwd"), str) else None,
        url=raw["url"] if isinstance(raw.get("url"), str) else None,
        headers=_str_map(raw.get("headers")),
        tools=_str_tuple(raw.get("tools")),
        exclude_tools=_str_tuple(raw.get("exclude_tools")) or (),
        trust=raw.get("trust") is True,
        timeout_s=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 60.0,
        source=source,
    )


def user_settings_path() -> Path:
    return config_dir() / SETTINGS_FILE


def project_settings_path(root: Path) -> Path:
    return root / PROJECT_DIR / SETTINGS_FILE


def load_settings(root: Path, *, trusted: bool) -> AgentSettings:
    """The user's settings with the project's on top, the project's only
    when the folder is trusted. Unreadable files read as empty. A saved
    `yolo` default is ignored with a notice: it needs a flag each session."""
    notices: list[str] = []
    layers: list[tuple[Literal["user", "project"], dict[str, Any]]] = []
    user = _read_json(user_settings_path())
    if isinstance(user, dict):
        layers.append(("user", user))
    if trusted:
        project = _read_json(project_settings_path(root))
        if isinstance(project, dict):
            layers.append(("project", project))

    values: dict[str, Any] = {}
    servers: dict[str, McpServerConfig] = {}
    sandbox: dict[str, Any] = {}
    for source, layer in layers:
        agent_part = layer.get("agent")
        if isinstance(agent_part, dict):
            values.update(agent_part)
        mcp = layer.get("mcpServers")
        if isinstance(mcp, dict):
            for name, raw in mcp.items():
                if isinstance(raw, dict) and raw.get("enabled", True) is not False:
                    servers[str(name)] = _server(str(name), raw, source)
                elif isinstance(raw, dict):
                    servers.pop(str(name), None)
        own = layer.get("matterwaveMcp")
        if isinstance(own, dict):
            if "enabled" in own:
                values["matterwave_mcp"] = own.get("enabled") is not False
            if "tools" in own:
                values["matterwave_mcp_tools"] = _str_tuple(own.get("tools"))
        box = layer.get("sandbox")
        if isinstance(box, dict):
            sandbox.update(box)
        elif isinstance(box, str):
            sandbox["engine"] = box

    mode = values.get("default_mode", contract.DEFAULT_MODE)
    if mode not in contract.MODE_NAMES:
        notices.append(f"Settings name an unknown approval mode {mode!r}; using ask.")
        mode = contract.DEFAULT_MODE
    elif not contract.mode(str(mode)).persistable:
        notices.append(
            f"Settings ask for {mode} as the default mode, which is never saved: "
            "it needs --yolo each session. Using ask."
        )
        mode = contract.DEFAULT_MODE

    def positive(key: str, default: int) -> int:
        value = values.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else default

    engine = str(sandbox.get("engine") or "none")
    return AgentSettings(
        default_mode=mode,
        model=values["model"] if isinstance(values.get("model"), str) else None,
        max_turns=positive("max_turns", DEFAULT_MAX_TURNS),
        max_tokens=positive("max_tokens", DEFAULT_MAX_TOKENS),
        mcp_servers=tuple(servers.values()),
        matterwave_mcp=values.get("matterwave_mcp", True) is not False,
        matterwave_mcp_tools=values.get("matterwave_mcp_tools"),
        sandbox=SandboxConfig(
            engine=engine,
            image=str(sandbox.get("image") or SandboxConfig.image),
            network=sandbox.get("network") is True,
        ),
        web_fetch=values.get("web_fetch", True) is not False,
        todo=values.get("todo", True) is not False,
        notices=tuple(notices),
    )


def save_default_mode(mode: str) -> str | None:
    """Keep `mode` as the user's default. Returns the reason it was not
    kept: an unknown mode, or one the contract marks not persistable
    (`yolo`), which is never written."""
    if mode not in contract.MODE_NAMES:
        return f"unknown approval mode {mode!r}"
    if not contract.mode(mode).persistable:
        return f"{mode} is never saved as a default; it needs --yolo each session"
    path = user_settings_path()
    raw = _read_json(path)
    data = dict(raw) if isinstance(raw, dict) else {}
    agent_part = data.get("agent")
    agent_part = dict(agent_part) if isinstance(agent_part, dict) else {}
    agent_part["default_mode"] = mode
    data["agent"] = agent_part
    if not _write_json(path, data):
        return "the settings file could not be written"
    return None
