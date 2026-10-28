"""The `/api` routes the TUI uses, behind a Protocol the app is written against.

The wire is the SDK's: `AsyncMatterWave.conversations` for the routes, its
`AsyncConversationStream` for a turn (named events, typed frames, one resume
by `Last-Event-ID`), its errors with the request id on them. What stays here
is the TUI's own policy: a 401 is answered once with a fresh credential
before it stands, a write is never retried, and the routes the SDK has no
usable method for yet (`/api/models`, the conversation delete, pin and
archive routes, the per-conversation feedback list and the library's
folders and tags) go through the same client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from matterwave_sdk import AsyncConversationStream, AsyncMatterWave
from matterwave_sdk import _base as sdk_base
from matterwave_sdk._client import USER_AGENT as SDK_USER_AGENT
from matterwave_sdk.errors import APIConnectionError, AuthenticationError
from matterwave_sdk.frames import TERMINAL_FRAMES, ConversationFrame
from matterwave_sdk.models import (
    Attachment,
    Conversation,
    ConversationContext,
    ConversationMessage,
    Me,
    MessageFeedback,
    RemovedFeedback,
    UsageBalance,
)

from . import __version__

# A local API; anything else comes from --base-url or MATTERWAVE_BASE_URL.
DEFAULT_BASE_URL = "http://localhost:8000"
USER_AGENT = f"matterwave-tui/{__version__} {SDK_USER_AGENT}"


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


class ChatAPI(Protocol):
    """Everything the app asks of the platform."""

    async def me(self) -> Me: ...

    async def models(self) -> dict[str, Any]: ...

    async def list_conversations(self, filter: str = "default") -> list[Conversation]: ...

    async def search_conversations(self, query: str) -> list[Conversation]: ...

    async def create_conversation(
        self, title: str | None = None, model: str | None = None, temporary: bool = False
    ) -> Conversation: ...

    async def tree(self, conversation_id: str) -> Conversation: ...

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation: ...

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation: ...

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation: ...

    async def delete_conversation(self, conversation_id: str) -> None: ...

    async def set_pinned(self, conversation_id: str, pinned: bool) -> Conversation: ...

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation: ...

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation: ...

    async def share_conversation(self, conversation_id: str) -> str: ...

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

    async def usage(self) -> UsageBalance: ...

    async def upload_attachment(
        self, data: bytes, filename: str, conversation_id: str | None = None
    ) -> Attachment: ...

    async def context(self, conversation_id: str) -> ConversationContext: ...

    async def compact(self, conversation_id: str) -> ConversationMessage: ...

    def send(
        self,
        conversation_id: str,
        content: str,
        model: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> AsyncIterator[StreamItem]: ...

    def edit(
        self, conversation_id: str, message_id: str, content: str, model: str | None = None
    ) -> AsyncIterator[StreamItem]: ...

    def regenerate(
        self, conversation_id: str, message_id: str, model: str | None = None
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

    async def _send(self, request: sdk_base.Request) -> httpx.Response:
        self._config.api_key = self._credential.token
        try:
            return await super()._send(request)
        except AuthenticationError:
            if not await self._credential.renew(self._config.api_key or ""):
                raise
            self._config.api_key = self._credential.token
            return await super()._send(request)


def _path(*segments: str) -> str:
    return "/api/" + "/".join(quote(s, safe="") for s in segments)


class HttpChatAPI:
    """`ChatAPI` over the SDK."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        refresh: TokenRefresher | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient()
        credential = _Credential(token, refresh)
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
        self, client: _Client, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """A route the SDK has no method for yet, sent the way its own are."""
        response = await client._send(sdk_base.Request(method, path, body, True, False))
        return sdk_base.parse_json_object(response)

    async def _app_none(
        self, client: _Client, method: str, path: str, body: dict[str, Any] | None = None
    ) -> None:
        """As `_app_json`, for a route that answers no body."""
        response = await client._send(sdk_base.Request(method, path, body, True, False))
        await response.aclose()

    # --- routes --------------------------------------------------------------

    async def me(self) -> Me:
        return await self._reads.me()

    async def models(self) -> dict[str, Any]:
        """`GET /api/models`: the picker's rows, with each model's plan
        gating; the SDK's `models.list()` is `/v1/models`, which has none
        of that."""
        return await self._app_json(self._reads, "GET", _path("models"))

    async def list_conversations(self, filter: str = "default") -> list[Conversation]:
        return (await self._reads.conversations.list(filter=filter)).data

    async def search_conversations(self, query: str) -> list[Conversation]:
        hits = await self._reads.conversations.search(query)
        return [hit.conversation for hit in hits.data]

    async def create_conversation(
        self, title: str | None = None, model: str | None = None, temporary: bool = False
    ) -> Conversation:
        """`POST /api/conversations`. `temporary` is fixed at creation and
        the key is sent only when it is on; the SDK's `create` has no
        `temporary` yet, so that one goes through the raw client."""
        if not temporary:
            return await self._writes.conversations.create(title=title, model=model)
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if model is not None:
            body["model"] = model
        body["temporary"] = True
        return Conversation.from_dict(
            await self._app_json(self._writes, "POST", _path("conversations"), body)
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

    async def rename_conversation(self, conversation_id: str, title: str) -> Conversation:
        """The SDK's update with a title: a PATCH that lands twice writes
        the same title twice, so it may take the reads' retries."""
        return await self._reads.conversations.update(conversation_id, title=title)

    async def delete_conversation(self, conversation_id: str) -> None:
        """`DELETE /api/conversations/{id}`: gone for good, answered with no
        body. A delete that lands twice deletes the same nothing, but a
        delete is still a write: no retries."""
        await self._app_none(self._writes, "DELETE", _path("conversations", conversation_id))

    async def set_pinned(self, conversation_id: str, pinned: bool) -> Conversation:
        """`POST` / `DELETE /api/conversations/{id}/pin`, answered with the
        updated conversation."""
        method = "POST" if pinned else "DELETE"
        return Conversation.from_dict(
            await self._app_json(self._reads, method, _path("conversations", conversation_id, "pin"))
        )

    async def set_archived(self, conversation_id: str, archived: bool) -> Conversation:
        """`POST` / `DELETE /api/conversations/{id}/archive`, answered with
        the updated conversation."""
        method = "POST" if archived else "DELETE"
        return Conversation.from_dict(
            await self._app_json(
                self._reads, method, _path("conversations", conversation_id, "archive")
            )
        )

    async def fork_conversation(
        self, conversation_id: str, message_id: str, title: str | None = None
    ) -> Conversation:
        """The SDK's fork: a new conversation holding the path up to the
        message, which the caller then opens like any other."""
        return await self._writes.conversations.fork(conversation_id, message_id, title=title)

    async def share_conversation(self, conversation_id: str) -> str:
        """A read-only link to a snapshot of the active path, as a full URL
        against this deployment."""
        created = await self._writes.shares.create(conversation_id)
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
        """`GET /api/conversations/{id}/feedback`: the caller's own ratings
        in the thread, to mark the transcript with when it opens. The SDK
        has no method for the route yet."""
        body = await self._app_json(
            self._reads, "GET", _path("conversations", conversation_id, "feedback")
        )
        rows = body.get("data")
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

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
        return await self._writes.attachments.upload(data, filename, conversation_id=conversation_id)

    async def context(self, conversation_id: str) -> ConversationContext:
        return await self._reads.conversations.context(conversation_id)

    async def compact(self, conversation_id: str) -> ConversationMessage:
        return await self._writes.conversations.compact(conversation_id)

    # --- the library (folders and tags): adapters only, no UI yet -------------

    async def library(self) -> dict[str, Any]:
        """`GET /api/library`: the caller's folders, tags and which
        conversation carries which tag. The SDK's `Tag` model does not
        survive its own `from_dict` yet, so the raw answer is kept."""
        return await self._app_json(self._reads, "GET", _path("library"))

    async def create_folder(self, name: str) -> dict[str, Any]:
        """`POST /api/library/folders`. A write: no retries."""
        return await self._app_json(
            self._writes, "POST", _path("library", "folders"), {"name": name}
        )

    async def move_to_folder(self, conversation_id: str, folder_id: str | None) -> None:
        """`PUT /api/library/conversations/{id}/folder`; None files the
        conversation back in the main list. The key is always sent."""
        await self._app_none(
            self._writes,
            "PUT",
            _path("library", "conversations", conversation_id, "folder"),
            {"folder_id": folder_id},
        )

    async def create_tag(self, name: str) -> dict[str, Any]:
        """`POST /api/library/tags`: the tag of that name, made now or
        already there. A write: no retries."""
        return await self._app_json(self._writes, "POST", _path("library", "tags"), {"name": name})

    async def set_tags(self, conversation_id: str, tag_ids: Sequence[str]) -> dict[str, Any]:
        """`PUT /api/library/conversations/{id}/tags`: the conversation's
        tags become exactly these."""
        return await self._app_json(
            self._writes,
            "PUT",
            _path("library", "conversations", conversation_id, "tags"),
            {"tag_ids": list(tag_ids)},
        )

    def send(
        self,
        conversation_id: str,
        content: str,
        model: str | None = None,
        attachment_ids: list[str] | None = None,
    ) -> AsyncIterator[StreamItem]:
        return self._stream(
            self._writes.conversations.send(
                conversation_id, content, model=model, attachment_ids=attachment_ids
            )
        )

    def edit(
        self, conversation_id: str, message_id: str, content: str, model: str | None = None
    ) -> AsyncIterator[StreamItem]:
        return self._stream(
            self._writes.conversations.edit(conversation_id, message_id, content, model=model)
        )

    def regenerate(
        self, conversation_id: str, message_id: str, model: str | None = None
    ) -> AsyncIterator[StreamItem]:
        return self._stream(
            self._writes.conversations.regenerate(conversation_id, message_id, model=model)
        )

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
