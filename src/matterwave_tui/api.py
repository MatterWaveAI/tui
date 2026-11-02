"""The `/api` routes the TUI uses, behind a Protocol the app is written against.

The wire is the SDK's: `AsyncMatterWave.conversations` for the routes
(create, temporary or not, delete, pin and archive included), `app_models`
for the picker, its `AsyncConversationStream` for a turn (named events, typed
frames, one resume by `Last-Event-ID`), its errors with the request id on
them, and the typed `notifications`, `announcements`, `shares`, `library`,
`budget`, `keys`, `attachments` and `messages` resources.
What stays here is the TUI's own policy: a 401 is answered once with a fresh
credential before it stands, a write is never retried, and the routes the SDK
has no method for (an upload's scan status, and the account, import, speech
and export routes) go through the same client. The typed rows the app reads
as plain dicts (models, notifications, announcements, shares, library,
budget, keys, feedback) are flattened here, at the edge.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlsplit

import httpx
from matterwave_sdk import AsyncConversationStream, AsyncMatterWave, ChatCompletionAccumulator
from matterwave_sdk import _base as sdk_base
from matterwave_sdk._client import USER_AGENT as SDK_USER_AGENT
from matterwave_sdk.errors import APIConnectionError, AuthenticationError, McpToolError
from matterwave_sdk.frames import TERMINAL_FRAMES, ConversationFrame
from matterwave_sdk.models import (
    Attachment,
    AttachmentLimits,
    Conversation,
    ConversationContext,
    ConversationMessage,
    CreatedShare,
    FiledReport,
    Me,
    MessageFeedback,
    RemovedFeedback,
    UsageBalance,
)

from . import __version__
from .agent.wire import Completed, CompletionEvent, TextDelta

# A local API; anything else comes from --base-url or MATTERWAVE_BASE_URL.
DEFAULT_BASE_URL = "http://localhost:8000"
USER_AGENT = f"matterwave-tui/{__version__} {SDK_USER_AGENT}"
# The notifications route's page bounds (api/matterwave/notifications.py:
# LIST_LIMIT_DEFAULT 30, LIST_LIMIT_MAX 100). There is no cursor or offset:
# a longer history is a longer limit, capped at the route's own maximum.
NOTIFICATIONS_PAGE = 30
NOTIFICATIONS_LIMIT_MAX = 100


# --- what the adapter adds to the server's frames ----------------------------


@dataclass(frozen=True)
class Opened:
    """The stream's response arrived; its request id is the one to quote."""

    request_id: str | None


@dataclass(frozen=True)
class Cut:
    """The body ended without `done`, `error` or `moderation_block`, and the
    SDK's resume did not bring the rest."""

    request_id: str | None


StreamItem = ConversationFrame | Opened | Cut


@dataclass(frozen=True)
class SearchPage:
    """One page of `GET /api/search`: the conversations that matched, best
    first, and the cursor of the page after it (None on the last)."""

    conversations: list[Conversation]
    next_cursor: str | None


