"""The transcript's pieces: one message, one compaction divider.

Every string from the server or the model is rendered with markup off.
Rich markup in a model's answer is text the model wrote, not styling to apply.
"""

from __future__ import annotations

import json
from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message as MessageBase
from textual.widgets import Collapsible, Markdown, Static

from .thread import Divider, Message

TOOL_LINE_MAX = 100


def _one_line(text: str, limit: int = TOOL_LINE_MAX) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def tool_call_line(part: dict[str, Any]) -> str:
    args = str(part.get("arguments") or "")
    try:
        decoded = json.loads(args) if args else {}
        if isinstance(decoded, dict):
            args = ", ".join(f"{k}={json.dumps(v)}" for k, v in decoded.items())
    except ValueError:
        pass
    return _one_line(f"⚙ {part.get('name') or 'tool'}({args})")


def tool_result_line(part: dict[str, Any]) -> str:
    mark = "✗" if part.get("is_error") else "↳"
    return _one_line(f"{mark} {part.get('output') or ''}")


def role_label(message: Message, position: tuple[int, int] | None) -> str:
    if message.role == "user":
        label = "You"
    elif message.role == "assistant":
        label = f"Assistant · {message.model}" if message.model else "Assistant"
    else:
        label = (message.role or "message").capitalize()
    if position is not None:
        label += f"   ‹ {position[0] + 1}/{position[1]} ›"
    return label


class ThumbClicked(MessageBase):
    """A thumb on an assistant reply was clicked: `rating` is 1 or -1."""

    def __init__(self, message_id: str, rating: int) -> None:
        super().__init__()
        self.message_id = message_id
        self.rating = rating


class MessageView(Vertical):
    """One message: a header line, then each part the way the web renders it.
    An assistant reply carries the two thumbs the feedback is given with."""

    DEFAULT_CSS = """
    MessageView { height: auto; padding: 0 1; margin: 0 0 1 0; }
    MessageView.selected { background: $boost; border-left: thick $accent; }
    MessageView .role { color: $text-muted; text-style: bold; }
    MessageView.user .role { color: $accent; }
    MessageView .tool { color: $text-muted; }
    MessageView .truncated { color: $warning; text-style: italic; }
    MessageView .thumbs { height: auto; }
    MessageView .thumbs Static { width: auto; margin-right: 1; color: $text-muted; }
    MessageView .thumbs Static.active { color: $text; text-style: bold; }
    MessageView Markdown { margin: 0; padding: 0; }
    """

    def __init__(
        self,
        message: Message,
        *,
        position: tuple[int, int] | None = None,
        streaming: bool = False,
        feedback: dict[str, Any] | None = None,
        image_backend: str | None = None,
    ) -> None:
        super().__init__(classes=message.role)
        self.message = message
        self.position = position
        self.streaming = streaming
        self.feedback = feedback
        self.image_backend = image_backend

    def compose(self) -> ComposeResult:
        yield Static(role_label(self.message, self.position), classes="role", markup=False)
        if self.message.role == "assistant" and not self.streaming:
            rating = (self.feedback or {}).get("rating")
            with Horizontal(classes="thumbs"):
                yield Static(
                    "👍",
                    id="thumb-up",
                    classes="thumb" + (" active" if rating == 1 else ""),
                    markup=False,
                )
                yield Static(
                    "👎",
                    id="thumb-down",
                    classes="thumb" + (" active" if rating == -1 else ""),
                    markup=False,
                )
        parts: list[dict[str, Any]] = self.message.parts
        if not parts and self.message.content:
            parts = [{"type": "text", "text": self.message.content}]
        for part in parts:
            kind = part.get("type")
            if kind == "text":
                yield Markdown(str(part.get("text") or ""))
            elif kind == "reasoning":
                # Collapsed: it is the model's working, not its answer.
                yield Collapsible(
                    Static(str(part.get("text") or ""), markup=False),
                    title="Reasoning",
                    collapsed=True,
                )
            elif kind == "tool_call":
                yield Static(tool_call_line(part), classes="tool", markup=False)
            elif kind == "tool_result":
                yield Static(tool_result_line(part), classes="tool", markup=False)
            elif kind == "image":
                yield ImageView(part, backend=self.image_backend)
            elif kind == "document":
                yield Static(
                    f"[document: {part.get('filename') or 'attachment'}]", classes="tool",
                    markup=False,
                )
            # Unknown kinds are skipped, as every reader of parts does.
        if self.streaming:
            yield Static("…", classes="tool")
        elif self.message.truncated:
            yield Static(
                "⚠ Cut off — this is not the whole answer.", classes="truncated", markup=False
            )

    async def update_message(self, message: Message, *, streaming: bool | None = None) -> None:
        self.message = message
        if streaming is not None:
            self.streaming = streaming
        await self.recompose()

    def apply_feedback(self, feedback: dict[str, Any] | None) -> None:
        """The caller's rating, marked on the thumbs in place: a vote must
        not rebuild the message it sits on."""
        self.feedback = feedback
        rating = (feedback or {}).get("rating")
        for thumb_id, active in (("thumb-up", rating == 1), ("thumb-down", rating == -1)):
            self.query_one(f"#{thumb_id}", Static).set_class(active, "active")

    def on_click(self, event: events.Click) -> None:
        """A thumb clicked is a rating asked for; the app decides whether it
        sets or withdraws, the server being the keeper of the truth."""
        if self.message.role != "assistant" or self.streaming:
            return
        thumb = event.control.id if event.control is not None else None
        if thumb == "thumb-up":
            self.post_message(ThumbClicked(self.message.id, 1))
        elif thumb == "thumb-down":
            self.post_message(ThumbClicked(self.message.id, -1))


