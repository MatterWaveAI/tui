"""An in-memory `ChatAPI` that streams scripted frames."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import ConflictError
from matterwave_sdk.models import (
    Attachment,
    Conversation,
    ConversationContext,
    ConversationMessage,
    Me,
    UsageBalance,
)

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


def usage_balance(**fields: Any) -> UsageBalance:
    return UsageBalance.from_dict(
        {
            "unit": "tokens",
            "used": 1234,
            "included": 100000,
            "remaining": 98766,
            "percent": 1.234,
            "warn": False,
            "in_overage": False,
            "exhausted": False,
            "period_start": "2026-10-01",
            "resets_at": "2026-11-01",
            "plan": {"name": "Free"},
            "overage": {},
            **fields,
        }
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
        self.balance = usage_balance()
        self.sent: list[tuple[str, str, str | None]] = []
        self.sent_attachment_ids: list[list[str] | None] = []
        self.leaf_moves: list[str] = []
        self.model_updates: list[tuple[str, str | None]] = []
        self.searches: list[str] = []
        self.compacted: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.pins: list[tuple[str, bool]] = []
        self.archives: list[tuple[str, bool]] = []
        self.forks: list[tuple[str, str]] = []
        self.shares: list[str] = []
        self.uploads: list[tuple[str, bytes]] = []
        self.tree_reads = 0
        self.streams_closed = 0

    async def me(self) -> Me:
        return Me.from_dict({"subject": "u", "kind": "user"})

    async def models(self) -> dict[str, Any]:
        return {"data": MODELS, "default_model": "grt-nano"}

    async def list_conversations(self, filter: str = "default") -> list[Conversation]:
        if filter == "all":
            return list(self.conversations)
        if filter == "pinned":
            return [c for c in self.conversations if c.pinned_at and not c.archived_at]
        if filter == "archived":
            return [c for c in self.conversations if c.archived_at]
        return [c for c in self.conversations if not c.archived_at]

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

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        self.renames.append((conversation_id, title))
        return self._replace(conversation_id, title=title)

    async def delete_conversation(self, conversation_id: str) -> None:
        self.deletes.append(conversation_id)
        self.conversations = [c for c in self.conversations if c.id != conversation_id]

    async def set_pinned(self, conversation_id: str, pinned: bool) -> Conversation:
        self.pins.append((conversation_id, pinned))
        return self._replace(
            conversation_id, pinned_at="2026-10-27T00:00:00Z" if pinned else None
        )

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation:
        self.archives.append((conversation_id, archived))
        return self._replace(
            conversation_id, archived_at="2026-10-27T00:00:00Z" if archived else None
        )

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation:
        self.forks.append((conversation_id, message_id))
        source = self._find(conversation_id)
        forked = conversation(
            f"c{len(self.conversations) + 1}", title=title or f"Fork of {source.title}"
        )
        self.conversations.insert(0, forked)
        return forked

    async def share_conversation(self, conversation_id: str) -> str:
        self.shares.append(conversation_id)
        return f"https://matterwave.test/share/tok-{conversation_id}"

    async def usage(self) -> UsageBalance:
        return self.balance

    async def upload_attachment(
        self, data: bytes, filename: str, conversation_id: str | None = None
    ) -> Attachment:
        self.uploads.append((filename, data))
        return Attachment.from_dict(
            {"id": f"att{len(self.uploads)}", "filename": filename,
             "content_type": "application/octet-stream", "size_bytes": len(data),
             "scan_status": "clean", "created_at": "1"}
        )

    def _replace(self, conversation_id: str, **fields: Any) -> Conversation:
        conv = replace(self._find(conversation_id), **fields)
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

    def send(
        self, conversation_id, content, model=None, attachment_ids=None
    ) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, content, model))
        self.sent_attachment_ids.append(attachment_ids)
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