class ChatAPI(Protocol):
    """Everything the app asks of the platform."""

    async def me(self) -> Me: ...

    async def models(self) -> dict[str, Any]: ...

    async def presets(self) -> list[dict[str, Any]]: ...

    async def preset(self, preset_id: str) -> dict[str, Any]: ...

    async def list_conversations(self, filter: str = "default") -> list[Conversation]: ...

    async def all_conversations(self, filter: str = "default") -> list[Conversation]: ...

    async def search(self, query: str, cursor: str | None = None) -> SearchPage: ...

    async def create_conversation(
        self,
        title: str | None = None,
        model: str | None = None,
        temporary: bool = False,
        preset_id: str | None = None,
    ) -> Conversation: ...

    async def tree(self, conversation_id: str) -> Conversation: ...

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation: ...

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation: ...

    async def set_preset(self, conversation_id: str, preset_id: str | None) -> Conversation: ...

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation: ...

    async def delete_conversation(self, conversation_id: str) -> None: ...

    async def set_pinned(self, conversation_id: str, pinned: bool) -> Conversation: ...

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation: ...

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation: ...

    async def share_conversation(
        self,
        conversation_id: str,
        *,
        expires_in_s: int | None = None,
        include_attachments: bool = False,
    ) -> str: ...

    async def conversation_instructions(self, conversation_id: str) -> dict[str, Any]: ...

    async def set_conversation_instructions(
        self, conversation_id: str, text: str | None
    ) -> dict[str, Any]: ...

    async def set_feedback(
        self,
        message_id: str,
        rating: int,
        reasons: Sequence[str] = (),
        comment: str | None = None,
    ) -> MessageFeedback: ...

    async def delete_feedback(self, message_id: str) -> RemovedFeedback: ...

    async def conversation_feedback(self, conversation_id: str) -> list[dict[str, Any]]: ...

    async def notifications(
        self, *, unread: bool = False, limit: int = NOTIFICATIONS_PAGE
    ) -> dict[str, Any]: ...

    async def read_notification(self, notification_id: str) -> dict[str, Any]: ...

    async def read_all_notifications(self) -> dict[str, Any]: ...

    async def library(self) -> dict[str, Any]: ...

    async def create_folder(self, name: str) -> dict[str, Any]: ...

    async def rename_folder(self, folder_id: str, name: str) -> dict[str, Any]: ...

    async def delete_folder(self, folder_id: str) -> None: ...

    async def delete_tag(self, tag_id: str) -> None: ...

    async def move_to_folder(self, conversation_id: str, folder_id: str | None) -> None: ...

    async def create_tag(self, name: str) -> dict[str, Any]: ...

    async def set_tags(self, conversation_id: str, tag_ids: Sequence[str]) -> dict[str, Any]: ...

    async def announcements(self) -> dict[str, Any]: ...

    async def report_conversation(
        self,
        conversation_id: str,
        kind: str,
        note: str | None = None,
        message_id: str | None = None,
    ) -> FiledReport: ...

    async def conversation_shares(self, conversation_id: str) -> list[dict[str, Any]]: ...

    async def my_shares(self) -> list[dict[str, Any]]: ...

    async def revoke_share(self, share_id: str) -> dict[str, Any]: ...

    def own_key_prefix(self) -> str | None: ...

    async def api_keys(self) -> list[dict[str, Any]]: ...

    async def rotate_key(self, key_id: str) -> dict[str, Any]: ...

    async def revoke_key(self, key_id: str) -> None: ...

    async def sessions(self) -> list[dict[str, Any]]: ...

    async def revoke_session(self, session_id: str) -> None: ...

    async def preferences(self) -> dict[str, Any]: ...

    async def set_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]: ...

    async def user_instructions(self) -> dict[str, str]: ...

    async def set_user_instructions(self, about: str, style: str) -> dict[str, str]: ...

    async def entitlements(self) -> dict[str, Any]: ...

    async def billing_status(self) -> dict[str, Any]: ...

    async def plans(self) -> list[dict[str, Any]]: ...

    async def enforcement(self) -> dict[str, Any]: ...

    async def accounts(self) -> dict[str, Any]: ...

    async def switch_account(self, slot: int) -> dict[str, Any]: ...

    async def download_attachment(self, attachment_id: str) -> bytes: ...

    async def start_import(self, data: bytes, filename: str) -> dict[str, Any]: ...

    async def import_status(self, import_id: str) -> dict[str, Any]: ...

    async def start_export(self) -> dict[str, Any]: ...

    async def export_status(self, export_id: str) -> dict[str, Any]: ...

    async def export_download(self, path: str) -> bytes: ...

    async def transcribe(
        self, data: bytes, filename: str, content_type: str, language: str | None = None
    ) -> str: ...

    async def budget(self) -> dict[str, Any]: ...

    async def usage(self) -> UsageBalance: ...

    async def upload_attachment(
        self, data: bytes, filename: str, conversation_id: str | None = None
    ) -> Attachment: ...

    async def attachment_limits(self) -> AttachmentLimits: ...

    async def attachment_scan_status(self, attachment_id: str) -> str: ...

    async def delete_attachment(self, attachment_id: str) -> None: ...

    async def context(self, conversation_id: str) -> ConversationContext: ...

    async def compact(self, conversation_id: str) -> ConversationMessage: ...

    def send(
        self,
        conversation_id: str,
        content: str,
        model: str | None = None,
        attachment_ids: list[str] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    def edit(
        self,
        conversation_id: str,
        message_id: str,
        content: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    def regenerate(
        self,
        conversation_id: str,
        message_id: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    async def aclose(self) -> None: ...


# Called once on a 401 for a fresh bearer: one re-read or re-login. None means
# there is no other credential to try and the 401 stands.
TokenRefresher = Callable[[], Awaitable[str | None]]


class _Credential:
    """The bearer both clients send, and the one renewal a 401 gets. The
    lock makes concurrent refusals share a single sign-in rather than each
    opening a device-code modal of its own."""

    def __init__(self, token: str, refresh: TokenRefresher | None) -> None:
        self.token = token
        self._refresh = refresh
        self._lock = asyncio.Lock()

    async def renew(self, refused: str) -> bool:
        """Whether there is a bearer other than `refused` to try now."""
        async with self._lock:
            if self.token != refused:
                return True
            if self._refresh is None:
                return False
            token = await self._refresh()
            if not token or token == refused:
                return False
            self.token = token
            return True


class _Client(AsyncMatterWave):
    """The SDK client with the TUI's answer to a 401: a fresh credential,
    then the request again. `_send` is the one path every request takes,
    the stream's own resume included, so a resume that lands on a revoked
    key signs in again instead of ending the turn as a session failure."""

    def __init__(self, credential: _Credential, **kwargs: Any) -> None:
        super().__init__(api_key=credential.token, **kwargs)
        self._credential = credential

    async def _send(
        self,
        request: sdk_base.Request,
        *,
        # The supertype's parameters: an override mirrors them, however
        # ruff feels about a `timeout` argument (ASYNC109).
        timeout: float | httpx.Timeout | None = None,  # noqa: ASYNC109
        max_retries: int | None = None,
    ) -> httpx.Response:
        self._config.api_key = self._credential.token
        try:
            return await super()._send(request, timeout=timeout, max_retries=max_retries)
        except AuthenticationError:
            if not await self._credential.renew(self._config.api_key or ""):
                raise
            self._config.api_key = self._credential.token
            return await super()._send(request, timeout=timeout, max_retries=max_retries)


def key_prefix(token: str) -> str | None:
    """The part of an `mw_` key the server lists it by (`mw_` and eight
    characters, then `_`), as the platform's own `prefix_of` cuts it."""
    if token.startswith("mw_") and len(token) > 12 and token[11] == "_":
        return token[:11]
    return None


def _debug_hook(path: Path) -> Callable[[httpx.Response], Awaitable[None]]:
    """`--debug`: a line per response in `path` -- the time, the method,
    the path without its query, the status, the request id. Never a body,
    a header or a query string: those carry what people wrote."""
    log = logging.getLogger("matterwave_tui.http")
    log.setLevel(logging.INFO)
    log.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)

    async def on_response(response: httpx.Response) -> None:
        request = response.request
        log.info(
            "%s %s %s %s",
            request.method,
            request.url.path,
            response.status_code,
            response.headers.get("x-request-id", "-"),
        )

    return on_response


def _path(*segments: str) -> str:
    return "/api/" + "/".join(quote(s, safe="") for s in segments)


def _plain(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """`asdict`'s factory that leaves the SDK models' `request_id` out: the
    app's rows are the server's fields, and the transport's bookkeeping is
    not one of them."""
    return {key: value for key, value in pairs if key != "request_id"}


def _row_dict(model: Any) -> dict[str, Any]:
    return asdict(model, dict_factory=_plain)


class HttpChatAPI:
    """`ChatAPI` over the SDK."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        refresh: TokenRefresher | None = None,
        http_client: httpx.AsyncClient | None = None,
        debug_log: Path | None = None,
    ) -> None:
        self._owns_http = http_client is None
        if http_client is not None:
            self._http = http_client
        elif debug_log is not None:
            self._http = httpx.AsyncClient(event_hooks={"response": [_debug_hook(debug_log)]})
        else:
            self._http = httpx.AsyncClient()
        credential = _Credential(token, refresh)
        self._credential = credential
        common: dict[str, Any] = {
            "base_url": base_url,
            "http_client": self._http,
            "default_headers": {"User-Agent": USER_AGENT},
        }
        # Reads retry the SDK's way. Writes do not: a retried POST that the
        # server did receive would create a second conversation or bill a
        # second turn, so those go through a client with retries off.
        self._reads = _Client(credential, **common)
        self._writes = _Client(credential, max_retries=0, **common)

    @property
    def base_url(self) -> str:
        return self._reads.base_url

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def _app_json(
        self,
        client: _Client,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """A route the SDK has no method for, sent the way its own are.
        `files` turns it multipart; the extra fields a route wants beside
        the file ride as empty-named file parts (the way the SPA's FormData
        would send them), since httpx drops a json body when files are set."""
        response = await client._send(
            sdk_base.Request(method, path, body, True, False, files=files)
        )
        return sdk_base.parse_json_object(response)

    # --- routes --------------------------------------------------------------

    async def me(self) -> Me:
        return await self._reads.me()

    async def models(self) -> dict[str, Any]:
        """`GET /api/models` by way of the SDK's `app_models`: the picker's
        rows, with each model's plan gating and price (the SDK's
        `models.list()` is `/v1/models`, which has neither), flattened to
        the dicts the app reads."""
        listed = await self._reads.app_models()
        return {
            "data": [_row_dict(model) for model in listed.data],
            "default_model": listed.default_model,
        }

    async def presets(self) -> list[dict[str, Any]]:
        """`GET /api/org/presets` by way of the SDK: the org's live presets,
        flattened to the dicts the picker reads."""
        listed = await self._reads.presets.list()
        return [_row_dict(preset) for preset in listed.data]

    async def preset(self, preset_id: str) -> dict[str, Any]:
        """`GET /api/org/presets/{id}` by way of the SDK: one preset, a
        deleted one included (`deleted_at` says so)."""
        return _row_dict(await self._reads.presets.get(preset_id))

    async def list_conversations(self, filter: str = "default") -> list[Conversation]:
        return (await self._reads.conversations.list(filter=filter)).data

    async def all_conversations(self, filter: str = "default") -> list[Conversation]:
        """Every page of the list, for a view that filters on the client
        (a folder, a tag) and must not miss what the first page left out."""
        found: list[Conversation] = []
        async for page in self._reads.conversations.pages(filter=filter):
            found.extend(page.data)
        return found

    async def search(self, query: str, cursor: str | None = None) -> SearchPage:
        """`GET /api/search` by way of the SDK's `library.search`: titles and
        messages, ranked by relevance and recency, a page at a time;
        `cursor` is the previous page's `next_cursor`."""
        page = await self._reads.library.search(query, cursor=cursor)
        return SearchPage([hit.conversation for hit in page.data], page.next_cursor)

    async def create_conversation(
        self,
        title: str | None = None,
        model: str | None = None,
        temporary: bool = False,
        preset_id: str | None = None,
    ) -> Conversation:
        """`POST /api/conversations` by way of the SDK. `temporary` is fixed
        at creation, and the key is sent only when it is on. A write: no
        retries."""
        return await self._writes.conversations.create(
            title=title,
            model=model,
            temporary=True if temporary else None,
            preset_id=preset_id,
        )

    async def tree(self, conversation_id: str) -> Conversation:
        return await self._reads.conversations.tree(conversation_id)

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation:
        return await self._reads.conversations.set_active_leaf(conversation_id, message_id)

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation:
        """The picker's choice, kept on the conversation so the web app opens
        it on the same model. None clears it. A PATCH that lands twice sets
        the same value twice, so it may take the reads' retries."""
        return await self._reads.conversations.update(conversation_id, default_model=model)

    async def set_preset(self, conversation_id: str, preset_id: str | None) -> Conversation:
        """`PATCH /api/conversations/{id}` with `preset_id`: put the
        conversation on a preset, or clear it (`None`). Idempotent, so it
        may take the reads' retries."""
        return await self._reads.conversations.update(conversation_id, preset_id=preset_id)

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        """The SDK's update with a title: a PATCH that lands twice writes
        the same title twice, so it may take the reads' retries."""
        return await self._reads.conversations.update(conversation_id, title=title)

    async def delete_conversation(self, conversation_id: str) -> None:
        """`DELETE /api/conversations/{id}` by way of the SDK: gone from
        every list at once, the row itself after the server's grace period.
        The answer (`{"status": "deleted"}`) carries nothing the app shows.
        A delete that lands twice deletes the same nothing, but a delete is
        still a write: no retries."""
        await self._writes.conversations.delete(conversation_id)

    async def set_pinned(self, conversation_id: str, pinned: bool) -> Conversation:
        """`PUT` / `DELETE /api/conversations/{id}/pin` by way of the SDK,
        answered with the updated conversation. Set and clear are separate
        methods, so a retried request cannot land in the opposite state:
        the reads' retries do no harm."""
        conversations = self._reads.conversations
        if pinned:
            return await conversations.pin(conversation_id)
        return await conversations.unpin(conversation_id)

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation:
        """`PUT` / `DELETE /api/conversations/{id}/archive` by way of the
        SDK, answered with the updated conversation; idempotent by method,
        as pinning is."""
        conversations = self._reads.conversations
        if archived:
            return await conversations.archive(conversation_id)
        return await conversations.unarchive(conversation_id)

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation:
        """The SDK's fork: a new conversation holding the path up to the
        message, which the caller then opens like any other."""
        return await self._writes.conversations.fork(conversation_id, message_id, title=title)

    async def share_conversation(
        self,
        conversation_id: str,
        *,
        expires_in_s: int | None = None,
        include_attachments: bool = False,
    ) -> str:
        """`POST /api/conversations/{id}/share`: a read-only link to a
        snapshot of the active path, as a full URL against this deployment.
        `expires_in_s` None lives until revoked; `include_attachments` lets
        a reader open the files, where otherwise they see only the names.
        The SDK's `create` has no `include_attachments`, so the body is
        built here. A write: no retries."""
        body = await self._app_json(
            self._writes,
            "POST",
            _path("conversations", conversation_id, "share"),
            {"expires_in_s": expires_in_s, "include_attachments": include_attachments},
        )
        created = CreatedShare.from_dict(body)
        return str(self._reads.base_url).rstrip("/") + created.path

    async def conversation_instructions(self, conversation_id: str) -> dict[str, Any]:
        """The instruction levels in force for the thread, from
        `GET /api/conversations/{id}`: `org`, `user`, `conversation`,
        `user_overridden` and `effective` (the text the model is sent)."""
        conversation = await self._reads.conversations.get(conversation_id)
        return conversation.instructions or {}

    async def set_conversation_instructions(
        self, conversation_id: str, text: str | None
    ) -> dict[str, Any]:
        """`PUT /api/conversations/{id}/instructions`: the thread's own
        prompt, 2000 characters at most. None or "" clears it, so the
        user's instructions apply again; the answer carries the levels
        now in force. A write: no retries."""
        updated = await self._writes.conversations.update_instructions(conversation_id, text)
        return updated.instructions or {}

    async def set_feedback(
        self,
        message_id: str,
        rating: int,
        reasons: Sequence[str] = (),
        comment: str | None = None,
    ) -> MessageFeedback:
        """`PUT /api/messages/{id}/feedback`: rate a reply 1 or -1. A
        second rating for the same message replaces the first."""
        return await self._writes.messages.set_feedback(
            message_id, rating, reasons=reasons, comment=comment
        )

    async def delete_feedback(self, message_id: str) -> RemovedFeedback:
        """`DELETE /api/messages/{id}/feedback`: withdraw a rating."""
        return await self._writes.messages.delete_feedback(message_id)

    async def conversation_feedback(self, conversation_id: str) -> list[dict[str, Any]]:
        """`GET /api/conversations/{id}/feedback` by way of the SDK: the
        caller's own ratings in the thread, to mark the transcript with when
        it opens, flattened to the dicts the app reads."""
        page = await self._reads.messages.list_feedback(conversation_id)
        return [_row_dict(row) for row in page.data]

    async def notifications(
        self, *, unread: bool = False, limit: int = NOTIFICATIONS_PAGE
    ) -> dict[str, Any]:
        """`GET /api/me/notifications` by way of the SDK's typed resource:
        the caller's rows, newest first, with the unread count the badge
        shows. `unread=1` lists only the unread ones (the count is the same
        either way) and `limit` caps the page; the route has no cursor, so
        a longer history is a longer limit. The typed rows are flattened to
        the dicts the app reads."""
        page = await self._reads.notifications.list(unread=unread, limit=limit)
        return {"data": [_row_dict(row) for row in page.data], "unread_count": page.unread_count}

    async def read_notification(self, notification_id: str) -> dict[str, Any]:
        """`POST /api/me/notifications/{id}/read` by way of the SDK: the row
        with its (first) `read_at`, and the unread count now. A write: no
        retries."""
        row = await self._writes.notifications.read(notification_id)
        return _row_dict(row)

    async def read_all_notifications(self) -> dict[str, Any]:
        """`POST /api/me/notifications/read-all` by way of the SDK: how many
        rows it marked and an unread count of 0. A write: no retries."""
        marked = await self._writes.notifications.read_all()
        return {"read": marked.read, "unread_count": marked.unread_count}

    async def announcements(self) -> dict[str, Any]:
        """`GET /api/announcements` by way of the SDK's typed resource: the
        ones inside their window, most severe first; `dismissible` is the
        operator's, and a critical one is never dismissible. The typed rows
        are flattened to the dicts the app reads."""
        page = await self._reads.announcements.list()
        return {"data": [_row_dict(row) for row in page.data]}

    async def report_conversation(
        self,
        conversation_id: str,
        kind: str,
        note: str | None = None,
        message_id: str | None = None,
    ) -> FiledReport:
        """`POST /api/conversations/{id}/report` by way of the SDK: `kind`
        is `unsafe` (routed to review) or `unhelpful` (the product backlog),
        the note is the reporter's own words, and `message_id` targets one
        reply where the report is about a single answer rather than the
        whole conversation. The answer's id is the one a user quotes to
        support. A report filed twice would be two queue entries: a write,
        no retries."""
        return await self._writes.conversations.report(
            conversation_id, kind, message_id=message_id, note=note
        )

    async def conversation_shares(self, conversation_id: str) -> list[dict[str, Any]]:
        """`GET /api/conversations/{id}/shares` by way of the SDK's typed
        resource: the links made for the conversation, newest first,
        flattened to the dicts the app reads."""
        page = await self._reads.shares.list(conversation_id)
        return [_row_dict(row) for row in page.data]

    async def my_shares(self) -> list[dict[str, Any]]:
        """`GET /api/me/shares` by way of the SDK's typed resource: every
        link the caller made, the one place to see what is out there and
        take it back."""
        page = await self._reads.shares.list_mine()
        return [_row_dict(row) for row in page.data]

    async def revoke_share(self, share_id: str) -> dict[str, Any]:
        """`DELETE /api/shares/{id}` by way of the SDK: the link stops
        answering. The typed resource discards the answer's body (nothing
        in the app read it), so the adapter's dict is empty. A revoke that
        lands twice revokes the same nothing, but it is a write: no
        retries."""
        await self._writes.shares.revoke(share_id)
        return {}

    def own_key_prefix(self) -> str | None:
        """The listed prefix of the key this client sends, so the account
        screen can tell it from the others."""
        return key_prefix(self._credential.token)

    async def api_keys(self) -> list[dict[str, Any]]:
        """`GET /api/keys` by way of the SDK: the org's keys, never their
        secrets, revoked ones included. A key without `keys:manage` (a
        device key) is shown its own row and nothing else."""
        page = await self._reads.keys.list()
        return [_row_dict(key) for key in page.data]

    async def revoke_key(self, key_id: str) -> None:
        """`DELETE /api/keys/{id}` by way of the SDK: the key stops working
        at once; a key may always revoke itself. A write: no retries."""
        await self._writes.keys.revoke(key_id)

    async def rotate_key(self, key_id: str) -> dict[str, Any]:
        """`POST /api/keys/{id}/rotate` by way of the SDK: a new secret for
        the same key, in this answer and nowhere else; the old one works
        until `previous_valid_until`. A write: no retries."""
        return _row_dict(await self._writes.keys.rotate(key_id))

    async def sessions(self) -> list[dict[str, Any]]:
        """`GET /api/me/sessions`: the caller's live browser sessions."""
        body = await self._app_json(self._reads, "GET", _path("me", "sessions"))
        rows = body.get("sessions")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def revoke_session(self, session_id: str) -> None:
        """`DELETE /api/me/sessions/{id}`: that browser is signed out. A
        write: no retries."""
        await self._app_json(self._writes, "DELETE", _path("me", "sessions", session_id))

    async def preferences(self) -> dict[str, Any]:
        """`GET /api/me/preferences`: the email and notification switches
        and the locale, as every client of the account reads them."""
        return await self._app_json(self._reads, "GET", _path("me", "preferences"))

    async def set_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        """`PUT /api/me/preferences`: the whole shape, answered as saved. A
        PUT of the same shape twice saves it twice, so it may take the
        reads' retries."""
        return await self._app_json(self._reads, "PUT", _path("me", "preferences"), preferences)

    async def user_instructions(self) -> dict[str, str]:
        """`GET /api/me/instructions` by way of the SDK: what the assistant
        should know about the person, and how they want answers."""
        saved = await self._reads.instructions.user()
        return {"about": saved.about, "style": saved.style}

    async def set_user_instructions(self, about: str, style: str) -> dict[str, str]:
        """`PUT /api/me/instructions` by way of the SDK: the pair, replaced
        whole (a plan without custom instructions is the server's 402)."""
        saved = await self._writes.instructions.update_user(about=about, style=style)
        return {"about": saved.about, "style": saved.style}

    async def entitlements(self) -> dict[str, Any]:
        """`GET /api/entitlements` by way of the SDK: the org's plan, seats
        and what it may do."""
        return _row_dict(await self._reads.entitlements.get())

    async def billing_status(self) -> dict[str, Any]:
        """`GET /api/billing/status`: how the org pays and whether it is
        behind, readable by any member."""
        return await self._app_json(self._reads, "GET", _path("billing", "status"))

    async def plans(self) -> list[dict[str, Any]]:
        """`GET /api/plans`: the public catalog, cheapest first."""
        body = await self._app_json(self._reads, "GET", _path("plans"))
        rows = body.get("data")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    async def enforcement(self) -> dict[str, Any]:
        """`GET /api/me/enforcement`: any moderation action that applies to
        the caller now (`{"state": "none"}` for nearly everyone)."""
        return await self._app_json(self._reads, "GET", _path("me", "enforcement"))

    async def accounts(self) -> dict[str, Any]:
        """`GET /api/accounts`: who is signed in, the acting account first,
        each with its `slot` (0 acting, 1.. parked), an `email`, a `name`
        and an `org_name`; plus the browser's `max` and whether there is
        `room` for another. The route is cookie-shaped, so the answer shows
        what the server holds for this client's credential."""
        return await self._app_json(self._reads, "GET", _path("accounts"))

    async def switch_account(self, slot: int) -> dict[str, Any]:
        """`POST /api/accounts/switch`: make the account parked in `slot`
        the acting one. The answer names the subject and email now acting.
        A switch that lands twice swaps the same pair again: it is a write,
        no retries."""
        return await self._app_json(
            self._writes, "POST", _path("accounts", "switch"), {"slot": slot}
        )

    async def download_attachment(self, attachment_id: str) -> bytes:
        """An attachment's bytes by way of the SDK's typed resource: `get`
        mints the short-lived signed URL, `download` follows it. Both are
        reads, so they may take the SDK's retries."""
        attachment = await self._reads.attachments.get(attachment_id)
        return await self._reads.attachments.download(attachment)

    async def start_import(self, data: bytes, filename: str) -> dict[str, Any]:
        """`POST /api/me/imports` (multipart) by way of the raw client: the
        export is parsed and counted, the job queued, and the row comes
        back `pending` with what was found. An upload that lands twice
        would import everything twice, so it is a write: no retries."""
        return await self._app_json(
            self._writes,
            "POST",
            _path("me", "imports"),
            files={"file": (filename, data, "application/octet-stream")},
        )

    async def import_status(self, import_id: str) -> dict[str, Any]:
        """`GET /api/me/imports/{id}`: where one import stands — `pending`,
        `running`, `done` or `failed`, with the counts of what it did."""
        return await self._app_json(self._reads, "GET", _path("me", "imports", import_id))

    # --- the account's data export ---------------------------------------------

    async def start_export(self) -> dict[str, Any]:
        """`POST /api/me/export` (202): the same document, built by the
        server's worker. The answer is the job's row — `pending`, with the
        `id` the status route polls — and the person is told in-app and by
        mail when it is ready. One pending export per person (409), and a
        start that lands twice would build two copies: a write, no
        retries."""
        return await self._app_json(self._writes, "POST", _path("me", "export"))

    async def export_status(self, export_id: str) -> dict[str, Any]:
        """`GET /api/me/export/{id}`: where one export stands — `pending`,
        `ready`, `failed` or `expired` — with a fresh signed
        `download_url` once it is ready. Another person's export is the
        same 404 as none."""
        return await self._app_json(self._reads, "GET", _path("me", "export", export_id))

    async def export_download(self, path: str) -> bytes:
        """The export's bytes, from the signed `download_url` the status
        row carries: the signature rides the path's query, and the
        caller's own credential goes beside it — the attachment
        download's rule, a forwarded link being useless to anyone not
        signed in as the owner. A read."""
        parts = urlsplit(path)
        response = await self._reads._send(
            sdk_base.Request(
                "GET", parts.path, None, True, False, params=dict(parse_qsl(parts.query))
            )
        )
        return response.content

    async def transcribe(
        self, data: bytes, filename: str, content_type: str, language: str | None = None
    ) -> str:
        """`POST /api/speech/transcribe`: one clip in, its words out. The
        request is the SPA's multipart `file`; the optional BCP 47
        `language` hint rides beside it as a bare form field (httpx drops a
        json body when files are set, so it goes as an empty-named part,
        which is the same thing a FormData append produces). Nothing is
        kept server-side, so the reads' retries do no harm."""
        files: dict[str, tuple[str, bytes, str]] = {
            "file": (filename, data, content_type),
        }
        if language:
            files["language"] = ("", language.encode("utf-8"), "text/plain")
        body = await self._app_json(self._reads, "POST", _path("speech", "transcribe"), files=files)
        return str(body.get("text") or "").strip()

    async def budget(self) -> dict[str, Any]:
        """`GET /api/budget` by way of the SDK's typed resource: the org's
        allowance and usage with the overage cap in force, for the status
        line, flattened to the dict the app reads."""
        return _row_dict(await self._reads.budget.get())

    async def usage(self) -> UsageBalance:
        """`GET /api/me/usage` by way of the SDK: the org's balance this
        period, for the status line."""
        return await self._reads.usage.get()

    async def upload_attachment(
        self, data: bytes, filename: str, conversation_id: str | None = None
    ) -> Attachment:
        """`POST /api/attachments` by way of the SDK: the id the next turn
        names in `attachment_ids`. An upload that lands twice would store
        the file twice, so it is a write: no retries."""
        return await self._writes.attachments.upload(
            data, filename, conversation_id=conversation_id
        )

    async def attachment_limits(self) -> AttachmentLimits:
        """`GET /api/attachments/limits/me` by way of the SDK: the most one
        upload may be and what is left of the caller's quota, so a file
        too big is said before its bytes are sent."""
        return await self._reads.attachments.limits()

    async def attachment_scan_status(self, attachment_id: str) -> str:
        """`GET /api/attachments/{id}/scan`: where the malware scan of one of
        the caller's own uploads stands -- `scanning`, `clean`, `infected`
        or `scan_failed`. A deployment that scans after the upload answers
        the upload `scanning`, and a turn naming the file is refused until
        it is `clean`. The SDK has no method for the route; a read."""
        body = await self._app_json(self._reads, "GET", _path("attachments", attachment_id, "scan"))
        return str(body.get("scan_status") or "")

    async def delete_attachment(self, attachment_id: str) -> None:
        """`DELETE /api/attachments/{id}` by way of the SDK: an upload taken
        off the message before it was sent. A write: no retries."""
        await self._writes.attachments.delete(attachment_id)

    async def context(self, conversation_id: str) -> ConversationContext:
        return await self._reads.conversations.context(conversation_id)

    async def compact(self, conversation_id: str) -> ConversationMessage:
        return await self._writes.conversations.compact(conversation_id)

    # --- the library (folders and tags) ---------------------------------------

    async def library(self) -> dict[str, Any]:
        """`GET /api/library` by way of the SDK's typed resource: the
        caller's folders, tags and which conversation carries which tag,
        flattened to the dicts the app reads."""
        lib = await self._reads.library.get()
        return {
            "folders": [_row_dict(folder) for folder in lib.folders],
            "tags": [_row_dict(tag) for tag in lib.tags],
            "assignments": {cid: list(ids) for cid, ids in lib.assignments.items()},
        }

    async def create_folder(self, name: str) -> dict[str, Any]:
        """`POST /api/library/folders` by way of the SDK. A write: no
        retries."""
        return _row_dict(await self._writes.library.create_folder(name))

    async def rename_folder(self, folder_id: str, name: str) -> dict[str, Any]:
        """`PATCH /api/library/folders/{id}` by way of the SDK: the folder
        with its new name (a name another folder has is the server's 409).
        A write: no retries."""
        return _row_dict(await self._writes.library.update_folder(folder_id, name=name))

    async def delete_folder(self, folder_id: str) -> None:
        """`DELETE /api/library/folders/{id}` by way of the SDK: the folder
        goes and its conversations return to the main list; nothing else
        is deleted. A write: no retries."""
        await self._writes.library.delete_folder(folder_id)

    async def delete_tag(self, tag_id: str) -> None:
        """`DELETE /api/library/tags/{id}` by way of the SDK: the tag goes,
        and off every conversation that carried it. A write: no retries."""
        await self._writes.library.delete_tag(tag_id)

    async def move_to_folder(self, conversation_id: str, folder_id: str | None) -> None:
        """`PUT /api/library/conversations/{id}/folder` by way of the SDK;
        None files the conversation back in the main list."""
        await self._writes.library.move(conversation_id, folder_id)

    async def create_tag(self, name: str) -> dict[str, Any]:
        """`POST /api/library/tags` by way of the SDK: the tag of that
        name, made now or already there. A write: no retries."""
        return _row_dict(await self._writes.library.create_tag(name))

    async def set_tags(self, conversation_id: str, tag_ids: Sequence[str]) -> dict[str, Any]:
        """`PUT /api/library/conversations/{id}/tags` by way of the SDK:
        the conversation's tags become exactly these."""
        return _row_dict(await self._writes.library.set_tags(conversation_id, tag_ids))

    def send(
        self,
        conversation_id: str,
        content: str,
        model: str | None = None,
        attachment_ids: list[str] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]:
        """A turn; `temperature` None leaves the server's default."""
        return self._stream(
            self._writes.conversations.send(
                conversation_id,
                content,
                model=model,
                attachment_ids=attachment_ids,
                temperature=temperature,
            )
        )

    def edit(
        self,
        conversation_id: str,
        message_id: str,
        content: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]:
        return self._stream(
            self._writes.conversations.edit(
                conversation_id, message_id, content, model=model, temperature=temperature
            )
        )

    def regenerate(
        self,
        conversation_id: str,
        message_id: str,
        model: str | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[StreamItem]:
        return self._stream(
            self._writes.conversations.regenerate(
                conversation_id, message_id, model=model, temperature=temperature
            )
        )

    # --- agent mode: /v1 and /mcp ---------------------------------------------

    async def complete(
        self,
        messages: Sequence[dict[str, Any]],
        *,
        model: str | None,
        tools: Sequence[dict[str, Any]],
        conversation_id: str | None,
    ) -> AsyncIterator[CompletionEvent]:
        """One `POST /v1/chat/completions` of an agent run, streamed: the
        text as it arrives, then the whole reply with its tool calls (the
        SDK's accumulator puts them back together) and the usage. The
        server runs none of the calls. `conversation_id` stores the round in
        that conversation. A write: no retries, since a retried call is
        billed and stored twice. Closing the iterator closes the stream."""
        stream = await self._writes.chat.completions.create(
            [dict(m) for m in messages],
            model=model,
            stream=True,
            include_usage=True,
            tools=[dict(t) for t in tools] or None,
            conversation_id=conversation_id,
        )
        accumulator = ChatCompletionAccumulator()
        try:
            async for chunk in stream:
                accumulator.add(chunk)
                if chunk.content:
                    yield TextDelta(chunk.content)
        finally:
            await stream.close()
        yield Completed(
            accumulator.message(), accumulator.usage, accumulator.finish_reason, stream.request_id
        )

    async def mcp_tools(self) -> list[dict[str, Any]]:
        """MatterWave's own `/mcp` tools for the caller's credential, by way
        of the SDK's MCP client, in the shape an MCP server lists them."""
        tools = await self._reads.mcp.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "annotations": t.annotations,
            }
            for t in tools
        ]

    async def mcp_call(self, name: str, arguments: dict[str, Any]) -> tuple[str, bool]:
        """One `/mcp` tool call by way of the SDK: its text, and whether the
        tool refused (a budget, a missing conversation)."""
        try:
            result = await self._writes.mcp.call_tool(name, arguments)
        except McpToolError as exc:
            return str(exc), True
        return result.text, False

    # --- the stream ----------------------------------------------------------

    async def _stream(
        self, opening: Awaitable[AsyncConversationStream]
    ) -> AsyncIterator[StreamItem]:
        """The SDK's frames with `Opened` in front and `Cut` where the SDK
        gives up: it resumes a dropped body once by `Last-Event-ID`, and
        raises a connection error when that cannot bring the rest."""
        stream = await opening
        yield Opened(stream.request_id)
        finished = False
        try:
            async for frame in stream:
                yield frame
                if isinstance(frame, TERMINAL_FRAMES):
                    finished = True
                    return
        except APIConnectionError:
            # Everything before the drop was yielded and stands; the app
            # reads the tree back for what the server kept.
            pass
        finally:
            await stream.close()
        if not finished:
            yield Cut(stream.request_id)