class ImageView(Static):
    """An image part of a message. Without a terminal to draw on (no
    backend, or an id-less part) it is the text placeholder and stays one;
    with one, the app downloads the bytes and hands them here, and the
    image layer draws the picture where this line sits."""

    DEFAULT_CSS = """
    ImageView { color: $text-muted; }
    """

    def __init__(self, part: dict[str, Any], *, backend: str | None = None) -> None:
        self.part = part
        self.attachment_id = str(part.get("attachment_id") or "")
        self.filename = str(part.get("filename") or "attachment")
        self.backend = backend
        self.data: bytes | None = None
        self.failed = False
        super().__init__(self.placeholder(), classes="tool", markup=False)

    def placeholder(self) -> str:
        return f"[image: {self.filename}]"

    def wants_bytes(self) -> bool:
        """Whether the app still owes this view its bytes: there is a
        terminal to draw on, an attachment to fetch, and neither a delivery
        nor a failure yet."""
        return bool(self.backend and self.attachment_id and self.data is None and not self.failed)

    def set_data(self, data: bytes) -> None:
        """The attachment arrived; the image layer takes it from here."""
        self.data = data
        self.update(self.placeholder())

    def set_failed(self) -> None:
        """The download did not land: the placeholder gains a word of why,
        and nothing fetches again."""
        self.failed = True
        self.update(self.placeholder() + " — could not be loaded")


class DividerView(Vertical):
    """Earlier messages were summarised; `x` shows the originals above it."""

    DEFAULT_CSS = """
    DividerView { height: auto; padding: 0 1; margin: 0 0 1 0; border-top: dashed $secondary; }
    DividerView.selected { background: $boost; }
    DividerView .head { color: $secondary; text-style: bold; }
    """

    def __init__(self, divider: Divider) -> None:
        super().__init__()
        self.divider = divider
        self.message = divider.summary

    def compose(self) -> ComposeResult:
        count = len(self.divider.covered)
        hint = "x hides the originals" if self.divider.expanded else "x shows the originals"
        yield Static(
            f"── Earlier messages were summarised ({count} messages) · {hint} ──",
            classes="head",
            markup=False,
        )
        yield Collapsible(
            Markdown(self.divider.text), title="Summary", collapsed=True
        )
