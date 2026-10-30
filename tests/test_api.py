from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from matterwave_sdk import frames as fr
from matterwave_sdk.errors import AuthenticationError, ServerError
from matterwave_sdk.models import Conversation

from matterwave_tui.api import Cut, HttpChatAPI, Opened

BASE = "https://matterwave.test"

CONV = {"id": "c1", "title": "t", "created_at": "1", "updated_at": "1"}


def make(handler, **kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpChatAPI(base_url=BASE, token="mw_one", http_client=http, **kwargs)


class DroppingStream(httpx.AsyncByteStream):
    """A body that sends some frames, then the connection dies."""

    def __init__(self, text: str) -> None:
        self.text = text

    async def __aiter__(self):
        yield self.text.encode()
        raise httpx.ReadError("connection reset")

    async def aclose(self) -> None:
        return None


FIRST_HALF = (
    'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
    'id: g1:0\nevent: part_start\ndata: {"index": 0, "type": "text"}\n\n'
    'id: g1:1\nevent: delta\ndata: {"content": "Hel", "index": 0}\n\n'
)
SECOND_HALF = (
    'id: g1:2\nevent: delta\ndata: {"content": "lo", "index": 0}\n\n'
    'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n'
)


def sse(status: int, body: str | DroppingStream, n: int) -> httpx.Response:
    headers = {"x-request-id": f"req-{n}", "content-type": "text/event-stream"}
    if isinstance(body, DroppingStream):
        return httpx.Response(status, headers=headers, stream=body)
    return httpx.Response(status, headers=headers, text=body)


@pytest.mark.asyncio
async def test_a_dropped_stream_resumes_with_last_event_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return sse(200, DroppingStream(FIRST_HALF), 1)
        return sse(200, SECOND_HALF, 2)

    items = [i async for i in make(handler).send("c1", "hello", "grt-nano")]
    assert items[0] == Opened("req-1")
    deltas = [i.content for i in items if isinstance(i, fr.DeltaFrame)]
    assert deltas == ["Hel", "lo"]
    assert isinstance(items[-1], fr.DoneFrame)
    assert json.loads(seen[0].content) == {"content": "hello", "model": "grt-nano"}
    assert seen[0].headers["authorization"] == "Bearer mw_one"
    assert seen[0].headers["accept"] == "text/event-stream"
    assert seen[1].headers["last-event-id"] == "g1:1"
    assert seen[1].url.path == "/api/conversations/c1/messages"


@pytest.mark.asyncio
async def test_a_resume_refused_with_401_signs_in_again_and_carries_on():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if len(seen) == 1:
            return sse(200, DroppingStream(FIRST_HALF), 1)
        if request.headers["authorization"] != "Bearer mw_two":
            return httpx.Response(401, json={"detail": "revoked"})
        return sse(200, SECOND_HALF, 3)

    async def refresh() -> str | None:
        return "mw_two"

    items = [i async for i in make(handler, refresh=refresh).send("c1", "hello")]
    assert [i.content for i in items if isinstance(i, fr.DeltaFrame)] == ["Hel", "lo"]
    assert isinstance(items[-1], fr.DoneFrame)
    assert [r.headers["authorization"] for r in seen] == [
        "Bearer mw_one", "Bearer mw_one", "Bearer mw_two",
    ]
    assert all(r.headers.get("last-event-id") == "g1:1" for r in seen[1:])


@pytest.mark.asyncio
async def test_a_stream_without_ids_that_ends_early_is_cut():
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(200, 'event: message\ndata: {"id": "u1"}\n\n', 9)

    items = [i async for i in make(handler).send("c1", "hi")]
    assert isinstance(items[1], fr.MessageFrame)
    assert items[-1] == Cut("req-9")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "kind", "field", "value"),
    [
        ("budget.sse", fr.ErrorFrame, "code", "budget"),
        ("moderation.sse", fr.ModerationBlockFrame, "request_id", "req-mod-1"),
    ],
)
async def test_recorded_refusals_arrive_as_their_frame(recorded, name, kind, field, value):
    def handler(request: httpx.Request) -> httpx.Response:
        return sse(200, recorded(name), 1)

    items = [i async for i in make(handler).send("c1", "hi")]
    assert isinstance(items[-1], kind)
    assert getattr(items[-1], field) == value
    assert not any(isinstance(i, Cut) for i in items)


