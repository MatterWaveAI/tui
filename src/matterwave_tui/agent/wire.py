"""What a run asks of the API, and a stored run read back as a prompt.

The run talks to `/v1/chat/completions` (the tools, the calls, the usage)
and, when it is stored, to its conversation: created at the start, named
on every request by `metadata.conversation_id`, compacted when the context
fills, read back by `--resume`. `HttpChatAPI` implements this over the SDK;
the tests' fake implements it over a scripted `/v1`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from matterwave_sdk.models import (
    AssistantMessage,
    Conversation,
    ConversationMessage,
    Me,
    Usage,
    local_agent_tools_of,
    tools_of,
)

from ..thread import Divider, Thread

UNANSWERED_CALL = "(not run: the run stopped before this call was answered)"


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class Completed:
    """The model's whole reply: its text and tool calls, the usage the
    server reported (None when it sent none), and the request id."""

    message: AssistantMessage
    usage: Usage | None
    finish_reason: str | None
    request_id: str | None


CompletionEvent = TextDelta | Completed


class AgentWire(Protocol):
    async def me(self) -> Me: ...

    async def models(self) -> dict[str, Any]: ...

    async def create_conversation(
        self, title: str | None = None, model: str | None = None, temporary: bool = False
    ) -> Conversation: ...

    async def tree(self, conversation_id: str) -> Conversation: ...

    async def compact(self, conversation_id: str) -> ConversationMessage: ...

    async def conversation_instructions(self, conversation_id: str) -> dict[str, Any]: ...

    def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None,
        tools: Sequence[dict[str, Any]],
        conversation_id: str | None,
    ) -> AsyncIterator[CompletionEvent]: ...

    async def mcp_tools(self) -> list[dict[str, Any]]: ...

    async def mcp_call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]: ...


# --- capability checks -----------------------------------------------------------------


def model_row(models: dict[str, Any], model_id: str | None) -> dict[str, Any] | None:
    data = models.get("data")
    if not isinstance(data, list):
        return None
    for row in data:
        if isinstance(row, dict) and row.get("id") == model_id:
            return row
    return None


def model_rows(models: dict[str, Any]) -> list[dict[str, Any]]:
    data = models.get("data")
    return [m for m in data if isinstance(m, dict)] if isinstance(data, list) else []


def unavailable_reason(me: Me | None, models: dict[str, Any] | None, model_id: str | None) -> str | None:
    """Why agent mode is off for this account and model, or None when it is
    on: the SDK's `local_agent_tools_of` and `tools_of`, where only "no"
    refuses, so a field the server does not send (an older server) allows it."""
    if local_agent_tools_of(me) == "no":
        return (
            "Agent unavailable: your organisation has switched off local agent tools. "
            "An owner or admin can turn them on in Settings > Organisation."
        )
    if models is not None and model_id and tools_of(model_rows(models), model_id) == "no":
        row = model_row(models, model_id) or {}
        label = row.get("label") or model_id
        return f"Agent unavailable: {label} cannot call tools. Pick a model that can (/model)."
    return None


def pick_model(models: dict[str, Any], wanted: str | None) -> str | None:
    """The run's model: the one asked for; else the deployment's default
    unless it cannot call tools; else the first included model that is not
    known to be unable to; else the default (and the unavailable notice
    says why)."""
    if wanted:
        return wanted
    default = models.get("default_model")
    default_id = str(default) if default else None
    rows = model_rows(models)
    if default_id and tools_of(rows, default_id) != "no":
        return default_id
    for row in rows:
        model_id = row.get("id")
        if row.get("included", True) is not False and tools_of(rows, str(model_id)) != "no":
            return str(model_id)
    return default_id


# --- a stored run, back as a prompt --------------------------------------------------------


def _assistant_messages(parts: list[dict[str, Any]], fallback: str) -> list[dict[str, Any]]:
    """One stored assistant message (text, tool_call and tool_result parts,
    in the order the run wrote them) as the prompt's messages: each round
    an assistant message with its `tool_calls`, then a `tool` message per
    call. A call left without a result gets a stand-in, since a prompt with
    an unanswered call is refused."""
    out: list[dict[str, Any]] = []
    text: list[str] = []
    calls: list[dict[str, Any]] = []
    results: dict[str, str] = {}

    def emit() -> None:
        message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
        if calls:
            message["tool_calls"] = list(calls)
        out.append(message)
        for call in calls:
            content = results.get(call["id"], UNANSWERED_CALL)
            out.append({"role": "tool", "tool_call_id": call["id"], "content": content})
        text.clear()
        calls.clear()
        results.clear()

    for part in parts:
        kind = part.get("type")
        if kind in ("text", "tool_call") and results:
            emit()
        if kind == "text":
            text.append(str(part.get("text") or ""))
        elif kind == "tool_call" and part.get("id"):
            calls.append(
                {
                    "id": str(part["id"]),
                    "type": "function",
                    "function": {
                        "name": str(part.get("name") or ""),
                        "arguments": str(part.get("arguments") or "{}"),
                    },
                }
            )
        elif kind == "tool_result" and any(c["id"] == part.get("id") for c in calls):
            results[str(part["id"])] = str(part.get("output") or "")
    if calls or "".join(text):
        emit()
    if not out and fallback:
        out.append({"role": "assistant", "content": fallback})
    return out


def stored_run_messages(tree: Conversation) -> list[dict[str, Any]]:
    """A stored conversation's active path as `/v1` messages: user turns,
    assistant turns with their tool rounds, and a compaction summary as a
    note in place of what it covers. The system prompt is not stored; the
    caller puts the current one first."""
    thread = Thread.from_tree(list(tree.messages), tree.current_leaf_message_id)
    out: list[dict[str, Any]] = []
    for item in thread.view():
        if isinstance(item, Divider):
            out.append(
                {
                    "role": "user",
                    "content": f"(A summary of the earlier part of this conversation.)\n{item.text}",
                }
            )
            continue
        if item.role == "user":
            out.append({"role": "user", "content": str(item.content or "")})
        elif item.role == "assistant":
            parts = [p for p in (item.parts or []) if isinstance(p, dict)]
            out.extend(_assistant_messages(parts, str(item.content or "")))
    return out
