"""An in-memory `ChatAPI` that streams scripted frames, and `FakeV1`: a
scripted `/v1` (with the routes an agent run touches) under the real adapter."""

from __future__ import annotations

import asyncio
import copy
import json as _json
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import replace
from typing import Any

import httpx
from matterwave_sdk import frames as fr
from matterwave_sdk.errors import ConflictError
from matterwave_sdk.models import (
    Attachment,
    AttachmentLimits,
    Conversation,
    ConversationContext,
    ConversationMessage,
    ConversationPreset,
    FiledReport,
    Me,
    UsageBalance,
)

from matterwave_tui.api import NOTIFICATIONS_PAGE, HttpChatAPI, Opened, SearchPage, StreamItem

# The fake's search answers this many hits a page, so paging is exercised.
SEARCH_PAGE = 2

MODELS: list[dict[str, Any]] = [
    {
        "id": "grt-nano",
        "label": "GRT Nano",
        "partner": False,
        "research_preview": False,
        "included": True,
        "upgrade_to": None,
        "context_window": 8192,
        "input_usd_per_m": 0.5,
        "output_usd_per_m": 1.5,
    },
    {
        "id": "grt-max",
        "label": "GRT Max",
        "partner": False,
        "research_preview": False,
        "included": False,
        "upgrade_to": "Pro",
        "context_window": 131072,
        "supports_vision": False,
    },
    {
        "id": "other-7b",
        "label": "Other 7B",
        "partner": True,
        "research_preview": False,
        "included": True,
        "upgrade_to": None,
        "context_window": None,
        "supports_vision": True,
    },
    {
        "id": "lab-x",
        "label": "Lab X",
        "partner": False,
        "research_preview": True,
        "included": True,
        "upgrade_to": None,
        "context_window": 32768,
    },
]

ORG_INSTRUCTIONS_TEXT = "Be brief."
DEFAULT_INSTRUCTIONS: dict[str, Any] = {
    "org": ORG_INSTRUCTIONS_TEXT,
    "user": {"about": "", "style": ""},
    "conversation": None,
    "user_overridden": False,
    "effective": ORG_INSTRUCTIONS_TEXT,
}


def conversation(cid: str = "c1", **fields: Any) -> Conversation:
    return Conversation.from_dict(
        {
            "id": cid,
            "title": "New chat",
            "created_at": "1",
            "updated_at": "1",
            "default_model": "grt-nano",
            **fields,
        }
    )