@pytest.mark.asyncio
async def test_a_401_refreshes_once_then_stands():
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if request.headers["authorization"] == "Bearer mw_two":
            return httpx.Response(200, json={"data": [CONV]})
        return httpx.Response(401, json={"detail": "no"}, headers={"x-request-id": "r401"})

    refreshes = 0

    async def refresh() -> str | None:
        nonlocal refreshes
        refreshes += 1
        return "mw_two"

    convs = await make(handler, refresh=refresh).list_conversations()
    assert [c.id for c in convs] == ["c1"]
    assert isinstance(convs[0], Conversation)
    assert tokens == ["Bearer mw_one", "Bearer mw_two"]
    assert refreshes == 1

    async def same() -> str | None:
        return "mw_one"

    with pytest.raises(AuthenticationError):
        await make(handler, refresh=same).list_conversations()


@pytest.mark.asyncio
async def test_concurrent_401s_share_one_sign_in():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers["authorization"] == "Bearer mw_two":
            return httpx.Response(200, json={"data": [CONV]})
        return httpx.Response(401, json={"detail": "no"})

    refreshes = 0

    async def refresh() -> str | None:
        nonlocal refreshes
        refreshes += 1
        await asyncio.sleep(0.01)
        return "mw_two"

    api = make(handler, refresh=refresh)
    first, second = await asyncio.gather(api.list_conversations(), api.search_conversations("t"))
    assert [c.id for c in first] == ["c1"] and [c.id for c in second] == ["c1"]
    assert refreshes == 1


@pytest.mark.asyncio
async def test_writes_are_not_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "busy"})

    with pytest.raises(ServerError):
        await make(handler).create_conversation(title="x")
    assert calls == 1


@pytest.mark.asyncio
async def test_the_picker_routes_go_through_the_same_client_as_the_tui():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/models":
            return httpx.Response(200, json={"data": [{"id": "m"}], "default_model": "m"})
        return httpx.Response(200, json={**CONV, "default_model": "grt-max"})

    api = make(handler)
    assert (await api.models())["default_model"] == "m"
    updated = await api.set_default_model("c1", "grt-max")
    assert updated.default_model == "grt-max"
    patch = seen[-1]
    assert (patch.method, patch.url.path) == ("PATCH", "/api/conversations/c1")
    assert json.loads(patch.content) == {"default_model": "grt-max"}
    assert all(r.headers["authorization"] == "Bearer mw_one" for r in seen)
    # The TUI names itself on the wire, ahead of the SDK it is built on.
    assert all(r.headers["user-agent"].startswith("matterwave-tui/") for r in seen)
    assert all("matterwave-python/" in r.headers["user-agent"] for r in seen)


PINNED = {**CONV, "pinned_at": "2026-10-27T00:00:00Z"}
ARCHIVED = {**CONV, "archived_at": "2026-10-27T00:00:00Z"}
USAGE = {
    "unit": "tokens", "used": 1234, "included": 100000, "remaining": 98766,
    "percent": 1.234, "warn": False, "in_overage": False, "exhausted": False,
    "period_start": "2026-10-01", "resets_at": "2026-11-01",
    "plan": {"name": "Free"}, "overage": {},
}


