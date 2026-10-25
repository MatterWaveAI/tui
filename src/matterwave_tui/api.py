"""The `/api` routes the TUI uses, behind a Protocol the app is written against.

The wire is the SDK's: `AsyncMatterWave.conversations` for the routes, its
`AsyncConversationStream` for a turn (named events, typed frames, one resume
by `Last-Event-ID`), its errors with the request id on them. What stays here
is the TUI's own policy: a 401 is answered once with a fresh credential
before it stands, a write is never retried, and the one route the SDK has
no method for yet (the picker's `/api/models`) goes through the same client.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

import httpx
from matterwave_sdk import AsyncConversationStream, AsyncMatterWave
from matterwave_sdk import _base as sdk_base
from matterwave_sdk._client import USER_AGENT as SDK_USER_AGENT
from matterwave_sdk.errors import APIConnectionError, AuthenticationError
from matterwave_sdk.frames import TERMINAL_FRAMES, ConversationFrame
from matterwave_sdk.models import Conversation, ConversationContext, ConversationMessage, Me

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
        self, title: str | None = None, model: str | None = None
    ) -> Conversation: ...

    async def tree(self, conversation_id: str) -> Conversation: ...

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation: ...

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation: ...

    async def context(self, conversation_id: str) -> ConversationContext: ...

    async def compact(self, conversation_id: str) -> ConversationMessage: ...

    def send(
        self, conversation_id: str, content: str, model: str | None = None
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
        self, title: str | None = None, model: str | None = None
    ) -> Conversation:
        return await self._writes.conversations.create(title=title, model=model)

    async def tree(self, conversation_id: str) -> Conversation:
        return await self._reads.conversations.tree(conversation_id)

    async def set_active_leaf(self, conversation_id: str, message_id: str) -> Conversation:
        return await self._reads.conversations.set_active_leaf(conversation_id, message_id)

    async def set_default_model(self, conversation_id: str, model: str | None) -> Conversation:
        """The picker's choice, kept on the conversation so the web app opens
        it on the same model. None clears it. A PATCH that lands twice sets
        the same value twice, so it may take the reads' retries."""
        return await self._reads.conversations.update(conversation_id, default_model=model)

    async def context(self, conversation_id: str) -> ConversationContext:
        return await self._reads.conversations.context(conversation_id)

    async def compact(self, conversation_id: str) -> ConversationMessage:
        return await self._writes.conversations.compact(conversation_id)

    def send(
        self, conversation_id: str, content: str, model: str | None = None
    ) -> AsyncIterator[StreamItem]:
        return self._stream(self._writes.conversations.send(conversation_id, content, model=model))

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
