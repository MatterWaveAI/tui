"""`matterwave-tui code [path]`: the agent as a screen of the app.

The screen is a log (prompts, the model's text, one line per tool call and
its result, notices), a task list when the model keeps one, a composer and
a status line that always names the approval mode. Shift+Tab cycles the
mode; Esc stops a run (the stream and any running command's process tree).
A write in `ask` mode opens `ApprovalScreen` with its unified diff; a
command opens it with the whole command. Nothing is applied until `y`.

A leading `/` in the composer is a command (`/help` lists them); a custom
command from a Markdown file runs as a prompt.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from matterwave_sdk import agent as contract
from matterwave_sdk.errors import APIConnectionError, APIError
from matterwave_sdk.models import ToolCall
from rich.syntax import Syntax
from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Markdown, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .checkpoints import CheckpointError
from .context import expand_mentions, load_commands, load_context
from .extras import load_subagents, render_todos
from .loop import AgentSession, ApprovalRequest, Budget, RunEvents, RunOutcome
from .printable import shown as safe_text
from .session import OpenedSession, OpenError, SessionOptions, open_session, remember_run, resume
from .settings import (
    folder_trust,
    load_settings,
    recorded_runs,
    save_default_mode,
    set_folder_trust,
)
from .shell import ProcessTree
from .tools import ToolOutcome
from .wire import AgentWire, model_rows, unavailable_reason

LIVE_REDRAW_S = 1 / 15
# A dialog that decides something ignores keys this long after it opens, so
# a key typed for something else cannot answer a question not yet read.
ANSWER_GRACE_S = 0.6
RESULT_PREVIEW_LINES = 3
INIT_PROMPT = (
    "Draft a MATTERWAVE.md for this repository: read its README, its build and test setup and "
    "its layout (glob, list_dir, read_file), then write MATTERWAVE.md at the root with "
    "write_file. Keep it short and specific to this repository: what it is, how to build, "
    "test and lint it (the exact commands), the layout, and the conventions a change must "
    "follow. Do not invent anything you did not read."
)

SLASH_HELP = """Commands
  /help                     this list
  /model [id]               pick the model (only ones that can call tools run the agent)
  /approval-mode [mode]     plan, ask, auto-edit (yolo with --yolo); add --save to keep it
  /compact                  fold old tool output, then summarise the run
  /context                  what is in the context: tokens, files, tools, tasks
  /restore [id]             list file checkpoints, or put one back
  /init                     draft a MATTERWAVE.md for this repository
  /resume [id]              list this folder's stored runs, or continue one
  /trust                    trust this folder (load its context files and settings)
  /mcp                      MCP servers and their tools
  /agents                   subagent definitions
  /pi <prompt>              run a prompt through Pi 1.0 RPC agent
  /pi-engine [engine]       inspect or switch agent engine (auto, pi, builtin)
  /pi-model [model]         switch or display the active Pi 1.0 model
  /pi-fork [entry_id]       fork the current Pi 1.0 session branch
  /pi-compact [instructions] compact the Pi 1.0 RPC session context
  /pi-status                inspect Pi 1.0 binary, engine, and session status
  /pi-new                   reset the Pi 1.0 RPC session
  /clear                    start a fresh run history
  /chat                     back to the chat view
Keys
  Shift+Tab   cycle the approval mode    Esc   stop the run
  Ctrl+Enter  send (also Ctrl+J, Ctrl+S)  @path attaches a file or folder"""


class LogLine(Static):
    DEFAULT_CSS = """
    LogLine { height: auto; padding: 0 1; }
    LogLine.user { color: $accent; text-style: bold; margin-top: 1; }
    LogLine.tool { color: $text-muted; }
    LogLine.result { color: $text-muted; padding-left: 3; }
    LogLine.error { color: $error; }
    LogLine.notice { color: $warning; }
    """


class Answer(Markdown):
    """The model's words once a reply is whole, kept as text too (the
    widget renders its source only once it is mounted)."""

    def __init__(self, text: str) -> None:
        text = safe_text(text)
        super().__init__(text, open_links=False)
        self.text = text

    def on_markdown_link_clicked(self, event: Markdown.LinkClicked) -> None:
        """Only a web link opens. Textual hands any link to the system's
        opener, and a `file:` link to a script the agent just wrote would run
        it on a click, with no approval."""
        event.stop()
        event.prevent_default()
        if urlsplit(event.href).scheme.lower() in ("http", "https"):
            self.app.open_url(event.href)
        else:
            self.notify(
                f"Not opened: {safe_text(event.href, strict=True)} (only http and https links "
                "open from the agent's answers).",
                severity="warning",
                markup=False,
            )


class Deliberate(ModalScreen[bool]):
    """A yes/no dialog that takes no answer in its first moments
    (`ANSWER_GRACE_S`): the composer may have had keys on their way."""

    def on_mount(self) -> None:
        self.opened_at = time.monotonic()

    @property
    def armed(self) -> bool:
        opened = getattr(self, "opened_at", None)
        return opened is not None and time.monotonic() - opened >= ANSWER_GRACE_S

    def answer(self, value: bool) -> None:
        if self.armed:
            self.dismiss(value)


class ApprovalScreen(Deliberate):
    """One approval: the change as a unified diff, or the whole command.
    `y` approves, `n` / Esc rejects. Enter is no answer: it is the key most
    likely to arrive from typing elsewhere."""

    BINDINGS = [
        Binding("y", "approve", "Approve"),
        Binding("n,escape", "reject", "Reject"),
    ]
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    ApprovalScreen > Vertical { width: 90%; height: 85%; border: round $warning;
        background: $surface; padding: 0 1; }
    #approval-body { height: 1fr; }
    #approval-mode { color: $text-muted; }
    #approval-note { color: $warning; }
    """

    def __init__(self, request: ApprovalRequest, mode: str) -> None:
        super().__init__()
        self.request = request
        self.mode = mode

    def compose(self) -> ComposeResult:
        request = self.request
        # Spelled out, not passed through: an escape sequence in a diff or a
        # command could otherwise redraw this dialog or set the clipboard.
        detail = safe_text(request.detail, strict=True)
        body: Any
        if request.kind in ("edit", "write"):
            body = Syntax(detail or "(no change)", "diff", word_wrap=True, theme="ansi_dark")
        elif request.kind == "shell":
            body = Syntax(detail, "bash", word_wrap=True, theme="ansi_dark")
        else:
            body = Text(detail)
        who = f" (subagent {request.agent})" if request.agent else ""
        title = safe_text(f"{request.title}{who}?", strict=True)
        with Vertical():
            yield Static(Text(title, style="bold"), id="approval-title")
            yield Static(
                f"mode {self.mode} │ y approves · n rejects", id="approval-mode", markup=False
            )
            if request.note:
                yield Static(safe_text(request.note, strict=True), id="approval-note", markup=False)
            with VerticalScroll(id="approval-body"):
                yield Static(body, id="approval-detail")

    def action_approve(self) -> None:
        self.answer(True)

    def action_reject(self) -> None:
        self.answer(False)