@pytest.mark.asyncio
async def test_rename_pin_and_archive_take_their_routes():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path, method = request.url.path, request.method
        if method == "PATCH":
            return httpx.Response(200, json={**CONV, "title": json.loads(request.content)["title"]})
        if path == "/api/conversations/c1/pin":
            # The platform registers PUT only: a POST would get a 405.
            return httpx.Response(200, json=PINNED if method == "PUT" else CONV)
        if path == "/api/conversations/c1/archive":
            return httpx.Response(200, json=ARCHIVED if method == "PUT" else CONV)
        return httpx.Response(404, json={"detail": path})

    api = make(handler)
    renamed = await api.rename_conversation("c1", "New name")
    assert renamed.title == "New name"
    pinned = await api.set_pinned("c1", True)
    assert pinned.pinned_at == "2026-10-27T00:00:00Z"
    unpinned = await api.set_pinned("c1", False)
    assert unpinned.pinned_at is None
    archived = await api.set_archived("c1", True)
    assert archived.archived_at == "2026-10-27T00:00:00Z"
    unarchived = await api.set_archived("c1", False)
    assert unarchived.archived_at is None
    assert [(r.method, r.url.path) for r in seen] == [
        ("PATCH", "/api/conversations/c1"),
        ("PUT", "/api/conversations/c1/pin"),
        ("DELETE", "/api/conversations/c1/pin"),
        ("PUT", "/api/conversations/c1/archive"),
        ("DELETE", "/api/conversations/c1/archive"),
    ]
    assert json.loads(seen[0].content) == {"title": "New name"}


@pytest.mark.asyncio
async def test_a_delete_is_a_bare_write_that_is_never_retried():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert (request.method, request.url.path) == ("DELETE", "/api/conversations/c1")
        return httpx.Response(503, json={"detail": "busy"})

    with pytest.raises(ServerError):
        await make(handler).delete_conversation("c1")
    assert calls == 1

    def gone(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    await make(gone).delete_conversation("c1")


@pytest.mark.asyncio
async def test_fork_share_and_usage_go_through_the_sdk():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/conversations/c1/fork":
            return httpx.Response(200, json={**CONV, "id": "c2", "title": "Fork"})
        if path == "/api/conversations/c1/share":
            return httpx.Response(
                200, json={"share": {"id": "s1"}, "token": "tok", "path": "/share/tok"}
            )
        if path == "/api/me/usage":
            return httpx.Response(200, json=USAGE)
        return httpx.Response(404, json={"detail": path})

    api = make(handler)
    forked = await api.fork_conversation("c1", "m1")
    assert (forked.id, forked.title) == ("c2", "Fork")
    assert json.loads(seen[0].content) == {"message_id": "m1"}
    assert seen[0].url.path == "/api/conversations/c1/fork"
    assert await api.share_conversation("c1") == BASE + "/share/tok"
    balance = await api.usage()
    assert (balance.used, balance.included, balance.percent) == (1234, 100000, 1.234)


NOTIFICATION = {
    "id": "n1", "kind": "budget.warn", "title": "80% of this period's tokens used",
    "body": "Usage past them is refused or billed at list price.",
    "link": "/settings/usage", "created_at": "2026-10-29T00:00:00Z", "read_at": None,
}
SHARE = {
    "id": "s1", "conversation_id": "c1", "title": "t", "message_count": 2,
    "include_attachments": False, "created_at": "2026-10-29T00:00:00Z",
    "expires_at": None, "revoked_at": None, "view_count": 3, "status": "active",
}
BUDGET = {
    "period_start": "2026-10-01", "included_tokens": 100000, "tokens_used": 1234,
    "remaining_tokens": 98766, "overage_enabled": True, "overage_cap_micros": 1000000,
    "overage_micros": 250000, "member_default_cap_micros": None,
    "member_cap": None, "member_spent": 0, "member_exhausted": False,
}


@pytest.mark.asyncio
async def test_notifications_announcements_and_budget_take_their_routes():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path, method = request.url.path, request.method
        if path == "/api/me/notifications" and method == "GET":
            return httpx.Response(200, json={"data": [NOTIFICATION], "unread_count": 1})
        if path == "/api/me/notifications/n1/read":
            return httpx.Response(
                200, json={**NOTIFICATION, "read_at": "2026-10-30T00:00:00Z", "unread_count": 0}
            )
        if path == "/api/me/notifications/read-all":
            return httpx.Response(200, json={"read": 1, "unread_count": 0})
        if path == "/api/announcements":
            return httpx.Response(200, json={"data": [{"id": "an1", "message": "hi"}]})
        if path == "/api/budget":
            return httpx.Response(200, json=BUDGET)
        return httpx.Response(404, json={"detail": path})

    api = make(handler)
    inbox = await api.notifications()
    assert inbox["unread_count"] == 1
    read = await api.read_notification("n1")
    assert read["read_at"] == "2026-10-30T00:00:00Z"
    assert await api.read_all_notifications() == {"read": 1, "unread_count": 0}
    assert (await api.announcements())["data"][0]["id"] == "an1"
    budget = await api.budget()
    assert (budget["overage_micros"], budget["overage_cap_micros"]) == (250000, 1000000)
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/me/notifications"),
        ("POST", "/api/me/notifications/n1/read"),
        ("POST", "/api/me/notifications/read-all"),
        ("GET", "/api/announcements"),
        ("GET", "/api/budget"),
    ]


