"""An in-memory `ChatAPI` that streams scripted frames."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import ConflictError
from matterwave_sdk.models import Conversation, ConversationContext, ConversationMessage, Me

from matterwave_tui.api import Opened, StreamItem

MODELS: list[dict[str, Any]] = [
    {"id": "grt-nano", "label": "GRT Nano", "partner": False, "research_preview": False,
     "included": True, "upgrade_to": None, "context_window": 8192},
    {"id": "grt-max", "label": "GRT Max", "partner": False, "research_preview": False,
     "included": False, "upgrade_to": "Pro", "context_window": 131072},
    {"id": "other-7b", "label": "Other 7B", "partner": True, "research_preview": False,
     "included": True, "upgrade_to": None, "context_window": None},
    {"id": "lab-x", "label": "Lab X", "partner": False, "research_preview": True,
     "included": True, "upgrade_to": None, "context_window": 32768},
]


def conversation(cid: str = "c1", **fields: Any) -> Conversation:
    return Conversation.from_dict(
        {"id": cid, "title": "New chat", "created_at": "1", "updated_at": "1",
         "default_model": "grt-nano", **fields}
    )


def message(
    mid: str, role: str, text: str, parent: str | None = None, created_at: str = "1", **extra: Any
) -> ConversationMessage:
    parts = extra.pop("parts", [{"type": "text", "text": text}])
    return ConversationMessage.from_dict(
        {"id": mid, "conversation_id": "c1", "role": role, "parts": parts, "content": text,
         "created_at": created_at, "parent_id": parent, **extra}
    )


class FakeAPI:
    def __init__(
        self,
        script: list[StreamItem] | None = None,
        *,
        hold: bool = False,
        refusal: Exception | None = None,
    ) -> None:
        self.script = script or []
        # `hold`: the stream never ends on its own, for the Esc test;
        # `refusal`: the request is refused before any frame.
        self.hold = hold
        self.refusal = refusal
        self.conversations: list[Conversation] = [conversation()]
        self.tree_messages: list[ConversationMessage] = []
        self.sent: list[tuple[str, str, str | None]] = []
        self.leaf_moves: list[str] = []
        self.model_updates: list[tuple[str, str | None]] = []
        self.searches: list[str] = []
        self.compacted: list[str] = []
        self.tree_reads = 0
        self.streams_closed = 0

    async def me(self) -> Me:
        return Me.from_dict({"subject": "u", "kind": "user"})

    async def models(self) -> dict[str, Any]:
        return {"data": MODELS, "default_model": "grt-nano"}

    async def list_conversations(self, filter: str = "default") -> list[Conversation]:
        return list(self.conversations)

    async def search_conversations(self, query: str) -> list[Conversation]:
        self.searches.append(query)
        return [c for c in self.conversations if query.lower() in c.title.lower()]

    async def create_conversation(self, title=None, model=None) -> Conversation:
        conv = conversation(
            f"c{len(self.conversations) + 1}", title=title or "New chat", default_model=model
        )
        self.conversations.insert(0, conv)
        return conv

    def _find(self, conversation_id: str) -> Conversation:
        return next(c for c in self.conversations if c.id == conversation_id)

    async def tree(self, conversation_id: str) -> Conversation:
        self.tree_reads += 1
        return replace(self._find(conversation_id), messages=list(self.tree_messages))

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation:
        self.leaf_moves.append(message_id)
        return self._find(conversation_id)

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation:
        self.model_updates.append((conversation_id, model))
        conv = replace(self._find(conversation_id), default_model=model)
        self.conversations = [conv if c.id == conversation_id else c for c in self.conversations]
        return conv

    async def context(self, conversation_id: str) -> ConversationContext:
        return ConversationContext("grt-nano", 8192, 100, False)

    async def compact(self, conversation_id: str) -> ConversationMessage:
        """Fold the whole tree into one summary, as the server would."""
        if not self.tree_messages:
            raise ConflictError("Nothing to compact yet.", status_code=409, request_id="req-409")
        self.compacted.append(conversation_id)
        first, last = self.tree_messages[0], self.tree_messages[-1]
        summary = message(
            "s1", "system", "", parent=last.id, created_at="9",
            parts=[{"type": "summary", "text": "They talked.", "covers_from": first.id,
                    "covers_to": last.id, "tokens_before": 900, "tokens_after": 120}],
        )
        self.tree_messages.append(summary)
        return summary

    async def _play(self) -> AsyncIterator[StreamItem]:
        try:
            if self.refusal is not None:
                raise self.refusal
            for item in self.script:
                await asyncio.sleep(0)
                yield item
            if self.hold:
                await asyncio.Event().wait()
        finally:
            self.streams_closed += 1

    def send(self, conversation_id, content, model=None) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, content, model))
        return self._play()

    def edit(self, conversation_id, message_id, content, model=None) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, f"edit:{message_id}:{content}", model))
        return self._play()

    def regenerate(self, conversation_id, message_id, model=None) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, f"regen:{message_id}", model))
        return self._play()

    async def aclose(self) -> None:
        return None


def happy_script(text: str = "Hello there") -> list[StreamItem]:
    user = message("u1", "user", "hi")
    done = message("a1", "assistant", text, parent="u1", model="grt-nano")
    return [
        Opened("req-ok"),
        fr.MessageFrame(user),
        fr.TitleFrame("hi"),
        fr.PartStartFrame(0, "text"),
        fr.DeltaFrame(0, text[:5]),
        fr.DeltaFrame(0, text[5:]),
        fr.PartEndFrame(0),
        fr.ContextFrame(150, 20, 8192),
        fr.DoneFrame(done),
    ]
