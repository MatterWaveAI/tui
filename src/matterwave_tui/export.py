"""The open conversation as one Markdown file, built from the tree in memory.

Pure client-side: nothing is fetched and nothing is sent. What is on the
active path is what exports, summaries folded in as quotes exactly where
the transcript folds them, so the file reads like the transcript looks.
"""

from __future__ import annotations

import re
from typing import Any

from matterwave_sdk.models import Conversation

from .thread import Divider, Message, Thread


def _parts_markdown(message: Message) -> list[str]:
    """The message's parts the way the transcript renders them, as Markdown."""
    out: list[str] = []
    for part in message.parts:
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text:
                out.append(text)
        elif kind == "reasoning":
            # The model's working, not its answer; the transcript collapses it.
            continue
        elif kind == "tool_call":
            out.append(f"> ⚙ `{part.get('name') or 'tool'}` called")
        elif kind == "tool_result":
            flat = " ".join(str(part.get("output") or "").split())
            out.append(f"> ↳ {flat}")
        elif kind in ("image", "document"):
            out.append(f"> *[{kind}: {part.get('filename') or 'attachment'}]*")
        # Unknown kinds are skipped, as every reader of parts does.
    if not out and message.content:
        out.append(message.content)
    return out


def _heading(message: Message) -> str:
    if message.role == "user":
        return "You"
    if message.role == "assistant":
        return f"Assistant · {message.model}" if message.model else "Assistant"
    return (message.role or "message").capitalize()


def conversation_markdown(conversation: Conversation, thread: Thread) -> str:
    """The conversation's active path as one Markdown document."""
    out: list[str] = [f"# {conversation.title or 'MatterWave conversation'}", ""]
    for item in thread.view():
        if isinstance(item, Divider):
            head = f"> **Summary** — {item.text}" if item.text else "> **Summary**"
            out += [head, ""]
            continue
        out += [f"## {_heading(item)}", ""]
        chunks: list[Any] = _parts_markdown(item)
        if item.truncated:
            chunks.append("*Cut off — this is not the whole answer.*")
        if chunks:
            out += ["\n\n".join(str(c) for c in chunks), ""]
    return "\n".join(out).rstrip("\n") + "\n"


def export_filename(conversation: Conversation) -> str:
    """`matterwave-<slugged-title>-<id prefix>.md`, safe on every filesystem."""
    slug = re.sub(r"[^a-z0-9]+", "-", (conversation.title or "").lower()).strip("-")
    stem = slug or "conversation"
    return f"matterwave-{stem}-{conversation.id[:8] or 'chat'}.md"
