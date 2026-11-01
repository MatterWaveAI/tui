"""`matterwave-tui -p "<prompt>"` in agent mode: one run, no interface.

Output formats:

- `text`: the answer on stdout as it streams; tool lines and notices on
  stderr, so a pipe gets only the answer.
- `json`: one JSON document when the run ends: the result, the usage and
  an error flag (`is_error`), with the status and its message.
- `stream-json`: one JSON object per line as the run goes (`init`, `text`,
  `tool_call`, `tool_result`, `notice`, `todos`), then the same `result`
  object `json` prints.

A headless run never prompts. A call the mode would ask about is refused
and the run ends; the exit code says how it ended (`EXIT_CODES`).
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import IO, Any

from matterwave_sdk.models import ToolCall

from .context import expand_mentions, load_commands
from .loop import ApprovalRequest, Budget, RunEvents, RunOutcome, Status
from .session import OpenError, SessionOptions, open_session, remember_run
from .tools import ToolOutcome
from .wire import AgentWire

OUTPUT_FORMATS = ("text", "json", "stream-json")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_BUDGET = 3
EXIT_REFUSED = 4
EXIT_CAP = 5

EXIT_CODES: dict[Status, int] = {
    "done": EXIT_OK,
    "error": EXIT_ERROR,
    "unavailable": EXIT_ERROR,
    "stopped": EXIT_ERROR,
    "budget": EXIT_BUDGET,
    "rate_limited": EXIT_BUDGET,
    "plan": EXIT_BUDGET,
    "refused": EXIT_REFUSED,
    "step_cap": EXIT_CAP,
    "token_cap": EXIT_CAP,
}


class HeadlessEvents(RunEvents):
    def __init__(self, output_format: str, out: IO[str], err: IO[str]) -> None:
        self.format = output_format
        self.out = out
        self.err = err
        self.notices: list[str] = []
        self.tool_calls = 0
        self.wrote_text = False

    def line(self, record: dict[str, Any]) -> None:
        self.out.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.out.flush()

    def on_text(self, delta: str, agent: str | None) -> None:
        if agent is not None:
            return
        if self.format == "text":
            self.out.write(delta)
            self.out.flush()
            self.wrote_text = True
        elif self.format == "stream-json":
            self.line({"type": "text", "text": delta})

    def on_reply(self, content: str | None, calls: list[ToolCall], agent: str | None) -> None:
        if self.format == "text" and agent is None and calls and self.wrote_text:
            self.out.write("\n")
            self.wrote_text = False

    def on_tool_start(self, call: ToolCall, summary: str, agent: str | None) -> None:
        self.tool_calls += 1
        if self.format == "stream-json":
            record: dict[str, Any] = {
                "type": "tool_call", "id": call.id, "name": call.function.name,
                "arguments": call.function.arguments,
            }
            if agent:
                record["agent"] = agent
            self.line(record)
        elif self.format == "text":
            prefix = f"[{agent}] " if agent else ""
            self.err.write(f"{prefix}⚙ {summary}\n")

    def on_tool_end(self, call: ToolCall, outcome: ToolOutcome, agent: str | None) -> None:
        if self.format == "stream-json":
            record: dict[str, Any] = {
                "type": "tool_result", "id": call.id, "name": call.function.name,
                "is_error": outcome.is_error, "output": outcome.text,
            }
            if agent:
                record["agent"] = agent
            self.line(record)
        elif self.format == "text" and outcome.is_error:
            self.err.write(f"  ✗ {outcome.text.splitlines()[0] if outcome.text else ''}\n")

    def on_notice(self, text: str) -> None:
        self.notices.append(text)
        if self.format == "stream-json":
            self.line({"type": "notice", "text": text})
        elif self.format == "text":
            self.err.write(f"note: {text}\n")

    def on_todos(self, todos: list[dict[str, str]]) -> None:
        if self.format == "stream-json":
            self.line({"type": "todos", "todos": todos})

    def on_usage(self, budget: Budget) -> None:
        return None

    async def approve(self, request: ApprovalRequest) -> bool:
        return False


def result_record(
    outcome: RunOutcome, *, conversation_id: str | None, mode: str, model: str | None,
    notices: list[str], tool_calls: int,
) -> dict[str, Any]:
    return {
        "type": "result",
        "status": outcome.status,
        "is_error": not outcome.ok,
        "result": outcome.text,
        "message": outcome.message,
        "usage": outcome.usage,
        "tool_calls": tool_calls,
        "conversation_id": conversation_id,
        "request_id": outcome.request_id,
        "mode": mode,
        "model": model,
        "notices": notices,
        "exit_code": EXIT_CODES[outcome.status],
    }


def expand_command(prompt: str, commands: dict[str, Any]) -> str:
    """A prompt that starts with a custom command's `/name` becomes the
    command's text with the rest as its arguments."""
    if not prompt.startswith("/"):
        return prompt
    name, _, rest = prompt[1:].partition(" ")
    command = commands.get(name)
    return command.expand(rest.strip()) if command is not None else prompt


async def run_headless(
    wire: AgentWire,
    options: SessionOptions,
    prompt: str,
    *,
    output_format: str = "text",
    out: IO[str] | None = None,
    err: IO[str] | None = None,
) -> int:
    stdout = out or sys.stdout
    stderr = err or sys.stderr
    events = HeadlessEvents(output_format, stdout, stderr)
    try:
        opened = await open_session(wire, options, events)
    except OpenError as exc:
        stderr.write(f"{exc}\n")
        return EXIT_USAGE
    session = opened.session
    for notice in opened.notices:
        events.on_notice(notice)
    if output_format == "stream-json":
        events.line(
            {"type": "init", "root": str(session.root), "mode": session.mode, "model": session.model,
             "conversation_id": session.conversation_id, "trusted": opened.trusted}
        )
    try:
        if opened.unavailable is not None:
            outcome = RunOutcome("unavailable", message=opened.unavailable)
        else:
            text = expand_command(prompt, load_commands(session.root, trusted=opened.trusted))
            mentions = expand_mentions(text, session.root)
            for notice in mentions.notices:
                events.on_notice(notice)
            try:
                outcome = await session.run(mentions.text)
            except asyncio.CancelledError:
                # Ctrl+C: the stream is closed and any command's tree killed;
                # the result is still written, then the interrupt goes on.
                outcome = session.last_outcome or RunOutcome("stopped", message="Stopped.")
            remember_run(session)
    finally:
        await session.aclose()
    record = result_record(
        outcome, conversation_id=session.conversation_id, mode=session.mode, model=session.model,
        notices=events.notices, tool_calls=events.tool_calls,
    )
    if output_format == "text":
        if events.wrote_text:
            stdout.write("\n")
        if not outcome.ok and outcome.message:
            stderr.write(f"{outcome.message}\n")
    elif output_format == "stream-json":
        events.line(record)
    else:
        stdout.write(json.dumps(record, ensure_ascii=False, indent=2) + "\n")
    return EXIT_CODES[outcome.status]
