"""Opening a `code` session: the checks and the loading both front ends
share (the interactive screen and the headless `-p` run).

Opening reads, in order: whether the folder is trusted, the settings (the
project's only when it is), `/api/me` and `/api/models` (is the agent
allowed, can the model call tools; a field an older server does not send
allows it), the context files, the subagent definitions, the MCP servers,
and, for `--continue` / `--resume`, the stored run to pick up.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matterwave_sdk import agent as contract
from matterwave_sdk.errors import APIConnectionError, APIError

from .checkpoints import Checkpoints
from .context import load_context, project_context_present
from .extras import load_subagents
from .loop import AgentSession, RunEvents
from .mcp import McpManager
from .settings import (
    AgentSettings,
    SandboxConfig,
    folder_trust,
    load_settings,
    record_run,
    recorded_runs,
)
from .shell import sandbox_available
from .wire import AgentWire, model_row, pick_model, stored_run_messages, unavailable_reason


@dataclass
class SessionOptions:
    root: Path
    mode: str | None = None
    yolo: bool = False
    model: str | None = None
    max_turns: int | None = None
    max_tokens: int | None = None
    trust: bool | None = None  # None: the stored answer
    resume: str | None = None
    continue_last: bool = False
    sandbox: str | None = None
    store: bool = True
    interactive: bool = True


@dataclass
class OpenedSession:
    session: AgentSession
    settings: AgentSettings
    trusted: bool
    trust_unknown: bool
    unavailable: str | None
    notices: list[str] = field(default_factory=list)
    models: dict[str, Any] = field(default_factory=dict)


class OpenError(RuntimeError):
    """The session cannot open at all (a bad option, a run that is not
    there); the message is for the user."""


def resolve_root(path: str | None) -> Path:
    root = Path(os.path.abspath(path or "."))
    if not root.is_dir():
        raise OpenError(f"{root} is not a folder.")
    return root


async def open_session(
    wire: AgentWire, options: SessionOptions, events: RunEvents | None = None
) -> OpenedSession:
    root = options.root
    stored = folder_trust(root)
    trusted = options.trust if options.trust is not None else stored == "trusted"
    trust_unknown = options.trust is None and stored is None and project_context_present(root)
    settings = load_settings(root, trusted=trusted)
    notices = list(settings.notices)

    mode = options.mode or settings.default_mode
    if mode not in contract.MODE_NAMES:
        raise OpenError(f"Unknown approval mode {mode!r}; one of {', '.join(contract.MODE_NAMES)}.")
    if contract.mode(mode).requires_flag and not options.yolo:
        raise OpenError(f"--approval-mode {mode} needs --yolo as well: it is never on by default.")

    sandbox = settings.sandbox
    if options.sandbox is not None:
        sandbox = SandboxConfig(engine=options.sandbox, image=sandbox.image, network=sandbox.network)
    reason = sandbox_available(sandbox)
    if reason is not None:
        notices.append(f"The sandbox ({sandbox.engine}) cannot run here: {reason}. Commands will be refused.")
    if options.yolo and sandbox.engine == "none":
        notices.append(
            "yolo is available this session and no sandbox is on: commands the model runs "
            "go ahead without asking and can reach anything you can. Consider a container "
            "(sandbox in settings, or --sandbox podman)."
        )

    me = None
    models: dict[str, Any] = {}
    try:
        me = await wire.me()
    except (APIError, APIConnectionError):
        me = None
    try:
        models = await wire.models()
    except (APIError, APIConnectionError):
        models = {}
    model = pick_model(models, options.model or settings.model)
    unavailable = unavailable_reason(me, models or None, model)

    context = load_context(root, trusted=trusted)
    notices.extend(context.notices)
    subagents = load_subagents(root, trusted=trusted)
    window = None
    row = model_row(models, model) if models else None
    if row is not None and isinstance(row.get("context_window"), int):
        window = int(row["context_window"])

    mcp = None
    if settings.mcp_servers or settings.matterwave_mcp:
        mcp = McpManager(
            settings.mcp_servers,
            cwd=str(root),
            matterwave=wire if settings.matterwave_mcp else None,
            matterwave_tools=settings.matterwave_mcp_tools,
        )

    session = AgentSession(
        wire,
        root,
        events=events,
        mode=mode,
        yolo_allowed=options.yolo,
        model=model,
        interactive=options.interactive,
        max_turns=options.max_turns or settings.max_turns,
        max_tokens=options.max_tokens or settings.max_tokens,
        context_window=window,
        context=context,
        store=options.store,
        checkpoints=Checkpoints(root) if Checkpoints.available() else None,
        mcp=mcp,
        subagents=subagents,
        sandbox=sandbox,
        web_fetch_enabled=settings.web_fetch,
        todo_enabled=settings.todo,
    )
    if session.checkpoints is None:
        notices.append("git is not on PATH, so file checkpoints (/restore) are off.")
    if session.holds_home:
        notices.append(
            "This workspace holds your home folder (shell profiles, ~/.ssh, startup items), so "
            "every file change asks, in every mode. Open the agent on a project folder instead."
        )

    resume_id = options.resume
    if options.continue_last and resume_id is None:
        runs = recorded_runs(root)
        if not runs:
            raise OpenError("There is no earlier run in this folder to continue.")
        resume_id = str(runs[0]["conversation_id"])
    if resume_id:
        await resume(session, resume_id)

    if mcp is not None and unavailable is None:
        notices.extend(await mcp.start())
    return OpenedSession(session, settings, trusted, trust_unknown, unavailable, notices, models)


async def resume(session: AgentSession, conversation_id: str) -> int:
    """Pick up a stored run: its active path, tool rounds included, becomes
    the session's history. The number of messages read back."""
    try:
        tree = await session.wire.tree(conversation_id)
    except (APIError, APIConnectionError) as exc:
        raise OpenError(f"The run {conversation_id} could not be read: {exc}") from exc
    session.messages[:] = stored_run_messages(tree)
    session.summary = None
    session.unreviewed.clear()
    session.conversation_id = conversation_id
    session.title = tree.title
    await session.load_account_instructions()
    return len(session.messages)


def remember_run(session: AgentSession) -> None:
    if session.conversation_id is None:
        return
    record_run(
        session.root,
        session.conversation_id,
        session.title or "Agent run",
        datetime.now(UTC).isoformat(timespec="seconds"),
    )