@pytest.mark.asyncio
async def test_a_report_posts_kind_and_note_and_is_never_retried():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert (request.method, request.url.path) == ("POST", "/api/conversations/c1/report")
        return httpx.Response(201, json={"id": "rep1", "kind": "unhelpful"})

    filed = await make(handler).report_conversation("c1", "unhelpful", note="repeats itself")
    assert filed == {"id": "rep1", "kind": "unhelpful"}
    assert json.loads(seen[0].content) == {"kind": "unhelpful", "note": "repeats itself"}

    calls = 0

    def busy(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"detail": "busy"})

    with pytest.raises(ServerError):
        await make(busy).report_conversation("c1", "unsafe")
    assert calls == 1


@pytest.mark.asyncio
async def test_share_management_lists_and_revokes():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path == "/api/me/shares":
            return httpx.Response(200, json={"data": [SHARE]})
        if path == "/api/conversations/c1/shares":
            return httpx.Response(200, json={"data": [SHARE]})
        if path == "/api/shares/s1":
            return httpx.Response(200, json={"status": "revoked"})
        return httpx.Response(404, json={"detail": path})

    api = make(handler)
    mine = await api.my_shares()
    assert [s["id"] for s in mine] == ["s1"]
    theirs = await api.conversation_shares("c1")
    assert [s["id"] for s in theirs] == ["s1"]
    assert await api.revoke_share("s1") == {"status": "revoked"}
    assert [(r.method, r.url.path) for r in seen] == [
        ("GET", "/api/me/shares"),
        ("GET", "/api/conversations/c1/shares"),
        ("DELETE", "/api/shares/s1"),
    ]


@pytest.mark.asyncio
async def test_an_attachment_is_uploaded_as_multipart():
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/api/attachments")
        assert request.url.params["conversation_id"] == "c1"
        body = request.content.decode("utf-8", "replace")
        assert "picture.png" in body and "PNGDATA" in body
        return httpx.Response(
            200,
            json={"id": "att1", "filename": "picture.png", "content_type": "image/png",
                  "size_bytes": 7, "scan_status": "clean", "created_at": "1"},
        )

    attachment = await make(handler).upload_attachment(b"PNGDATA", "picture.png", "c1")
    assert attachment.id == "att1"
    assert attachment.content_type == "image/png"


DONE_ONLY = 'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n'


@pytest.mark.asyncio
async def test_send_names_the_attachments_on_the_wire():
    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content) == {
            "content": "look", "model": "grt-nano", "attachment_ids": ["att1"],
        }
        return sse(200, DONE_ONLY, 1)

    items = [i async for i in make(handler).send("c1", "look", "grt-nano",
                                                 attachment_ids=["att1"])]
    assert isinstance(items[-1], fr.DoneFrame)