class PickScreen(ModalScreen[str | None]):
    """A list to choose one from (runs to resume, checkpoints to restore,
    models). Enter picks, Esc cancels."""

    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    PickScreen { align: center middle; }
    PickScreen > Vertical { width: 80%; max-width: 110; height: auto; max-height: 80%;
        border: round $accent; background: $surface; padding: 0 1; }
    """

    def __init__(self, title: str, rows: list[tuple[str, str]], mode: str) -> None:
        super().__init__()
        self.title_text = title
        self.rows = rows
        self.mode = mode

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(safe_text(f"{self.title_text}  (mode {self.mode})"), markup=False)
            # Titles and file names are not markup: a `[@click=...]` in a
            # conversation's title must not become an action.
            yield OptionList(
                *[
                    Option(safe_text(label, strict=True), id=f"pick-{i}")
                    for i, (_, label) in enumerate(self.rows)
                ],
                markup=False,
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        index = int((event.option.id or "pick-0").removeprefix("pick-"))
        self.dismiss(self.rows[index][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class TrustScreen(Deliberate):
    BINDINGS = [Binding("y", "yes", "Trust"), Binding("n,escape", "no", "Don't trust")]
    DEFAULT_CSS = """
    TrustScreen { align: center middle; }
    TrustScreen > Vertical { width: 76; height: auto; border: round $warning;
        background: $surface; padding: 1 2; }
    """

    def __init__(self, root: Path, mode: str) -> None:
        super().__init__()
        self.root = root
        self.mode = mode

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(
                f"Trust {safe_text(str(self.root), strict=True)}?\n\n"
                "This folder has its own MATTERWAVE.md / AGENTS.md or .matterwave settings. In a "
                "trusted folder they are loaded as instructions, and its settings can start MCP "
                "servers (programs). Trust only folders whose contents you know.\n\n"
                f"y trusts it (remembered) · n does not (remembered) · mode {self.mode}",
                markup=False,
            )

    def action_yes(self) -> None:
        self.answer(True)

    def action_no(self) -> None:
        self.answer(False)


class ScreenEvents(RunEvents):
    def __init__(self, screen: AgentScreen) -> None:
        self.screen = screen

    def on_text(self, delta: str, agent: str | None) -> None:
        self.screen.stream_text(delta, agent)

    def on_reply(self, content: str | None, calls: list[ToolCall], agent: str | None) -> None:
        self.screen.end_text()

    def on_tool_start(self, call: ToolCall, summary: str, agent: str | None) -> None:
        prefix = f"[{agent}] " if agent else ""
        self.screen.add_line(f"{prefix}⚙ {summary}", "tool", strict=True)

    def on_tool_end(self, call: ToolCall, outcome: ToolOutcome, agent: str | None) -> None:
        lines = outcome.text.splitlines() or [""]
        preview = "\n".join(lines[:RESULT_PREVIEW_LINES])
        if len(lines) > RESULT_PREVIEW_LINES:
            preview += f"\n… ({len(lines) - RESULT_PREVIEW_LINES} more lines)"
        mark = "✗ " if outcome.is_error else "↳ "
        self.screen.add_line(mark + preview, "result error" if outcome.is_error else "result")

    def on_notice(self, text: str) -> None:
        self.screen.add_line(text, "notice")

    def on_usage(self, budget: Budget) -> None:
        self.screen.render_status()

    def on_todos(self, todos: list[dict[str, str]]) -> None:
        self.screen.show_todos(todos)

    def on_mode(self, mode: str) -> None:
        self.screen.render_status()

    async def approve(self, request: ApprovalRequest) -> bool:
        return await self.screen.ask(request)


class AgentScreen(Screen[None]):
    """The coding agent in one folder."""

    BINDINGS = [
        Binding("ctrl+enter,ctrl+j,ctrl+s", "send", "Send", priority=True),
        Binding("shift+tab", "cycle_mode", "Mode", priority=True),
        Binding("escape", "stop", "Stop"),
    ]
    DEFAULT_CSS = """
    #agent-log { height: 1fr; }
    #agent-todos { display: none; height: auto; max-height: 8; border: round $panel;
        padding: 0 1; color: $text; }
    #agent-todos.shown { display: block; }
    #agent-composer { height: 5; border: round $panel; }
    #agent-composer:focus { border: round $accent; }
    #agent-status { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    """

    def __init__(
        self,
        wire: AgentWire,
        options: SessionOptions,
        *,
        leave: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.wire = wire
        self.options = options
        self.leave_callback = leave
        self.opened: OpenedSession | None = None
        self.session: AgentSession | None = None
        self.unavailable: str | None = None
        self.running = False
        # A command's worker (/compact, /restore, /resume) is changing the
        # history; a run waits for it.
        self.busy = False
        self.state = "opening"
        self.shell: ProcessTree | None = None
        self.outcomes: list[RunOutcome] = []
        self._live: LogLine | None = None
        self._live_text = ""
        self._live_dirty = False
        self._pending: asyncio.Future[bool] | None = None
        self.events = ScreenEvents(self)

    # --- layout ------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="agent-log")
        yield Static("", id="agent-todos", markup=False)
        yield TextArea(id="agent-composer", placeholder="Ask the agent… (/help for commands)")
        yield Static("", id="agent-status", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self.set_interval(LIVE_REDRAW_S, self._flush_live)
        self.render_status()
        self.query_one("#agent-composer", TextArea).focus()
        self.start()

    @work(group="agent-open", exclusive=True)
    async def start(self) -> None:
        root = self.options.root
        self.add_line(
            f"Workspace {root}. What the agent reads, and every command's output, is sent to "
            "MatterWave and the model; the deny list (.env, keys, .git, .matterwaveignore) is a "
            "floor, not a guarantee.",
            "notice",
        )
        if self.options.trust is None and folder_trust(root) is None:
            from .context import project_context_present

            if project_context_present(root):
                trusted = await self._ask_trust()
                set_folder_trust(root, "trusted" if trusted else "untrusted")
        try:
            opened = await open_session(self.wire, self.options, self.events)
        except (OpenError, APIError, APIConnectionError) as exc:
            self.state = "error"
            self.add_line(str(exc), "error")
            self.render_status()
            return
        self.opened = opened
        self.session = opened.session
        self.session.local.on_shell_start = self._shell_started
        self.unavailable = opened.unavailable
        for notice in opened.notices:
            self.add_line(notice, "notice")
        if self.session.messages:
            self.add_line(
                f"Resumed {self.session.title or self.session.conversation_id} "
                f"({len(self.session.messages)} messages).",
                "notice",
            )
        if self.unavailable:
            self.state = "unavailable"
            self.add_line(self.unavailable, "error")
        else:
            self.state = "idle"
        self.render_status()

    async def _ask_trust(self) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        def done(answer: bool | None) -> None:
            if not future.done():
                future.set_result(bool(answer))

        self.state = "trust"
        self.render_status()
        self.app.push_screen(TrustScreen(self.options.root, self.mode_name()), done)
        return await future

    # --- the log --------------------------------------------------------------------------

    def _log(self) -> VerticalScroll | None:
        """The log, or None once the screen is gone (a run's last events can
        arrive while the app shuts down)."""
        try:
            return self.query_one("#agent-log", VerticalScroll)
        except NoMatches:
            return None

    def add_line(self, text: str, classes: str = "", *, strict: bool = False) -> None:
        self.end_text()
        log = self._log()
        if log is None:
            return
        log.mount(LogLine(safe_text(text, strict=strict), classes=classes, markup=False))
        log.scroll_end(animate=False)

    def log_texts(self) -> list[str]:
        out: list[str] = []
        log = self._log()
        for child in log.children if log is not None else []:
            if isinstance(child, Answer):
                out.append(child.text)
            elif isinstance(child, Static):
                out.append(str(child.render()))
        return out

    def stream_text(self, delta: str, agent: str | None) -> None:
        if agent is not None:
            return
        if self._live is None:
            log = self._log()
            if log is None:
                return
            self._live = LogLine("", classes="live", markup=False)
            log.mount(self._live)
            self._live_text = ""
        self._live_text += delta
        self._live_dirty = True

    def _flush_live(self) -> None:
        if self._live is not None and self._live_dirty:
            self._live_dirty = False
            self._live.update(safe_text(self._live_text))
            log = self._log()
            if log is not None:
                log.scroll_end(animate=False)

    def end_text(self) -> None:
        """The streamed words become Markdown once the reply is whole."""
        live, self._live = self._live, None
        self._live_dirty = False
        if live is None:
            return
        log = self._log()
        if log is not None and self._live_text.strip():
            log.mount(Answer(self._live_text), after=live)
        live.remove()

    def show_todos(self, todos: list[dict[str, str]]) -> None:
        try:
            panel = self.query_one("#agent-todos", Static)
        except NoMatches:
            return
        panel.update(safe_text(render_todos(todos)))
        panel.set_class(bool(todos), "shown")

    # --- the status line -------------------------------------------------------------------

    def mode_name(self) -> str:
        if self.session is not None:
            return self.session.mode
        return self.options.mode or contract.DEFAULT_MODE

    def status_text(self) -> str:
        session = self.session
        bits = [f"mode {self.mode_name()} (Shift+Tab)"]
        if session is not None:
            bits.append(f"model {session.model or '—'}")
            budget = session.budget
            bits.append(f"turns {budget.turns}/{budget.max_turns}")
            bits.append(f"tokens {budget.tokens:,}/{budget.max_tokens:,}")
            bits.append(f"context ~{session.context_estimate():,}/{session.window():,}")
            if session.sandbox.engine != "none":
                bits.append(f"sandbox {session.sandbox.engine}")
        if self.state == "running":
            bits.append("running… (Esc stops)")
        elif self.state == "approval":
            bits.append("waiting for your approval")
        elif self.state == "unavailable":
            bits.append("agent unavailable")
        elif self.state in ("opening", "trust"):
            bits.append("opening…")
        elif self.state == "error":
            bits.append("could not open")
        if self.shell is not None and self.shell.proc.returncode is None:
            bits.append("a command is running")
        return " │ ".join(bits)

    def render_status(self) -> None:
        with contextlib.suppress(NoMatches):
            self.query_one("#agent-status", Static).update(self.status_text())

    # --- approvals ------------------------------------------------------------------------

    async def ask(self, request: ApprovalRequest) -> bool:
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending = future

        def done(answer: bool | None) -> None:
            if not future.done():
                future.set_result(bool(answer))

        previous = self.state
        self.state = "approval"
        self.render_status()
        self.app.push_screen(ApprovalScreen(request, self.mode_name()), done)
        try:
            approved = await future
        finally:
            self._pending = None
            self.state = previous
            self.render_status()
        verb = "approved" if approved else "rejected"
        self.add_line(f"You {verb}: {request.title}", "notice" if approved else "error")
        return approved

    def _shell_started(self, tree: ProcessTree) -> None:
        self.shell = tree
        self.render_status()

    # --- actions ---------------------------------------------------------------------------

    def action_cycle_mode(self) -> None:
        if self.session is None:
            return
        mode = self.session.next_mode()
        self.notify(f"Approval mode: {mode} — {contract.mode(mode).summary}")
        self.render_status()

    def action_stop(self) -> None:
        if self.running:
            self.workers.cancel_group(self, "agent-run")

    def action_send(self) -> None:
        composer = self.query_one("#agent-composer", TextArea)
        text = composer.text.strip()
        if not text:
            return
        if text.startswith("/"):
            composer.text = ""
            self.command(text)
            return
        if self.running or self.busy:
            self.notify("A run is going; Esc stops it." if self.running else "One moment.")
            return
        if self.session is None:
            self.notify("The session is still opening.")
            return
        if self.unavailable:
            self.add_line(self.unavailable, "error")
            return
        composer.text = ""
        self.begin(text)

    def begin(self, prompt: str, *, shown: str | None = None) -> None:
        self.add_line(f"› {shown or prompt}", "user")
        self.query_one("#agent-composer", TextArea).read_only = True
        self.running = True
        self.state = "running"
        self.render_status()
        self.run_worker(self._run(prompt), group="agent-run", exclusive=True)

    async def _run(self, prompt: str) -> None:
        session = self.session
        if session is None:
            return
        mentions = expand_mentions(prompt, session.root)
        for notice in mentions.notices:
            self.add_line(notice, "notice")
        if mentions.included:
            self.add_line("Attached: " + ", ".join(mentions.included), "notice")
        outcome: RunOutcome | None = None
        try:
            outcome = await session.run(mentions.text)
        except asyncio.CancelledError:
            outcome = session.last_outcome
            self._settle(outcome, stopped=True)
            raise
        except (APIError, APIConnectionError, OSError, ValueError) as exc:
            self.add_line(f"The run failed: {exc}", "error")
        self._settle(outcome, stopped=False)

    def _settle(self, outcome: RunOutcome | None, *, stopped: bool) -> None:
        self.end_text()
        self.running = False
        with contextlib.suppress(NoMatches):
            self.query_one("#agent-composer", TextArea).read_only = False
        self.shell = None
        self.state = "idle"
        if self.session is not None:
            remember_run(self.session)
        if outcome is not None:
            self.outcomes.append(outcome)
        if stopped:
            self.add_line(
                "Stopped. The stream was closed and any running command was ended.", "notice"
            )
        elif outcome is not None and not outcome.ok and outcome.message:
            self.add_line(outcome.message, "error")
        if outcome is not None:
            usage = outcome.usage
            if usage:
                self.add_line(
                    f"Run {outcome.status}: {usage.get('turns', 0)} model calls, "
                    f"{usage.get('total_tokens', 0):,} tokens.",
                    "notice",
                )
        self.render_status()

    # --- slash commands --------------------------------------------------------------------

    def command(self, text: str) -> None:
        name, _, rest = text[1:].partition(" ")
        rest = rest.strip()
        handlers: dict[str, Callable[[str], object]] = {
            "help": self._cmd_help,
            "model": self._cmd_model,
            "approval-mode": self._cmd_mode,
            "compact": self._cmd_compact,
            "context": self._cmd_context,
            "restore": self._cmd_restore,
            "init": self._cmd_init,
            "resume": self._cmd_resume,
            "trust": self._cmd_trust,
            "mcp": self._cmd_mcp,
            "agents": self._cmd_agents,
            "pi": self._cmd_pi,
            "pi-engine": self._cmd_pi_engine,
            "agent-engine": self._cmd_pi_engine,
            "pi-model": self._cmd_pi_model,
            "pi-fork": self._cmd_pi_fork,
            "pi-compact": self._cmd_pi_compact,
            "pi-status": self._cmd_pi_status,
            "pi-new": self._cmd_pi_new,
            "clear": self._cmd_clear,
            "chat": self._cmd_chat,
        }
        handler = handlers.get(name)
        if handler is not None:
            handler(rest)
            return
        session = self.session
        if session is not None:
            trusted = self.opened.trusted if self.opened else False
            commands = load_commands(session.root, trusted=trusted)
            custom = commands.get(name)
            if custom is not None:
                if self.running or self.unavailable:
                    self.notify("Not now: a run is going, or the agent is unavailable.")
                    return
                self.begin(custom.expand(rest), shown=text)
                return
        self.add_line(f"Unknown command /{name}. /help lists them.", "error")

    def _cmd_help(self, rest: str) -> None:
        session = self.session
        text = SLASH_HELP
        if session is not None:
            trusted = self.opened.trusted if self.opened else False
            custom = load_commands(session.root, trusted=trusted)
            if custom:
                text += "\nCustom commands\n" + "\n".join(
                    f"  /{c.name:<24}{c.description}" for c in custom.values()
                )
        self.add_line(text)

    def _cmd_mode(self, rest: str) -> None:
        session = self.session
        if session is None:
            return
        parts = rest.split()
        save = "--save" in parts
        parts = [p for p in parts if p != "--save"]
        if not parts:
            lines = [f"Approval mode: {session.mode}"]
            for name in contract.MODE_NAMES:
                locked = "" if name in session.cycle_modes() else "  (needs --yolo)"
                lines.append(f"  {name:<10}{contract.mode(name).summary}{locked}")
            self.add_line("\n".join(lines))
            return
        reason = session.set_mode(parts[0])
        if reason:
            self.add_line(f"Not changed: {reason}.", "error")
            return
        self.add_line(f"Approval mode: {session.mode}.", "notice")
        if save:
            refused = save_default_mode(session.mode)
            self.add_line(
                f"Not saved: {refused}."
                if refused
                else f"{session.mode} is now your default mode.",
                "error" if refused else "notice",
            )
        self.render_status()

    def _cmd_model(self, rest: str) -> None:
        session = self.session
        if session is None or self.opened is None:
            return
        models = self.opened.models
        if rest:
            self._set_model(rest)
            return
        rows: list[tuple[str, str]] = []
        for row in model_rows(models):
            if row.get("included", True) is False:
                continue
            mark = "● " if row.get("id") == session.model else "  "
            tools = "" if row.get("supports_tools") is not False else "  (cannot call tools)"
            rows.append((str(row.get("id")), f"{mark}{row.get('label') or row.get('id')}{tools}"))
        if not rows:
            self.add_line("No models to choose from.", "error")
            return

        def chosen(model_id: str | None) -> None:
            if model_id:
                self._set_model(model_id)

        self.app.push_screen(PickScreen("Choose the agent's model", rows, self.mode_name()), chosen)

    def _set_model(self, model_id: str) -> None:
        session = self.session
        if session is None or self.opened is None:
            return
        session.model = model_id
        for row in model_rows(self.opened.models):
            if row.get("id") == model_id and isinstance(row.get("context_window"), int):
                session.context_window = int(row["context_window"])
        self.unavailable = unavailable_reason(None, self.opened.models, model_id)
        if (
            self.unavailable is None
            and self.opened.unavailable
            and "organisation" in self.opened.unavailable
        ):
            self.unavailable = self.opened.unavailable
        self.state = "unavailable" if self.unavailable else "idle"
        self.add_line(
            self.unavailable or f"Model: {model_id}.", "error" if self.unavailable else "notice"
        )
        self.render_status()

    @work(group="agent-cmd")
    async def _cmd_compact(self, rest: str) -> None:
        session = self.session
        if session is None or self.running or self.busy:
            return
        self.busy = True
        try:
            note = await session.compact(force=True)
        finally:
            self.busy = False
        self.add_line(f"Compacted: {note}.", "notice")
        self.render_status()

    def _cmd_context(self, rest: str) -> None:
        session = self.session
        if session is None:
            return
        lines = [
            f"Context: ~{session.context_estimate():,} tokens of {session.window():,} "
            f"({len(session.messages)} messages)",
            "Context files: " + (", ".join(session.context.names) or "none loaded"),
        ]
        if session.context.skipped:
            lines.append("Not loaded (folder not trusted): " + ", ".join(session.context.skipped))
        lines.append(f"Account instructions: {'yes' if session.account_instructions else 'none'}")
        tools = [t["function"]["name"] for t in session.tools_for_request()]
        lines.append(f"Tools offered in {session.mode} mode: " + ", ".join(tools))
        if session.todos:
            lines.append("Tasks:\n" + render_todos(session.todos))
        if session.conversation_id:
            lines.append(f"Stored as conversation {session.conversation_id}")
        self.add_line("\n".join(lines))

    def _cmd_restore(self, rest: str) -> None:
        session = self.session
        if session is None or session.checkpoints is None:
            self.add_line("Checkpoints are off (git is not on PATH).", "error")
            return
        if self.running:
            self.notify("Stop the run first (Esc).")
            return
        if rest:
            self._restore(rest)
            return
        rows = session.checkpoints.entries()
        if not rows:
            self.add_line("No checkpoints yet: one is taken before each applied change.", "notice")
            return
        options = [
            (str(r.id), f"#{r.id}  {r.created}  {r.label}  ({', '.join(r.files)})")
            for r in reversed(rows)
        ]

        def chosen(value: str | None) -> None:
            if value:
                self._restore(value)

        self.app.push_screen(
            PickScreen("Restore the files to before…", options, self.mode_name()), chosen
        )

    @work(group="agent-cmd")
    async def _restore(self, value: str) -> None:
        session = self.session
        if session is None or session.checkpoints is None:
            return
        if self.running or self.busy:
            return
        self.busy = True
        try:
            checkpoint_id = int(value.lstrip("#"))
            paths = await session.checkpoints.arestore(checkpoint_id)
        except (ValueError, CheckpointError, OSError) as exc:
            self.add_line(f"Not restored: {exc}.", "error")
            return
        finally:
            self.busy = False
        rows = {r.id: r for r in session.checkpoints.entries()}
        row = rows.get(checkpoint_id)
        if (
            row is not None
            and row.turn is not None
            and row.conversation_id == session.conversation_id
        ):
            # The run's history goes back to where the checkpoint was taken,
            # so the model's picture of the files matches the files.
            del session.messages[row.turn :]
            AgentSession._close_open_calls(
                session.messages, "the files were restored to before this call"
            )
        self.add_line(
            f"Restored to before checkpoint #{checkpoint_id}: {', '.join(paths)}.", "notice"
        )
        self.render_status()

    def _cmd_init(self, rest: str) -> None:
        session = self.session
        if session is None:
            return
        if (session.root / "MATTERWAVE.md").exists():
            self.add_line(
                "MATTERWAVE.md already exists; edit it, or delete it and run /init again.", "notice"
            )
            return
        if self.running or self.unavailable:
            self.notify("Not now: a run is going, or the agent is unavailable.")
            return
        self.begin(INIT_PROMPT, shown="/init")

    def _cmd_resume(self, rest: str) -> None:
        session = self.session
        if session is None:
            return
        if self.running:
            self.notify("Stop the run first (Esc).")
            return
        if rest:
            self._resume(rest)
            return
        runs = recorded_runs(session.root)
        if not runs:
            self.add_line("No stored runs in this folder yet.", "notice")
            return
        rows = [
            (str(r["conversation_id"]), f"{r.get('started', '')}  {r.get('title', '')}")
            for r in runs
        ]

        def chosen(value: str | None) -> None:
            if value:
                self._resume(value)

        self.app.push_screen(PickScreen("Resume a run", rows, self.mode_name()), chosen)

    @work(group="agent-cmd")
    async def _resume(self, conversation_id: str) -> None:
        session = self.session
        if session is None:
            return
        if self.running or self.busy:
            return
        self.busy = True
        try:
            count = await resume(session, conversation_id)
        except OpenError as exc:
            self.add_line(str(exc), "error")
            return
        finally:
            self.busy = False
        self.add_line(f"Resumed {session.title or conversation_id} ({count} messages).", "notice")
        self.render_status()

    def _cmd_trust(self, rest: str) -> None:
        session = self.session
        if session is None:
            return
        set_folder_trust(session.root, "trusted")
        session.context = load_context(session.root, trusted=True)
        session.subagents = {d.name: d for d in load_subagents(session.root, trusted=True)}
        if self.opened is not None:
            self.opened.trusted = True
            self.opened.settings = load_settings(session.root, trusted=True)
        loaded = ", ".join(session.context.names) or "no context files"
        self.add_line(
            f"Trusted {session.root}. Loaded: {loaded}. Project MCP servers start next session.",
            "notice",
        )

    def _cmd_mcp(self, rest: str) -> None:
        session = self.session
        if session is None or session.mcp is None:
            self.add_line("No MCP servers.", "notice")
            return
        lines = [f"MCP tools ({session.mode} mode):"]
        registry = session.registry()
        verbs = {"allow": "runs", "ask": "asks", "deny": "not offered"}
        for name, info in sorted(session.mcp.tools.items()):
            spec = registry.get(name)
            verb = verbs[session.decide(spec)] if spec is not None else "not offered"
            kind = "read-only" if info.read_only else "may change things"
            lines.append(f"  {name}  ({kind}; {verb})")
        if len(lines) == 1:
            lines.append("  (none connected)")
        lines.extend(session.mcp.notices)
        self.add_line("\n".join(lines))

    def _cmd_agents(self, rest: str) -> None:
        session = self.session
        if session is None or not session.subagents:
            self.add_line(
                "No subagents. Add Markdown definitions under .matterwave/agents/ "
                "or the agents/ folder of your config.",
                "notice",
            )
            return
        self.add_line(
            "\n".join(
                f"  {d.name}: {d.description} (tools: {', '.join(d.tools) if d.tools else 'all'})"
                for d in session.subagents.values()
            )
        )

    def _cmd_pi_engine(self, rest: str) -> None:
        from ..app import AGENT_ENGINES, MatterWaveApp
        from ..pi_runner import find_pi_binary

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        current = app.agent_engine if app is not None else "auto"
        choice = rest.strip().lower()
        if not choice:
            resolved = (
                ("pi" if find_pi_binary(environ) else "builtin") if current == "auto" else current
            )
            self.add_line(
                f"Agent engine: {current} (active: {resolved}). "
                f"Use /pi-engine [{'|'.join(AGENT_ENGINES)}] to switch.",
                "notice",
            )
            return
        if choice not in AGENT_ENGINES:
            self.add_line(
                f"Unknown agent engine {rest.strip()!r}; choose {', '.join(AGENT_ENGINES)}.",
                "error",
            )
            return
        if app is not None:
            app.agent_engine = choice
            app.render_status()
        self.add_line(f"Agent engine set to {choice}.", "notice")

    def _cmd_pi_model(self, rest: str) -> None:
        from ..app import MatterWaveApp

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        raw = rest.strip()
        if not raw:
            model = (
                (self.session.model if self.session is not None else None)
                or (app.effective_model() if app is not None else None)
                or "default"
            )
            self.add_line(
                f"Pi 1.0 active model: {model} (provider: matterwave). Use /pi-model <model> to switch.",
                "notice",
            )
            return
        if "/" in raw:
            prov_part, mod_part = raw.split("/", 1)
            provider = prov_part.strip() or "matterwave"
            model = mod_part.strip() or raw
        else:
            provider = "matterwave"
            model = raw
        if self.session is not None:
            self.session.model = model
        if app is not None:
            app.run_pi_model(raw)
        self.add_line(f"Pi 1.0 model set to {model} (provider: {provider}).", "notice")
        self.render_status()

    def _cmd_pi_fork(self, rest: str) -> None:
        from ..app import MatterWaveApp
        from ..pi_runner import find_pi_binary

        if self.running or self.busy:
            self.notify("A run is going; Esc stops it." if self.running else "One moment.")
            return
        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        pi_bin = find_pi_binary(environ)
        if (app is None or app._pi_bridge is None) and pi_bin is None:
            self.add_line(
                "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi).",
                "error",
            )
            return
        self.run_worker(
            self._fork_pi_in_agent(rest.strip() or None, pi_bin=pi_bin),
            group="agent-cmd",
            exclusive=True,
        )

    async def _fork_pi_in_agent(
        self, entry_id: str | None = None, *, pi_bin: str | None = None
    ) -> None:
        from ..api import DEFAULT_BASE_URL
        from ..app import MatterWaveApp
        from ..pi_runner import PiRpcBridge, build_pi_argv, build_pi_launch_env

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        bridge = app._pi_bridge if app is not None else None
        owns_bridge = False
        if bridge is None:
            environ = app._environ if app is not None else None
            cred = getattr(self.wire, "_credential", None)
            api_key = str(
                getattr(cred, "token", "")
                or (environ.get("MATTERWAVE_API_KEY") if environ else "")
                or ""
            )
            base_url = str(getattr(self.wire, "base_url", "") or DEFAULT_BASE_URL)
            model = self.session.model if self.session is not None else None
            env = build_pi_launch_env(api_key, base_url, model=model, base_env=environ)
            argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
            bridge = PiRpcBridge(argv, env=env)
            owns_bridge = True
        self.busy = True
        try:
            if owns_bridge:
                await bridge.start()
            event = await bridge.fork_session(entry_id)
            if event is not None and event.is_error:
                self.add_line(
                    f"Pi session fork failed: {event.error or 'Pi RPC session fork failed'}",
                    "error",
                )
            else:
                self.add_line("Pi session forked.", "notice")
        finally:
            if owns_bridge:
                await bridge.close()
            self.busy = False
            self.render_status()

    def _cmd_pi_status(self, rest: str) -> None:
        from ..app import MatterWaveApp
        from ..pi_runner import find_pi_binary

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        current = app.agent_engine if app is not None else "auto"
        pi_bin = find_pi_binary(environ)
        resolved = ("pi" if pi_bin else "builtin") if current == "auto" else current
        model = (
            (self.session.model if self.session is not None else None)
            or (app.effective_model() if app is not None else None)
            or "default"
        )
        binary_label = pi_bin if pi_bin else "not found (set MATTERWAVE_PI_BIN or install pi)"
        self.add_line(
            f"Pi 1.0 status: engine={current} (active: {resolved}) | "
            f"binary={binary_label} | model={model}",
            "notice",
        )

    def _cmd_pi_compact(self, rest: str) -> None:
        from ..app import MatterWaveApp
        from ..pi_runner import find_pi_binary

        if self.running or self.busy:
            self.notify("A run is going; Esc stops it." if self.running else "One moment.")
            return
        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        pi_bin = find_pi_binary(environ)
        if (app is None or app._pi_bridge is None) and pi_bin is None:
            self.add_line(
                "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi).",
                "error",
            )
            return
        self.run_worker(
            self._compact_pi_in_agent(rest.strip() or None, pi_bin=pi_bin),
            group="agent-cmd",
            exclusive=True,
        )

    async def _compact_pi_in_agent(
        self, custom_instructions: str | None = None, *, pi_bin: str | None = None
    ) -> None:
        from ..api import DEFAULT_BASE_URL
        from ..app import MatterWaveApp
        from ..pi_runner import PiRpcBridge, build_pi_argv, build_pi_launch_env

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        bridge = app._pi_bridge if app is not None else None
        owns_bridge = False
        if bridge is None:
            environ = app._environ if app is not None else None
            cred = getattr(self.wire, "_credential", None)
            api_key = str(
                getattr(cred, "token", "")
                or (environ.get("MATTERWAVE_API_KEY") if environ else "")
                or ""
            )
            base_url = str(getattr(self.wire, "base_url", "") or DEFAULT_BASE_URL)
            model = self.session.model if self.session is not None else None
            env = build_pi_launch_env(api_key, base_url, model=model, base_env=environ)
            argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
            bridge = PiRpcBridge(argv, env=env)
            owns_bridge = True
        self.busy = True
        try:
            if owns_bridge:
                await bridge.start()
            event = await bridge.compact(custom_instructions)
            if event is not None and event.is_error:
                self.add_line(
                    f"Pi compaction failed: {event.error or 'Pi RPC compaction failed'}",
                    "error",
                )
            else:
                self.add_line("Pi session compacted.", "notice")
        finally:
            if owns_bridge:
                await bridge.close()
            self.busy = False
            self.render_status()

    def _cmd_pi_new(self, rest: str) -> None:
        from ..app import MatterWaveApp
        from ..pi_runner import find_pi_binary

        if self.running or self.busy:
            self.notify("A run is going; Esc stops it." if self.running else "One moment.")
            return
        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        pi_bin = find_pi_binary(environ)
        if (app is None or app._pi_bridge is None) and pi_bin is None:
            self.add_line(
                "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi).",
                "error",
            )
            return
        self.run_worker(
            self._new_pi_in_agent(pi_bin=pi_bin),
            group="agent-cmd",
            exclusive=True,
        )

    async def _new_pi_in_agent(self, *, pi_bin: str | None = None) -> None:
        from ..api import DEFAULT_BASE_URL
        from ..app import MatterWaveApp
        from ..pi_runner import PiRpcBridge, build_pi_argv, build_pi_launch_env

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        bridge = app._pi_bridge if app is not None else None
        owns_bridge = False
        if bridge is None:
            environ = app._environ if app is not None else None
            cred = getattr(self.wire, "_credential", None)
            api_key = str(
                getattr(cred, "token", "")
                or (environ.get("MATTERWAVE_API_KEY") if environ else "")
                or ""
            )
            base_url = str(getattr(self.wire, "base_url", "") or DEFAULT_BASE_URL)
            model = self.session.model if self.session is not None else None
            env = build_pi_launch_env(api_key, base_url, model=model, base_env=environ)
            argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
            bridge = PiRpcBridge(argv, env=env)
            owns_bridge = True
        self.busy = True
        try:
            if owns_bridge:
                await bridge.start()
            event = await bridge.new_session()
            if event is not None and event.is_error:
                self.add_line(
                    f"Pi session reset failed: {event.error or 'Pi RPC session reset failed'}",
                    "error",
                )
            else:
                self.add_line("Pi session reset.", "notice")
        finally:
            if owns_bridge:
                await bridge.close()
            self.busy = False
            self.render_status()

    def _cmd_pi(self, rest: str) -> None:
        from ..app import MatterWaveApp
        from ..pi_runner import find_pi_binary

        prompt = rest.strip()
        if not prompt:
            self.add_line("Usage: /pi <prompt> — run a prompt through Pi 1.0.", "error")
            return
        if self.running or self.busy:
            self.notify("A run is going; Esc stops it." if self.running else "One moment.")
            return
        app = self.app if isinstance(self.app, MatterWaveApp) else None
        environ = app._environ if app is not None else None
        pi_bin = find_pi_binary(environ)
        if (app is None or app._pi_bridge is None) and pi_bin is None:
            self.add_line(
                "pi binary not found on PATH (set MATTERWAVE_PI_BIN or install pi).",
                "error",
            )
            return
        self.add_line(f"› /pi {prompt}", "user")
        self.run_worker(
            self._run_pi_in_agent(prompt, pi_bin=pi_bin), group="agent-run", exclusive=True
        )

    async def _run_pi_in_agent(self, prompt: str, *, pi_bin: str | None = None) -> None:
        import json

        from ..api import DEFAULT_BASE_URL
        from ..app import MatterWaveApp
        from ..pi_runner import PiRpcBridge, build_pi_argv, build_pi_launch_env

        app = self.app if isinstance(self.app, MatterWaveApp) else None
        bridge = app._pi_bridge if app is not None else None
        owns_bridge = False
        if bridge is None:
            environ = app._environ if app is not None else None
            cred = getattr(self.wire, "_credential", None)
            api_key = str(
                getattr(cred, "token", "")
                or (environ.get("MATTERWAVE_API_KEY") if environ else "")
                or ""
            )
            base_url = str(getattr(self.wire, "base_url", "") or DEFAULT_BASE_URL)
            model = self.session.model if self.session is not None else None
            env = build_pi_launch_env(api_key, base_url, model=model, base_env=environ)
            argv = build_pi_argv(pi_bin or "pi", mode="rpc", model=model)
            bridge = PiRpcBridge(argv, env=env)
            owns_bridge = True
        self.running = True
        self.state = "running"
        self.render_status()
        try:
            if owns_bridge:
                await bridge.start()
            async for event in bridge.prompt(prompt):
                if event.type == "text_delta" and event.text:
                    self.stream_text(event.text, None)
                elif event.type == "tool_start":
                    args_str = json.dumps(event.tool_args) if event.tool_args else ""
                    self.add_line(f"⚙ {event.tool_name or 'tool'}({args_str})", "tool", strict=True)
                elif event.type == "tool_end":
                    mark = "✗ " if event.is_error else "↳ "
                    self.add_line(
                        f"{mark}{event.tool_result}",
                        "result error" if event.is_error else "result",
                    )
                elif event.type == "error":
                    self.add_line(event.error or "Pi RPC error", "error")
                    break
                elif event.type == "turn_end":
                    break
        finally:
            self.end_text()
            if owns_bridge:
                await bridge.close()
            self.running = False
            self.state = "idle"
            self.render_status()

    def _cmd_clear(self, rest: str) -> None:
        session = self.session
        if session is None or self.running:
            return
        session.messages.clear()
        session.todos = []
        session.summary = None
        session.unreviewed.clear()
        session.conversation_id = None
        self.show_todos([])
        self.add_line("Cleared. The next prompt starts a new run.", "notice")
        self.render_status()

    def _cmd_chat(self, rest: str) -> None:
        if self.running:
            self.notify("Stop the run first (Esc).")
            return
        if self.leave_callback is not None:
            self.leave_callback()

    async def on_unmount(self) -> None:
        if self.session is not None:
            await self.session.aclose()

    @on(TextArea.Changed, "#agent-composer")
    def _changed(self) -> None:
        self.render_status()