def message(
    mid: str, role: str, text: str, parent: str | None = None, created_at: str = "1", **extra: Any
) -> ConversationMessage:
    parts = extra.pop("parts", [{"type": "text", "text": text}])
    return ConversationMessage.from_dict(
        {
            "id": mid,
            "conversation_id": "c1",
            "role": role,
            "parts": parts,
            "content": text,
            "created_at": created_at,
            "parent_id": parent,
            **extra,
        }
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


def notification(nid: str = "n1", **fields: Any) -> dict[str, Any]:
    return {
        "id": nid,
        "kind": "budget.warn",
        "title": "80% of this period's included tokens used",
        "body": "The organisation is approaching the tokens its plan includes.",
        "link": "/settings/usage",
        "created_at": "2026-10-29T00:00:00Z",
        "read_at": None,
        **fields,
    }


def announcement(aid: str = "an1", **fields: Any) -> dict[str, Any]:
    return {
        "id": aid,
        "message": "Maintenance tonight at 22:00 UTC.",
        "severity": "info",
        "link": None,
        "starts_at": "2026-10-29T00:00:00Z",
        "ends_at": None,
        "dismissible": True,
        **fields,
    }


def share_row(sid: str = "s1", **fields: Any) -> dict[str, Any]:
    return {
        "id": sid,
        "conversation_id": "c1",
        "title": "New chat",
        "message_count": 2,
        "include_attachments": False,
        "created_at": "2026-10-29T00:00:00Z",
        "expires_at": None,
        "revoked_at": None,
        "view_count": 0,
        "status": "active",
        **fields,
    }


class FakeAPI:
    def __init__(
        self,
        script: list[StreamItem] | None = None,
        *,
        hold: bool = False,
        refusal: Exception | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self.script = script or []
        # `hold`: the stream never ends on its own, for the Esc test;
        # `refusal`: the request is refused before any frame; `gate`: the
        # stream waits before its first item until the test sets the event.
        self.hold = hold
        self.refusal = refusal
        self.gate = gate
        self.conversations: list[Conversation] = [conversation()]
        self.models_payload: list[dict[str, Any]] = MODELS
        self.tree_messages: list[ConversationMessage] = []
        self.balance = usage_balance()
        self.sent: list[tuple[str, str, str | None]] = []
        self.sent_attachment_ids: list[list[str] | None] = []
        self.leaf_moves: list[str] = []
        self.model_updates: list[tuple[str, str | None]] = []
        self.preset_rows: list[dict[str, Any]] = []
        self.retired_presets: dict[str, dict[str, Any]] = {}
        self.preset_updates: list[tuple[str, str | None]] = []
        self.created_presets: list[str | None] = []
        self.searches: list[str] = []
        self.all_reads = 0
        self.folder_renames: list[tuple[str, str]] = []
        self.folder_deletes: list[str] = []
        self.tag_deletes: list[str] = []
        self.compacted: list[str] = []
        self.renames: list[tuple[str, str]] = []
        self.deletes: list[str] = []
        self.pins: list[tuple[str, bool]] = []
        self.archives: list[tuple[str, bool]] = []
        self.forks: list[tuple[str, str]] = []
        self.shares: list[str] = []
        self.share_options: list[tuple[int | None, bool]] = []
        self.temperatures_sent: list[float | None] = []
        self.limits = AttachmentLimits(
            max_upload_bytes=1000, quota_bytes=5000, quota_used_bytes=0, quota_remaining_bytes=5000
        )
        self.uploads: list[tuple[str, bytes]] = []
        # The malware scan: what an upload answers, then what each poll of
        # its scan answers in turn (the last one repeats); `scan_gate`, when
        # set, holds every poll until the test sets the event.
        self.upload_scan_status = "clean"
        self.scan_answers: list[str] = []
        self.scan_polls: list[str] = []
        self.scan_gate: asyncio.Event | None = None
        self.attachment_deletes: list[str] = []
        # Temporary chats are fixed at creation; the server keeps them out
        # of every list and every search, and refuses pin, fork and share.
        self.creates: list[tuple[str | None, str | None, bool]] = []
        self.temporary_ids: set[str] = set()
        # Per-conversation custom instructions.
        self.instructions_state: dict[str, dict[str, Any]] = {}
        self.instructions_puts: list[tuple[str, str | None]] = []
        # The caller's own ratings, by message id.
        self.feedback_puts: list[tuple[str, int, list[str], str | None]] = []
        self.feedback_deletes: list[str] = []
        self.feedback_rows: list[dict[str, Any]] = []
        # Folders and tags, held the way the server holds them: the
        # routes write here and `library` reads back, so the UI's
        # captions can be tested against a state, not a recorder.
        self.folders_created: list[str] = []
        self.library_folders: list[dict[str, Any]] = []
        self.folder_moves: list[tuple[str, str | None]] = []
        self.tags_created: list[str] = []
        self.library_tags: list[dict[str, Any]] = []
        self.tag_sets: list[tuple[str, list[str]]] = []
        self.library_assignments: dict[str, list[str]] = {}
        # Notifications, announcements, reports, share management, budget.
        self.notification_rows: list[dict[str, Any]] = []
        self.notifications_read: list[str] = []
        self.notifications_read_all = 0
        self.announcement_rows: list[dict[str, Any]] = []
        self.reports: list[tuple[str, str, str | None, str | None]] = []
        self.share_rows: list[dict[str, Any]] = []
        self.shares_revoked: list[str] = []
        # Signed-in accounts (slot 0 acting), the switch, the attachments
        # the transcript asks to download, and imports with their polling.
        self.account_rows: list[dict[str, Any]] = []
        self.switches: list[int] = []
        self.switched_to: dict[str, Any] = {}
        self.attachments_store: dict[str, bytes] = {}
        self.downloaded: list[str] = []
        self.import_rows: list[dict[str, Any]] = []
        self.imports_started: list[tuple[str, bytes]] = []
        self.import_status_calls: list[str] = []
        self.import_done_after: int | None = None
        # And the account export, a job polled the same way.
        self.export_rows: list[dict[str, Any]] = []
        self.exports_started: list[str] = []
        self.export_status_calls: list[str] = []
        self.export_done_after: int | None = None
        self.export_downloads: list[str] = []
        self.export_document = b'{"conversations": []}'
        self.transcribes: list[tuple[str, str, str | None, bytes]] = []
        self.transcript_text = "hello there"
        self.budget_payload: dict[str, Any] = {
            "period_start": "2026-10-01",
            "included_tokens": 100000,
            "tokens_used": 1234,
            "remaining_tokens": 98766,
            "overage_enabled": False,
            "overage_cap_micros": 0,
            "overage_micros": 0,
            "member_default_cap_micros": None,
            "member_cap": None,
            "member_spent": 0,
            "member_exhausted": False,
        }
        self.tree_reads = 0
        # The person's own account: keys (this terminal's is `mw_mine0000`),
        # browser sessions, preferences, instructions and the plan.
        self.key_rows: list[dict[str, Any]] = [
            {
                "id": "k1",
                "prefix": "mw_mine0000",
                "name": "matterwave-tui",
                "kind": "device",
                "revoked_at": None,
                "last_used_at": "2026-10-31T00:00:00Z",
            },
            {
                "id": "k2",
                "prefix": "mw_ci000000",
                "name": "CI",
                "kind": "user",
                "revoked_at": None,
                "last_used_at": None,
            },
        ]
        self.keys_revoked: list[str] = []
        self.keys_rotated: list[str] = []
        self.session_rows: list[dict[str, Any]] = [
            {
                "id": "s1",
                "current": False,
                "created_at": "2026-10-01T00:00:00Z",
                "last_seen_at": "2026-10-30T00:00:00Z",
                "user_agent": "Firefox",
                "ip": "10.0.0.2",
            },
        ]
        self.sessions_revoked: list[str] = []
        self.preferences_state: dict[str, Any] = {
            "email": {"product_updates": False, "usage_alerts": True, "billing_receipts": True},
            "locale": None,
            "notifications": {"budget_warn": True},
        }
        self.preferences_puts: list[dict[str, Any]] = []
        self.user_instructions_state = {"about": "", "style": ""}
        self.user_instructions_puts: list[tuple[str, str]] = []
        self.usage_reads = 0
        self.budget_reads = 0
        self.streams_closed = 0
        self.web_searches: list[tuple[str, int]] = []
        self.web_search_results: list[dict[str, Any]] = [
            {
                "title": "MatterWave Docs",
                "url": "https://docs.matterwave.test/search",
                "snippet": "Documentation for MatterWave web search and citations.",
            }
        ]
        self.rag_collections: list[dict[str, Any]] = [
            {"id": "col-docs", "name": "Engineering Docs", "document_count": 12}
        ]
        self.rag_queries: list[tuple[str, str, int]] = []
        self.rag_results: list[dict[str, Any]] = [
            {
                "id": "chunk-1",
                "score": 0.94,
                "text": "Architecture overview for the RAG pipeline.",
                "source": "architecture.md",
            }
        ]
        self.mcp_server_rows: list[dict[str, Any]] = [
            {
                "id": "srv-github",
                "name": "GitHub MCP",
                "status": "connected",
                "tools": [
                    {"name": "list_issues", "description": "List repository issues"},
                    {"name": "get_pull_request", "description": "Fetch pull request details"},
                ],
            }
        ]
        self.mcp_tool_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.edit_sessions_created: list[tuple[str, list[str]]] = []
        self.agent_run_rows: list[dict[str, Any]] = [
            {
                "id": "run-101",
                "agent_id": "agent-coder",
                "status": "running",
                "goal": "Implement parity slash commands",
            },
            {
                "id": "run-100",
                "agent_id": "agent-reviewer",
                "status": "completed",
                "goal": "Verify SDK dependency floor",
            },
        ]
        self.agent_runs_queries: list[tuple[str | None, str | None]] = []
        self.connector_catalog_queries: list[tuple[str | None, str | None]] = []
        self.connector_catalog_rows: list[dict[str, Any]] = [
            {
                "provider_id": "github",
                "name": "GitHub",
                "category": "developer",
                "auth_types": ["oauth2", "pat"],
                "api_base_url": "https://api.github.com",
            },
            {
                "provider_id": "slack",
                "name": "Slack",
                "category": "communication",
                "auth_types": ["oauth2"],
                "api_base_url": "https://slack.com/api",
            },
        ]
        self.oauth_proxy_calls: list[tuple[str, str, str, dict[str, Any] | None]] = []
        self.decision_trail_queries: list[str] = []
        self.decision_trail_nodes: list[dict[str, Any]] = [
            {
                "id": "node-1",
                "type": "plan",
                "label": "Select search and analysis tools",
                "step_id": "step-1",
                "confidence": 0.96,
                "outcome": "completed",
                "rationale": "High-confidence tool selection for repository audit.",
            },
            {
                "id": "node-2",
                "type": "tool_call",
                "label": "Run static verification",
                "step_id": "step-2",
                "confidence": 0.92,
                "outcome": "completed",
                "rationale": "All checks passed with zero warnings.",
            },
        ]
        self.replay_calls: list[tuple[str, str]] = []
        self.replay_differences: list[dict[str, Any]] = [
            {
                "step_id": "step-2",
                "field": "tool_output",
                "original": "cached-v1",
                "replayed": "recomputed-v2",
                "significance": "minor",
            }
        ]
        self.checkpoint_queries: list[str] = []
        self.checkpoint_rows: list[dict[str, Any]] = [
            {
                "checkpoint_id": "ckpt-1",
                "run_id": "run-101",
                "step_id": "step-1",
                "step_index": 1,
                "timestamp": "2026-11-03T12:00:00Z",
            },
            {
                "checkpoint_id": "ckpt-2",
                "run_id": "run-101",
                "step_id": "step-2",
                "step_index": 2,
                "timestamp": "2026-11-03T12:01:00Z",
            },
        ]
        self.resume_run_calls: list[tuple[str, str | None]] = []
        self.reliability_queries: list[str | None] = []
        self.reliability_payload: dict[str, Any] = {
            "sr": 0.98,
            "cr": 0.96,
            "pc": 0.94,
            "hir": 0.04,
            "ma": 0.95,
            "tcl": 142.5,
            "wct": 1280.0,
            "total_runs": 50,
            "window": "7d",
        }
        self.guardrail_evaluations: list[tuple[str, str]] = []
        self.tool_plans: list[str] = []

    async def me(self) -> Me:
        return Me.from_dict({"subject": "u", "kind": "user"})

    async def models(self) -> dict[str, Any]:
        return {"data": self.models_payload, "default_model": "grt-nano"}

    async def presets(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.preset_rows]

    async def preset(self, preset_id: str) -> dict[str, Any]:
        for row in self.preset_rows:
            if row.get("id") == preset_id:
                return dict(row)
        if preset_id in self.retired_presets:
            return dict(self.retired_presets[preset_id])
        raise LookupError(preset_id)

    def _preset_summary(self, preset_id: str | None) -> ConversationPreset | None:
        if not preset_id:
            return None
        for row in self.preset_rows:
            if row.get("id") == preset_id:
                return ConversationPreset(id=preset_id, name=str(row.get("name") or preset_id))
        if preset_id in self.retired_presets:
            row = self.retired_presets[preset_id]
            return ConversationPreset(id=preset_id, name=str(row.get("name") or preset_id))
        return ConversationPreset(id=preset_id, name=preset_id)

    async def list_conversations(self, filter: str = "default") -> list[Conversation]:
        kept = [c for c in self.conversations if c.id not in self.temporary_ids]
        if filter == "all":
            return list(kept)
        if filter == "pinned":
            return [c for c in kept if c.pinned_at and not c.archived_at]
        if filter == "archived":
            return [c for c in kept if c.archived_at]
        return [c for c in kept if not c.archived_at]

    async def all_conversations(self, filter: str = "default") -> list[Conversation]:
        self.all_reads += 1
        return await self.list_conversations(filter)

    async def search(self, query: str, cursor: str | None = None) -> SearchPage:
        self.searches.append(query)
        hits = [
            c
            for c in self.conversations
            if c.id not in self.temporary_ids and query.lower() in c.title.lower()
        ]
        start = int(cursor or 0)
        page = hits[start : start + SEARCH_PAGE]
        more = start + SEARCH_PAGE < len(hits)
        return SearchPage(page, str(start + SEARCH_PAGE) if more else None)

    async def create_conversation(
        self,
        title=None,
        model=None,
        temporary: bool = False,
        preset_id: str | None = None,
    ) -> Conversation:
        self.creates.append((title, model, temporary))
        self.created_presets.append(preset_id)
        preset_summary = self._preset_summary(preset_id)
        conv = conversation(
            f"c{len(self.conversations) + 1}",
            title=title or "New chat",
            default_model=model,
            preset_id=preset_id,
            preset={"id": preset_summary.id, "name": preset_summary.name}
            if preset_summary is not None
            else None,
        )
        if temporary:
            self.temporary_ids.add(conv.id)
        self.conversations.insert(0, conv)
        return conv

    def _find(self, conversation_id: str) -> Conversation:
        return next(c for c in self.conversations if c.id == conversation_id)

    def _refuse_temporary(self, conversation_id: str, verb: str) -> None:
        if conversation_id in self.temporary_ids:
            raise ConflictError(
                f"A temporary conversation is not kept, so it cannot be {verb}.",
                status_code=409,
                request_id="req-temporary",
            )

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

    async def set_preset(self, conversation_id: str, preset_id: str | None) -> Conversation:
        self.preset_updates.append((conversation_id, preset_id))
        conv = replace(
            self._find(conversation_id),
            preset_id=preset_id,
            preset=self._preset_summary(preset_id),
        )
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
        self._refuse_temporary(conversation_id, "pinned")
        return self._replace(conversation_id, pinned_at="2026-10-27T00:00:00Z" if pinned else None)

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation:
        self.archives.append((conversation_id, archived))
        return self._replace(
            conversation_id, archived_at="2026-10-27T00:00:00Z" if archived else None
        )

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation:
        self._refuse_temporary(conversation_id, "forked")
        self.forks.append((conversation_id, message_id))
        source = self._find(conversation_id)
        forked = conversation(
            f"c{len(self.conversations) + 1}", title=title or f"Fork of {source.title}"
        )
        self.conversations.insert(0, forked)
        return forked

    async def share_conversation(
        self,
        conversation_id: str,
        *,
        expires_in_s: int | None = None,
        include_attachments: bool = False,
    ) -> str:
        self._refuse_temporary(conversation_id, "shared")
        self.shares.append(conversation_id)
        self.share_options.append((expires_in_s, include_attachments))
        self.share_rows.insert(
            0,
            share_row(
                f"s{len(self.share_rows) + 1}",
                conversation_id=conversation_id,
                title=self._find(conversation_id).title,
            ),
        )
        return f"https://matterwave.test/share/tok-{conversation_id}"

    async def conversation_instructions(self, conversation_id: str) -> dict[str, Any]:
        return dict(self.instructions_state.get(conversation_id) or DEFAULT_INSTRUCTIONS)

    async def set_conversation_instructions(
        self, conversation_id: str, text: str | None
    ) -> dict[str, Any]:
        self.instructions_puts.append((conversation_id, text))
        instructions = dict(DEFAULT_INSTRUCTIONS)
        instructions["conversation"] = text
        instructions["user_overridden"] = bool(text)
        instructions["effective"] = text or ORG_INSTRUCTIONS_TEXT
        self.instructions_state[conversation_id] = instructions
        return instructions

    async def set_feedback(
        self, message_id: str, rating: int, reasons: Sequence[str] = (), comment: str | None = None
    ) -> dict[str, Any]:
        self.feedback_puts.append((message_id, rating, list(reasons), comment))
        row = {
            "message_id": message_id,
            "rating": rating,
            "reasons": list(reasons),
            "comment": comment,
        }
        self.feedback_rows = [
            r for r in self.feedback_rows if r.get("message_id") != message_id
        ] + [row]
        return dict(row)

    async def delete_feedback(self, message_id: str) -> dict[str, Any]:
        self.feedback_deletes.append(message_id)
        self.feedback_rows = [r for r in self.feedback_rows if r.get("message_id") != message_id]
        return {"status": "removed"}

    async def conversation_feedback(self, conversation_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.feedback_rows]

    async def usage(self) -> UsageBalance:
        self.usage_reads += 1
        return self.balance

    async def upload_attachment(
        self, data: bytes, filename: str, conversation_id: str | None = None
    ) -> Attachment:
        self.uploads.append((filename, data))
        return Attachment.from_dict(
            {
                "id": f"att{len(self.uploads)}",
                "filename": filename,
                "content_type": "application/octet-stream",
                "size_bytes": len(data),
                "scan_status": self.upload_scan_status,
                "created_at": "1",
            }
        )

    async def attachment_limits(self) -> AttachmentLimits:
        return self.limits

    async def attachment_scan_status(self, attachment_id: str) -> str:
        if self.scan_gate is not None:
            await self.scan_gate.wait()
        self.scan_polls.append(attachment_id)
        if len(self.scan_answers) > 1:
            return self.scan_answers.pop(0)
        return self.scan_answers[0] if self.scan_answers else "clean"

    async def delete_attachment(self, attachment_id: str) -> None:
        self.attachment_deletes.append(attachment_id)

    async def library(self) -> dict[str, Any]:
        return {
            "folders": [dict(folder) for folder in self.library_folders],
            "tags": [dict(tag) for tag in self.library_tags],
            "assignments": {cid: list(ids) for cid, ids in self.library_assignments.items()},
        }

    async def create_folder(self, name: str) -> dict[str, Any]:
        if any(folder["name"] == name for folder in self.library_folders):
            raise ConflictError(
                "You already have one with that name.",
                status_code=409,
                request_id="req-name-taken",
            )
        self.folders_created.append(name)
        folder = {
            "id": f"f{len(self.library_folders) + 1}",
            "name": name,
            "position": 0,
            "created_at": "1",
            "updated_at": "1",
        }
        self.library_folders.append(folder)
        return dict(folder)

    async def rename_folder(self, folder_id: str, name: str) -> dict[str, Any]:
        if any(f["name"] == name and f["id"] != folder_id for f in self.library_folders):
            raise ConflictError(
                "You already have one with that name.",
                status_code=409,
                request_id="req-name-taken",
            )
        self.folder_renames.append((folder_id, name))
        for folder in self.library_folders:
            if folder["id"] == folder_id:
                folder["name"] = name
                return dict(folder)
        raise LookupError(folder_id)

    async def delete_folder(self, folder_id: str) -> None:
        self.folder_deletes.append(folder_id)
        self.library_folders = [f for f in self.library_folders if f["id"] != folder_id]
        self.conversations = [
            replace(c, folder_id=None) if c.folder_id == folder_id else c
            for c in self.conversations
        ]

    async def delete_tag(self, tag_id: str) -> None:
        self.tag_deletes.append(tag_id)
        self.library_tags = [t for t in self.library_tags if t["id"] != tag_id]
        self.library_assignments = {
            cid: [t for t in ids if t != tag_id] for cid, ids in self.library_assignments.items()
        }

    async def move_to_folder(self, conversation_id: str, folder_id: str | None) -> None:
        self.folder_moves.append((conversation_id, folder_id))
        self._replace(conversation_id, folder_id=folder_id)

    async def create_tag(self, name: str) -> dict[str, Any]:
        self.tags_created.append(name)
        for tag in self.library_tags:
            if tag["name"] == name:
                return dict(tag)
        tag = {"id": f"t{len(self.library_tags) + 1}", "name": name, "created_at": "1"}
        self.library_tags.append(tag)
        return dict(tag)

    async def set_tags(self, conversation_id: str, tag_ids: Sequence[str]) -> dict[str, Any]:
        self.tag_sets.append((conversation_id, list(tag_ids)))
        self.library_assignments[conversation_id] = list(tag_ids)
        return {"conversation_id": conversation_id, "tag_ids": list(tag_ids)}

    # --- notifications, announcements, reports, shares, budget ----------------

    def _unread_count(self) -> int:
        return sum(1 for row in self.notification_rows if not row.get("read_at"))

    async def notifications(
        self, *, unread: bool = False, limit: int = NOTIFICATIONS_PAGE
    ) -> dict[str, Any]:
        rows = [dict(row) for row in self.notification_rows if not unread or not row.get("read_at")]
        return {"data": rows[:limit], "unread_count": self._unread_count()}

    async def read_notification(self, notification_id: str) -> dict[str, Any]:
        self.notifications_read.append(notification_id)
        for row in self.notification_rows:
            if row.get("id") == notification_id:
                row["read_at"] = row.get("read_at") or "2026-10-30T00:00:00Z"
                return {**row, "unread_count": self._unread_count()}
        raise LookupError(notification_id)

    async def read_all_notifications(self) -> dict[str, Any]:
        count = self._unread_count()
        self.notifications_read_all += 1
        for row in self.notification_rows:
            row["read_at"] = row.get("read_at") or "2026-10-30T00:00:00Z"
        return {"read": count, "unread_count": 0}

    async def announcements(self) -> dict[str, Any]:
        return {"data": [dict(row) for row in self.announcement_rows]}

    async def report_conversation(
        self,
        conversation_id: str,
        kind: str,
        note: str | None = None,
        message_id: str | None = None,
    ) -> FiledReport:
        self.reports.append((conversation_id, kind, message_id, note))
        return FiledReport.from_dict({"id": f"rep{len(self.reports)}", "kind": kind})

    async def conversation_shares(self, conversation_id: str) -> list[dict[str, Any]]:
        return [
            dict(row) for row in self.share_rows if row.get("conversation_id") == conversation_id
        ]

    async def my_shares(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.share_rows]

    async def revoke_share(self, share_id: str) -> dict[str, Any]:
        self.shares_revoked.append(share_id)
        for row in self.share_rows:
            if row.get("id") == share_id:
                row["status"] = "revoked"
                row["revoked_at"] = "2026-10-30T00:00:00Z"
        return {"status": "revoked"}

    async def accounts(self) -> dict[str, Any]:
        return {
            "accounts": [dict(row) for row in self.account_rows],
            "max": 4,
            "room": True,
        }

    async def switch_account(self, slot: int) -> dict[str, Any]:
        self.switches.append(slot)
        for row in self.account_rows:
            acting = row.get("slot") == 0
            if row.get("slot") == slot:
                row["slot"] = 0
                row["current"] = True
                self.switched_to = dict(row)
            elif acting:
                row["slot"] = slot
                row["current"] = False
        return {
            "status": "ok",
            "subject": str(self.switched_to.get("subject") or ""),
            "email": self.switched_to.get("email"),
        }

    async def download_attachment(self, attachment_id: str) -> bytes:
        self.downloaded.append(attachment_id)
        return self.attachments_store[attachment_id]

    async def start_import(self, data: bytes, filename: str) -> dict[str, Any]:
        self.imports_started.append((filename, data))
        row = {
            "id": f"imp{len(self.import_rows) + 1}",
            "status": "pending",
            "source": "chatgpt",
            "filename": filename,
            "size_bytes": len(data),
            "conversations_total": 2,
            "conversations_done": 0,
            "conversations_created": 0,
            "conversations_existing": 0,
            "conversations_skipped": 0,
            "messages_created": 0,
            "attachments_skipped": 0,
            "branches_dropped": 0,
            "error": None,
            "created_at": "2026-10-30T00:00:00Z",
            "started_at": None,
            "completed_at": None,
        }
        self.import_rows.append(row)
        return dict(row)

    async def import_status(self, import_id: str) -> dict[str, Any]:
        self.import_status_calls.append(import_id)
        for row in self.import_rows:
            if row.get("id") == import_id:
                # A poll budget that runs the job to `done` when the test
                # asks for it; the row keeps its counts.
                if self.import_done_after is not None and (
                    len(self.import_status_calls) >= self.import_done_after
                ):
                    row["status"] = "done"
                    row["conversations_created"] = 2
                    row["conversations_done"] = 2
                    row["messages_created"] = 9
                return dict(row)
        raise LookupError(import_id)

    async def start_export(self) -> dict[str, Any]:
        self.exports_started.append("asked")
        row = {
            "id": f"exp{len(self.export_rows) + 1}",
            "status": "pending",
            "size_bytes": None,
            "created_at": "2026-10-30T00:00:00Z",
            "completed_at": None,
            "expires_at": None,
            "download_url": None,
        }
        self.export_rows.append(row)
        return dict(row)

    async def export_status(self, export_id: str) -> dict[str, Any]:
        self.export_status_calls.append(export_id)
        for row in self.export_rows:
            if row.get("id") == export_id:
                # A poll budget that runs the job to `ready` when the test
                # asks for it, with the signed link the real row carries.
                if self.export_done_after is not None and (
                    len(self.export_status_calls) >= self.export_done_after
                ):
                    row["status"] = "ready"
                    row["size_bytes"] = len(self.export_document)
                    row["completed_at"] = "2026-10-30T00:00:05Z"
                    row["expires_at"] = "2026-11-06T00:00:05Z"
                    row["download_url"] = (
                        f"/api/me/export/{export_id}/download?expires=1&sig=signed"
                    )
                return dict(row)
        raise LookupError(export_id)

    async def export_download(self, path: str) -> bytes:
        self.export_downloads.append(path)
        return self.export_document

    async def transcribe(
        self, data: bytes, filename: str, content_type: str, language: str | None = None
    ) -> str:
        self.transcribes.append((filename, content_type, language, data))
        return self.transcript_text

    async def budget(self) -> dict[str, Any]:
        self.budget_reads += 1
        return dict(self.budget_payload)

    # --- the person's own account ----------------------------------------------------

    def own_key_prefix(self) -> str | None:
        return "mw_mine0000"

    async def api_keys(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.key_rows]

    async def rotate_key(self, key_id: str) -> dict[str, Any]:
        self.keys_rotated.append(key_id)
        row = next(r for r in self.key_rows if r["id"] == key_id)
        return {**row, "key": "mw_rotated1_testsecret"}

    async def revoke_key(self, key_id: str) -> None:
        self.keys_revoked.append(key_id)
        for row in self.key_rows:
            if row["id"] == key_id:
                row["revoked_at"] = "2026-10-31T01:00:00Z"

    async def sessions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.session_rows]

    async def revoke_session(self, session_id: str) -> None:
        self.sessions_revoked.append(session_id)
        self.session_rows = [r for r in self.session_rows if r["id"] != session_id]

    async def preferences(self) -> dict[str, Any]:
        return copy.deepcopy(self.preferences_state)

    async def set_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        self.preferences_puts.append(copy.deepcopy(preferences))
        self.preferences_state = copy.deepcopy(preferences)
        return copy.deepcopy(preferences)

    async def user_instructions(self) -> dict[str, str]:
        return dict(self.user_instructions_state)

    async def set_user_instructions(self, about: str, style: str) -> dict[str, str]:
        self.user_instructions_puts.append((about, style))
        self.user_instructions_state = {"about": about, "style": style}
        return dict(self.user_instructions_state)

    async def entitlements(self) -> dict[str, Any]:
        return {
            "plan": "pro",
            "plan_name": "Pro",
            "status": "active",
            "per_seat": False,
            "seats": 1,
            "writes_blocked": False,
        }

    async def billing_status(self) -> dict[str, Any]:
        return {
            "billing_enabled": True,
            "collection_method": "charge_automatically",
            "dunning": None,
        }

    async def plans(self) -> list[dict[str, Any]]:
        return [
            {"code": "free", "name": "Free", "price_cents_monthly": 0, "per_seat": False},
            {"code": "pro", "name": "Pro", "price_cents_monthly": 2000, "per_seat": False},
        ]

    async def enforcement(self) -> dict[str, Any]:
        return {"state": "none"}

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
            "s1",
            "system",
            "",
            parent=last.id,
            created_at="9",
            parts=[
                {
                    "type": "summary",
                    "text": "They talked.",
                    "covers_from": first.id,
                    "covers_to": last.id,
                    "tokens_before": 900,
                    "tokens_after": 120,
                }
            ],
        )
        self.tree_messages.append(summary)
        return summary

    async def _play(self) -> AsyncIterator[StreamItem]:
        try:
            if self.refusal is not None:
                raise self.refusal
            for i, item in enumerate(self.script):
                if self.gate is not None and i == 0:
                    await self.gate.wait()
                await asyncio.sleep(0)
                yield item
            if self.hold:
                await asyncio.Event().wait()
        finally:
            self.streams_closed += 1

    def send(
        self, conversation_id, content, model=None, attachment_ids=None, temperature=None
    ) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, content, model))
        self.sent_attachment_ids.append(attachment_ids)
        self.temperatures_sent.append(temperature)
        return self._play()

    def edit(
        self, conversation_id, message_id, content, model=None, temperature=None
    ) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, f"edit:{message_id}:{content}", model))
        self.temperatures_sent.append(temperature)
        return self._play()

    def regenerate(
        self, conversation_id, message_id, model=None, temperature=None
    ) -> AsyncIterator[StreamItem]:
        self.sent.append((conversation_id, f"regen:{message_id}", model))
        self.temperatures_sent.append(temperature)
        return self._play()

    async def search_web(self, query: str, max_results: int = 5) -> dict[str, Any]:
        self.web_searches.append((query, max_results))
        return {
            "query": query,
            "results": [dict(r) for r in self.web_search_results[:max_results]],
        }

    async def query_rag(self, collection_id: str, query: str, top_k: int = 5) -> dict[str, Any]:
        self.rag_queries.append((collection_id, query, top_k))
        return {
            "collection_id": collection_id,
            "query": query,
            "results": [dict(r) for r in self.rag_results[:top_k]],
        }

    async def list_rag_collections(self) -> dict[str, Any]:
        return {"collections": [dict(c) for c in self.rag_collections]}

    async def list_mcp_servers(self) -> dict[str, Any]:
        return {"servers": [copy.deepcopy(s) for s in self.mcp_server_rows]}

    async def call_mcp_tool(
        self, server_id: str, tool_name: str, arguments: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        args = dict(arguments or {})
        self.mcp_tool_calls.append((server_id, tool_name, args))
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "output": f"Executed {tool_name} on {server_id}",
            "is_error": False,
        }

    async def create_edit_session(self, goal: str, files: list[str]) -> dict[str, Any]:
        file_list = list(files)
        self.edit_sessions_created.append((goal, file_list))
        return {
            "id": f"es-{len(self.edit_sessions_created)}",
            "goal": goal,
            "files": file_list,
            "status": "active",
        }

    async def list_agent_runs(
        self, agent_id: str | None = None, status: str | None = None
    ) -> dict[str, Any]:
        self.agent_runs_queries.append((agent_id, status))
        rows = [
            dict(r)
            for r in self.agent_run_rows
            if (not agent_id or r.get("agent_id") == agent_id)
            and (not status or r.get("status") == status)
        ]
        return {"runs": rows}

    async def list_connector_catalog(
        self, category: str | None = None, q: str | None = None
    ) -> dict[str, Any]:
        self.connector_catalog_queries.append((category, q))
        rows = [
            dict(r)
            for r in self.connector_catalog_rows
            if (not category or str(r.get("category") or "").lower() == category.lower())
            and (
                not q
                or q.lower() in str(r.get("provider_id") or "").lower()
                or q.lower() in str(r.get("name") or "").lower()
            )
        ]
        return {"connectors": rows, "data": rows, "total": len(rows)}

    async def proxy_oauth_request(
        self,
        credential_id: str,
        endpoint: str,
        method: str = "GET",
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.oauth_proxy_calls.append((credential_id, endpoint, method.upper(), body))
        return {
            "credential_id": credential_id,
            "provider_id": "github",
            "method": method.upper(),
            "target_url": endpoint,
            "status_code": 200,
            "auto_refreshed": False,
            "data": {"ok": True},
        }

    async def get_run_decision_trail(self, run_id: str) -> dict[str, Any]:
        self.decision_trail_queries.append(run_id)
        return {
            "run_id": run_id,
            "nodes": [dict(n) for n in self.decision_trail_nodes],
            "edges": [],
        }

    async def replay_run(self, run_id: str, mode: str = "simulated") -> dict[str, Any]:
        self.replay_calls.append((run_id, mode))
        return {
            "replay_id": f"rep-{run_id}",
            "run_id": run_id,
            "mode": mode,
            "success": True,
            "differences": [dict(d) for d in self.replay_differences],
        }

    async def list_run_checkpoints(self, run_id: str) -> dict[str, Any]:
        self.checkpoint_queries.append(run_id)
        rows = [dict(c) for c in self.checkpoint_rows]
        return {"run_id": run_id, "checkpoints": rows, "data": rows}

    async def resume_run(self, run_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
        self.resume_run_calls.append((run_id, checkpoint_id))
        return {
            "run_id": f"{run_id}-resumed",
            "source_run_id": run_id,
            "resumed_from_checkpoint_id": checkpoint_id or "ckpt-2",
            "resumed_from_step": "step-2",
            "state": "running",
        }

    async def get_reliability_metrics(self, agent_id: str | None = None) -> dict[str, Any]:
        self.reliability_queries.append(agent_id)
        return {**self.reliability_payload, "agent_id": agent_id}

    async def evaluate_guardrails(self, content: str, rail_type: str = "input") -> dict[str, Any]:
        self.guardrail_evaluations.append((content, rail_type))
        blocked = "unsafe" in content.lower() or "drop table" in content.lower()
        return {
            "rail_type": rail_type,
            "action": "block" if blocked else "allow",
            "allowed": not blocked,
            "modified_content": content,
            "refusal_message": "Blocked by input safety policy." if blocked else None,
            "triggered_rails": ["input_safety"] if blocked else [],
            "violations": (
                [
                    {
                        "rail_id": "input_safety",
                        "rail_type": rail_type,
                        "category": "safety",
                        "severity": "high",
                        "message": "Unsafe pattern detected",
                    }
                ]
                if blocked
                else []
            ),
        }

    async def plan_tools(self, goal: str) -> dict[str, Any]:
        self.tool_plans.append(goal)
        return {
            "plan_id": f"plan-{len(self.tool_plans)}",
            "task": goal,
            "goal": goal,
            "steps": [
                {
                    "step_id": "step-1",
                    "tool_name": "search_web",
                    "description": f"Research {goal}",
                    "arguments": {"query": goal},
                    "depends_on": [],
                }
            ],
            "confidence": 0.93,
        }

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


# --- a scripted /v1 for agent runs --------------------------------------------------
#
# The agent talks HTTP through the real adapter (`HttpChatAPI`) and the
# SDK, so the fake sits under them, as an httpx transport: /v1/chat/completions
# answers from a script (streamed as SSE the way the API streams it), and the
# /api routes a run touches (me, models, conversations, tree, compact) and
# /mcp answer from memory. A stored run is kept the way the API keeps one:
# the user's text, then one assistant message whose parts grow by tool_call
# and tool_result, so --resume reads back what a real server would hand it.

V1_BASE = "https://matterwave.test"

AGENT_MODELS: dict[str, Any] = {
    "data": [
        {
            "id": "coder",
            "label": "Coder",
            "included": True,
            "supports_tools": True,
            "context_window": 65536,
        },
        {
            "id": "grt-nano",
            "label": "GRT Nano",
            "included": True,
            "supports_tools": False,
            "context_window": 8192,
        },
    ],
    "default_model": "coder",
}


def say(text: str, *, usage: tuple[int, int] = (100, 20)) -> dict[str, Any]:
    """A reply in words, no tool call."""
    return {"text": text, "calls": [], "usage": usage}


def call(
    name: str, _text: str = "", *, usage: tuple[int, int] = (100, 20), **args: Any
) -> dict[str, Any]:
    """A reply that asks for one tool call."""
    return {"text": _text, "calls": [(name, args)], "usage": usage}


def calls(
    *pairs: tuple[str, dict[str, Any]], text: str = "", usage: tuple[int, int] = (100, 20)
) -> dict[str, Any]:
    return {"text": text, "calls": list(pairs), "usage": usage}


def refuse(status: int, message: str, code: str | None = None, **headers: str) -> dict[str, Any]:
    """The request is refused with OpenAI's error envelope."""
    return {"status": status, "message": message, "code": code, "headers": headers}


Reply = dict[str, Any] | Callable[[dict[str, Any]], dict[str, Any]]


class FakeV1:
    def __init__(
        self,
        script: list[Reply] | None = None,
        *,
        repeat: Reply | None = None,
        me: dict[str, Any] | None = None,
        models: dict[str, Any] | None = None,
        mcp_tools: list[dict[str, Any]] | None = None,
        instructions: str | None = None,
    ) -> None:
        self.script = list(script or [])
        # `repeat`: what to answer once the script runs out (a looping model).
        self.repeat = repeat
        self.me = (
            me
            if me is not None
            else {
                "subject": "u1",
                "kind": "user",
                "email": "dev@matterwave.test",
                "roles": [],
                "local_agent_tools": True,
            }
        )
        self.models = models if models is not None else AGENT_MODELS
        self.mcp_tools = mcp_tools if mcp_tools is not None else []
        self.mcp_called: list[tuple[str, dict[str, Any]]] = []
        self.instructions = instructions
        self.requests: list[dict[str, Any]] = []
        self.conversations: dict[str, dict[str, Any]] = {}
        self.stored: dict[str, list[dict[str, Any]]] = {}
        self.compactions = 0
        # Rounds the API would have stored on a branch (see _store_request).
        self.branched = 0
        self.calls_made = 0
        self._ids = 0

    # --- the client ---------------------------------------------------------------

    def api(self) -> HttpChatAPI:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return HttpChatAPI(base_url=V1_BASE, token="mw_test", http_client=client)

    def _next_id(self, prefix: str) -> str:
        self._ids += 1
        return f"{prefix}{self._ids}"

    # --- routes -----------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = _json.loads(request.content) if request.content else {}
        if path == "/v1/chat/completions":
            return self._complete(body)
        if path == "/api/me":
            return httpx.Response(200, json=self.me)
        if path == "/api/models":
            return httpx.Response(200, json=self.models)
        if path == "/api/conversations" and request.method == "POST":
            cid = f"conv-{len(self.conversations) + 1}"
            row = {
                "id": cid,
                "title": body.get("title") or "New chat",
                "created_at": "1",
                "updated_at": "1",
                "default_model": body.get("model"),
            }
            self.conversations[cid] = row
            self.stored[cid] = []
            return httpx.Response(200, json=row)
        if path == "/mcp":
            return self._mcp(body)
        parts = path.split("/")
        if len(parts) >= 4 and parts[1] == "api" and parts[2] == "conversations":
            cid = parts[3]
            if cid not in self.conversations:
                return httpx.Response(404, json={"detail": "Not Found"})
            tail = parts[4] if len(parts) > 4 else ""
            if tail == "tree":
                return httpx.Response(200, json=self._tree(cid))
            if tail == "compact":
                return self._compact(cid)
            if tail == "":
                row = dict(self.conversations[cid])
                effective = self.instructions or ""
                row["instructions"] = {
                    "org": None,
                    "user": None,
                    "conversation": None,
                    "user_overridden": False,
                    "effective": effective,
                }
                return httpx.Response(200, json=row)
        return httpx.Response(404, json={"detail": "Not Found"})

    def _tree(self, cid: str) -> dict[str, Any]:
        row = dict(self.conversations[cid])
        messages = self.stored[cid]
        row["messages"] = messages
        row["current_leaf_message_id"] = messages[-1]["id"] if messages else None
        return row

    def _compact(self, cid: str) -> httpx.Response:
        self.compactions += 1
        messages = self.stored[cid]
        summary = {
            "id": self._next_id("s"),
            "conversation_id": cid,
            "role": "system",
            "content": "",
            "created_at": "2",
            "parent_id": messages[-1]["id"] if messages else None,
            "parts": [
                {
                    "type": "summary",
                    "text": "The run so far, summarised.",
                    "covers_from": messages[0]["id"] if messages else None,
                    "covers_to": messages[-1]["id"] if messages else None,
                }
            ],
        }
        messages.append(summary)
        return httpx.Response(200, json=summary)

    # --- storing a run the way the API does -----------------------------------------------

    def _store_request(self, cid: str, messages: list[dict[str, Any]]) -> None:
        stored = self.stored[cid]
        last = messages[-1]
        if last.get("role") == "user":
            stored.append(
                {
                    "id": self._next_id("m"),
                    "conversation_id": cid,
                    "role": "user",
                    "content": last.get("content") or "",
                    "created_at": "1",
                    "parent_id": stored[-1]["id"] if stored else None,
                    "parts": [{"type": "text", "text": last.get("content") or ""}],
                }
            )
        elif last.get("role") == "tool":
            start = len(messages)
            while start > 0 and messages[start - 1].get("role") == "tool":
                start -= 1
            ids = (
                {c.get("id") for c in (messages[start - 1].get("tool_calls") or [])}
                if start
                else set()
            )
            holder = next(
                (
                    m
                    for m in reversed(stored)
                    if m["role"] == "assistant"
                    and any(p.get("type") == "tool_call" and p.get("id") in ids for p in m["parts"])
                ),
                None,
            )
            if holder is None:
                return
            if holder is not stored[-1]:
                # As the API does: a round closed anywhere but at the end of
                # the thread is stored on a branch beside its call, the way a
                # retried request is, and what came after it leaves the path.
                self.branched += 1
                holder = {
                    **holder,
                    "id": self._next_id("m"),
                    "parts": [dict(p) for p in holder["parts"]],
                }
                stored.append(holder)
            parts = holder["parts"]
            have = {p.get("id") for p in parts if p.get("type") == "tool_result"}
            called = {p.get("id") for p in parts if p.get("type") == "tool_call"}
            for m in messages[start:]:
                ident = m.get("tool_call_id")
                if ident in called and ident not in have:
                    parts.append({"type": "tool_result", "id": ident, "output": m.get("content")})

    def _store_reply(self, cid: str, text: str, tool_calls: list[dict[str, Any]]) -> None:
        stored = self.stored[cid]
        parts: list[dict[str, Any]] = []
        if text:
            parts.append({"type": "text", "text": text})
        for c in tool_calls:
            parts.append(
                {
                    "type": "tool_call",
                    "id": c["id"],
                    "name": c["function"]["name"],
                    "arguments": c["function"]["arguments"],
                }
            )
        if stored and stored[-1]["role"] == "assistant":
            stored[-1]["parts"].extend(parts)
            stored[-1]["content"] = (stored[-1].get("content") or "") + text
        else:
            stored.append(
                {
                    "id": self._next_id("m"),
                    "conversation_id": cid,
                    "role": "assistant",
                    "content": text,
                    "created_at": "1",
                    "model": "coder",
                    "parent_id": stored[-1]["id"] if stored else None,
                    "parts": parts,
                }
            )

    # --- /v1/chat/completions ------------------------------------------------------------

    def _complete(self, body: dict[str, Any]) -> httpx.Response:
        self.requests.append(body)
        if self.script:
            reply = self.script.pop(0)
        elif self.repeat is not None:
            reply = self.repeat
        else:
            reply = say("(the script ran out)")
        if callable(reply):
            reply = reply(body)
        if "status" in reply:
            envelope = {
                "error": {
                    "message": reply["message"],
                    "type": "invalid_request_error",
                    "param": None,
                    "code": reply.get("code"),
                }
            }
            headers = {
                "x-request-id": f"req-refused-{len(self.requests)}",
                **reply.get("headers", {}),
            }
            return httpx.Response(reply["status"], json=envelope, headers=headers)
        cid = (body.get("metadata") or {}).get("conversation_id")
        if cid in self.stored:
            self._store_request(cid, body["messages"])
        tool_calls = []
        for name, args in reply["calls"]:
            self.calls_made += 1
            tool_calls.append(
                {
                    "id": f"call_{self.calls_made}",
                    "type": "function",
                    "function": {"name": name, "arguments": _json.dumps(args)},
                }
            )
        if cid in self.stored:
            self._store_reply(cid, reply["text"], tool_calls)
        return httpx.Response(
            200,
            headers={
                "content-type": "text/event-stream",
                "x-request-id": f"req-{len(self.requests)}",
            },
            text=self._sse(reply["text"], tool_calls, reply["usage"]),
        )

    @staticmethod
    def _sse(text: str, tool_calls: list[dict[str, Any]], usage: tuple[int, int]) -> str:
        def frame(delta: dict[str, Any], finish: str | None = None) -> str:
            chunk = {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "coder",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            return f"data: {_json.dumps(chunk)}\n\n"

        out = [frame({"role": "assistant"})]
        if text:
            half = max(1, len(text) // 2)
            out.append(frame({"content": text[:half]}))
            if text[half:]:
                out.append(frame({"content": text[half:]}))
        for index, c in enumerate(tool_calls):
            args = c["function"]["arguments"]
            out.append(
                frame(
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": c["id"],
                                "type": "function",
                                "function": {"name": c["function"]["name"], "arguments": ""},
                            }
                        ]
                    }
                )
            )
            cut = len(args) // 2
            out.append(
                frame({"tool_calls": [{"index": index, "function": {"arguments": args[:cut]}}]})
            )
            out.append(
                frame({"tool_calls": [{"index": index, "function": {"arguments": args[cut:]}}]})
            )
        out.append(frame({}, "tool_calls" if tool_calls else "stop"))
        prompt, completion = usage
        tail = {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "coder",
            "choices": [],
            "usage": {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": prompt + completion,
            },
        }
        out.append(f"data: {_json.dumps(tail)}\n\n")
        out.append("data: [DONE]\n\n")
        return "".join(out)

    # --- /mcp ------------------------------------------------------------------------------

    def _mcp(self, body: dict[str, Any]) -> httpx.Response:
        method = body.get("method")
        if "id" not in body:
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "matterwave", "version": "test"},
            }
        elif method == "tools/list":
            result = {"tools": self.mcp_tools}
        elif method == "tools/call":
            params = body.get("params") or {}
            self.mcp_called.append((params.get("name"), params.get("arguments") or {}))
            result = {
                "content": [{"type": "text", "text": f"ran {params.get('name')}"}],
                "isError": False,
            }
        else:
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body["id"],
                    "error": {"code": -32601, "message": "no such method"},
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    # --- what the tests read ------------------------------------------------------------------

    def tool_names(self, index: int = -1) -> list[str]:
        return [t["function"]["name"] for t in self.requests[index].get("tools") or []]

    def tool_results(self, index: int = -1) -> list[str]:
        return [m["content"] for m in self.requests[index]["messages"] if m.get("role") == "tool"]
