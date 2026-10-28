"""The open conversation as one Markdown file, byte for byte the web
exporter's format, so the same conversation downloads the same document
from either client.

Pure client-side: nothing is fetched and nothing is sent. What is on the
active path is what exports; summaries and system messages are omitted, as
the web export omits them.
"""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime

from matterwave_sdk.models import Conversation

from .thread import Message, Thread

# The filename's stem is cut here before the `.md` is appended.
FILENAME_MAX = 80
# Characters Windows forbids in a name, on top of the control characters.
UNSAFE_CHARACTERS = '<>:"/\\|?*'
WINDOWS_RESERVED = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{n}" for n in range(1, 10)),
        *(f"lpt{n}" for n in range(1, 10)),
    }
)


def _parts_markdown(message: Message) -> list[str]:
    """The message's parts in the web exporter's rendering, joined later by
    a blank line. Reasoning is dropped: it is the model's working, not its
    answer, and the web export does not keep it either."""
    out: list[str] = []
    for part in message.parts:
        kind = part.get("type")
        if kind == "text":
            out.append(str(part.get("text") or ""))
        elif kind == "reasoning":
            continue
        elif kind == "tool_call":
            name = part.get("name") or "tool"
            arguments = str(part.get("arguments") or "")
            out.append(f"*Called `{name}`*\n\n```json\n{arguments}\n```")
        elif kind == "tool_result":
            failed = bool(part.get("is_error") or part.get("error"))
            output = str(part.get("output") or "")
            out.append(f"*Tool result:*\n\n```\n{output}\n```" + (" (error)" if failed else ""))
        elif kind == "image":
            out.append(f"*Attached image: {part.get('filename') or 'attachment'}*")
        elif kind == "document":
            out.append(f"*Attached file: {part.get('filename') or 'attachment'}*")
        else:
            out.append(f"*(unsupported content: {kind})*")
    return out


def _heading(message: Message) -> str:
    if message.role == "user":
        return "You"
    if message.role == "assistant":
        return f"Assistant ({message.model})" if message.model else "Assistant"
    return (message.role or "message").capitalize()


def _body(message: Message) -> str:
    body = "\n\n".join(_parts_markdown(message))
    if not body and message.content:
        return message.content
    return body


def conversation_markdown(
    conversation: Conversation, thread: Thread, *, now: datetime | None = None
) -> str:
    """The conversation's active path as one Markdown document. `now` pins
    the export stamp; it is the current UTC time when left out."""
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    title = conversation.title or "MatterWave conversation"
    out = [
        f"# {title}\n\n"
        f"Exported from MatterWave on {stamp}. Conversation `{conversation.id}`.\n\n"
    ]
    for item in thread.view():
        # A `Divider` is a summary's fold, and a summary message itself is
        # a system message: neither is in the web export.
        if not isinstance(item, Message) or item.role == "system":
            continue
        out.append(f"## {_heading(item)}\n\n{_body(item)}\n\n")
    return "".join(out)


def _filename_stem(title: str) -> str:
    """The title as a safe stem, exactly as the web exporter derives it."""
    text = unicodedata.normalize("NFKC", title)
    text = "".join(
        " " if (unicodedata.category(character)[0] == "C" or character in UNSAFE_CHARACTERS)
        else character
        for character in text
    )
    text = " ".join(text.split())  # collapse whitespace, which also trims
    text = text[:FILENAME_MAX]
    text = text.strip(". ")
    if not text:
        return "conversation"
    if text.lower() in WINDOWS_RESERVED:
        text += " conversation"
    return text


def export_filename(conversation: Conversation) -> str:
    return _filename_stem(conversation.title or "") + ".md"
