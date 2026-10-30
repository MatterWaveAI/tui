from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest
from matterwave_sdk import frames as fr
from matterwave_sdk.errors import APIError, RateLimitError
from matterwave_sdk.models import Conversation
from textual.widgets import Collapsible, Input, ListView, Markdown, OptionList, Static, TextArea

from matterwave_tui.api import Cut, HttpChatAPI, Opened, StreamItem
from matterwave_tui.app import (
    IMPORT_MAX_BYTES,
    TEMPORARY_NOTICE,
    MatterWaveApp,
    Transcript,
    TuiCommands,
)
from matterwave_tui.auth import DeviceCode
from matterwave_tui.screens import (
    Accounts,
    Confirm,
    DeviceLogin,
    Dictation,
    FeedbackDialog,
    InstructionsEditor,
    Library,
    ModelPicker,
    Notifications,
    Prompt,
    Shares,
)
from matterwave_tui.widgets import DividerView, ImageView, MessageView

from .fake_api import (
    FakeAPI,
    announcement,
    conversation,
    happy_script,
    message,
    notification,
    share_row,
)

BASE = "https://matterwave.test"
CONV = {"id": "c1", "title": "New chat", "default_model": "grt-nano",
        "created_at": "1", "updated_at": "1", "current_leaf_message_id": None}


def transcript_text(app: MatterWaveApp) -> str:
    return "\n".join(md.source for view in app.query(MessageView) for md in view.query(Markdown))


def status(app: MatterWaveApp) -> str:
    return str(app.query_one("#status", Static).render())


def banner(app: MatterWaveApp) -> str:
    return str(app.query_one("#failure", Static).render())


def sidebar_titles(app: MatterWaveApp) -> list[str]:
    return [item.conversation.title for item in app.query_one("#conversations", ListView).children]


def sidebar_captions(app: MatterWaveApp) -> list[str]:
    return [item.caption() for item in app.query_one("#conversations", ListView).children]


def capture_notify(app: MatterWaveApp) -> list[str]:
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    return shown


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def http_api(handler, **kwargs) -> HttpChatAPI:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpChatAPI(base_url=BASE, token="mw_k", http_client=http, **kwargs)


def json_routes(request: httpx.Request) -> httpx.Response | None:
    """The reads the app makes on start-up, for a MockTransport handler."""
    path = request.url.path
    if path == "/api/models":
        return httpx.Response(200, json={"data": [], "default_model": "grt-nano"})
    if path == "/api/conversations":
        return httpx.Response(200, json={"data": [CONV]})
    if path == "/api/conversations/c1/tree":
        return httpx.Response(200, json={**CONV, "messages": []})
    if path == "/api/conversations/c1/context":
        return httpx.Response(404, json={"detail": "Not Found"})
    if path == "/api/conversations/c1/feedback":
        return httpx.Response(200, json={"data": []})
    if path == "/api/me/usage":
        return httpx.Response(404, json={"detail": "Not Found"})
    if path in (
        "/api/me/notifications",
        "/api/announcements",
        "/api/budget",
        "/api/library",
    ):
        return httpx.Response(404, json={"detail": "Not Found"})
    return None


@pytest.mark.asyncio
async def test_send_streams_and_finishes():
    api = FakeAPI(happy_script("Hello there"))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.conversation is not None
        await pilot.press("h", "i")
        await pilot.press("ctrl+enter")
        await settle(app, pilot)
        assert api.sent == [("c1", "hi", "grt-nano")]
        assert not app.streaming
        assert [m.id for m in app.thread.path()] == ["u1", "a1"]
        assert "Hello there" in transcript_text(app)
        assert app.failure is None
        assert app.query_one("#composer", TextArea).text == ""
        assert "context 170/8,192" in status(app)
        assert app.conversation.title == "hi"
        assert sidebar_titles(app) == ["hi"]


@pytest.mark.asyncio
async def test_a_budget_error_shows_the_web_copy_and_the_reference():
    api = FakeAPI(
        [Opened("req-budget"), fr.ErrorFrame("monthly token budget is used up", "budget")]
    )
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert app.failure is not None and app.failure.kind == "budget"
        assert "This workspace is out of tokens" in banner(app)
        assert "Reference: req-budget" in banner(app)
        assert "last error ref req-budget" in status(app)


@pytest.mark.asyncio
async def test_a_stream_that_ends_without_done_is_marked_cut_off():
    script = happy_script()[:-2]  # no context, no done
    app = MatterWaveApp(FakeAPI(script))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert app.failure is not None and app.failure.kind == "truncated"
        assert app.failure.request_id == "req-ok"


@pytest.mark.asyncio
async def test_a_cut_stream_reads_back_what_the_server_kept():
    api = FakeAPI(
        [Opened("req-cut"), fr.MessageFrame(message("u1", "user", "hi")),
         fr.PartStartFrame(0, "text"), fr.DeltaFrame(0, "Hel"), Cut("req-cut")]
    )
    kept = message("a1", "assistant", "Hel", parent="u1", created_at="2", truncated=True)
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        api.tree_messages = [message("u1", "user", "hi"), kept]
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert api.tree_reads == 2
        assert app.failure is not None and app.failure.kind == "truncated"
        assert app.failure.request_id == "req-cut"
        assert [m.id for m in app.thread.path()] == ["u1", "a1"]
        assert app.live is None
        marks = [str(s.render()) for s in app.query(MessageView).last().query(".truncated")]
        assert marks and "Cut off" in marks[0]


@pytest.mark.asyncio
async def test_esc_stops_a_streaming_answer():
    api = FakeAPI(happy_script()[:-3], hold=True)  # never reaches done
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        for _ in range(20):
            await pilot.pause()
            if "Hello" in transcript_text(app):
                break
        assert app.streaming
        assert "Esc stops" in status(app)
        await pilot.press("escape")
        await settle(app, pilot)
        assert not app.streaming
        assert api.streams_closed == 1
        assert api.tree_reads == 2
        assert shown == ["Stopped."]
        assert app.failure is None
        assert app.live is None


@pytest.mark.asyncio
async def test_create_then_send_when_there_is_no_conversation():
    api = FakeAPI(happy_script("Made one"))
    api.conversations = []
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.conversation is None
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert app.conversation is not None and app.conversation.id == "c1"
        assert api.sent == [("c1", "hi", None)]
        assert "Made one" in transcript_text(app)
        assert sidebar_titles(app) == ["hi"]
        assert app.query_one("#composer", TextArea).text == ""


@pytest.mark.asyncio
async def test_search_filters_the_sidebar_and_clearing_it_restores_the_list():
    api = FakeAPI()
    api.conversations.append(conversation("c2", title="Other"))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert sidebar_titles(app) == ["New chat", "Other"]
        await pilot.press("escape", "slash", *"oth")
        await asyncio.sleep(0.4)
        await settle(app, pilot)
        assert api.searches[-1] == "oth"
        assert sidebar_titles(app) == ["Other"]
        await pilot.press("backspace", "backspace", "backspace")
        await asyncio.sleep(0.4)
        await settle(app, pilot)
        assert sidebar_titles(app) == ["New chat", "Other"]
        # The sidebar's full list is what `n` and the title frame update.
        assert [c.id for c in app.conversations] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_drafts_are_kept_per_conversation():
    api = FakeAPI()
    api.conversations.append(conversation("c2", title="Other"))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("d", "r", "a", "f", "t")
        composer = app.query_one("#composer", TextArea)
        await app.open_conversation("c2")
        assert composer.text == ""
        await app.open_conversation("c1")
        assert composer.text == "draft"


@pytest.mark.asyncio
async def test_branch_keys_move_the_leaf_and_x_expands_a_summary():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
        message("a1b", "assistant", "two", parent="u1", created_at="3"),
    ]
    api.conversations[0] = conversation(current_leaf_message_id="a1b")
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape")  # to the transcript; the last message is the target
        await pilot.press("left_square_bracket")
        await settle(app, pilot)
        assert api.leaf_moves == ["a1"]
        assert app.thread.leaf == "a1"
        assert "one" in transcript_text(app)

        summary = message(
            "s1", "system", "", parent="a1", created_at="4",
            parts=[{"type": "summary", "text": "folded", "covers_from": "u1", "covers_to": "a1"}],
        )
        app.thread.add(summary)
        app.thread.leaf = "s1"
        await app.render_thread()
        assert len(app.query(DividerView)) == 1
        assert len(app.query(MessageView)) == 0
        await pilot.press("x")
        await settle(app, pilot)
        assert len(app.query(MessageView)) == 2


@pytest.mark.asyncio
async def test_edit_puts_the_message_in_the_composer_and_sends_an_edit():
    api = FakeAPI(happy_script())
    api.tree_messages = [
        message("u1", "user", "old"),
        message("a1", "assistant", "x", parent="u1", created_at="2"),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "e")
        composer = app.query_one("#composer", TextArea)
        assert composer.text == "old"
        assert app.editing == "u1"
        await pilot.press("!", "ctrl+s")
        await settle(app, pilot)
        assert api.sent[-1][1].startswith("edit:u1:")
        await pilot.press("escape", "r")
        await settle(app, pilot)
        assert api.sent[-1][1].startswith("regen:")


@pytest.mark.asyncio
async def test_the_sdk_adapter_feeds_the_app_a_recorded_stream(recorded):
    body = recorded("turn_with_tools.sse")

    def handler(request: httpx.Request) -> httpx.Response:
        if (known := json_routes(request)) is not None:
            return known
        if request.url.path == "/api/conversations/c1/messages":
            assert json.loads(request.content)["content"] == "weather"
            return httpx.Response(200, text=body, headers={"x-request-id": "req-live"})
        return httpx.Response(404, json={"detail": request.url.path})

    app = MatterWaveApp(http_api(handler))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press(*"weather", "ctrl+s")
        await settle(app, pilot)
        assert app.failure is None
        assert "It is raining, 9C." in transcript_text(app)
        view = list(app.query(MessageView))[-1]
        lines = [str(s.render()) for s in view.query(".tool")]
        assert any("web_search" in line for line in lines)
        assert "context 150/8,192" in status(app)
        assert app.conversation is not None and app.conversation.title == "Weather in Oslo?"


class DroppingStream(httpx.AsyncByteStream):
    def __init__(self, text: str) -> None:
        self.text = text

    async def __aiter__(self):
        yield self.text.encode()
        raise httpx.ReadError("connection reset")

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_a_dropped_stream_is_resumed_and_the_answer_is_whole():
    first = (
        'event: message\ndata: {"id": "u1", "role": "user", "content": "hi", "created_at": "1"}\n\n'
        'id: g1:0\nevent: part_start\ndata: {"index": 0, "type": "text"}\n\n'
        'id: g1:1\nevent: delta\ndata: {"content": "Hel", "index": 0}\n\n'
    )
    rest = (
        'id: g1:2\nevent: delta\ndata: {"content": "lo", "index": 0}\n\n'
        'event: done\ndata: {"id": "a1", "role": "assistant", "parent_id": "u1", '
        '"parts": [{"type": "text", "text": "Hello"}], "content": "Hello", "created_at": "2"}\n\n'
    )
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (known := json_routes(request)) is not None:
            return known
        posts.append(request)
        headers = {"x-request-id": f"req-{len(posts)}"}
        if len(posts) == 1:
            return httpx.Response(200, headers=headers, stream=DroppingStream(first))
        return httpx.Response(200, headers=headers, text=rest)

    app = MatterWaveApp(http_api(handler))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert [p.headers.get("last-event-id") for p in posts] == [None, "g1:1"]
        assert app.failure is None
        assert "Hello" in transcript_text(app)
        assert [m.id for m in app.thread.path()] == ["u1", "a1"]
        assert app.connection == "online"


@pytest.mark.asyncio
async def test_a_401_signs_in_again_inside_the_app():
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        tokens.append(request.headers["authorization"])
        if request.headers["authorization"] != "Bearer mw_new":
            return httpx.Response(401, json={"detail": "Invalid API key"})
        return json_routes(request) or httpx.Response(404, json={"detail": "?"})

    codes: list[DeviceCode] = []

    async def device(on_code) -> str:
        code = DeviceCode("dev", "WXYZ", f"{BASE}/device", None, 600, 5)
        codes.append(code)
        on_code(code)
        return "mw_new"

    holder: dict[str, MatterWaveApp] = {}

    async def refresh() -> str | None:
        return await holder["app"].relogin()

    app = MatterWaveApp(http_api(handler, refresh=refresh), relogin=device)
    holder["app"] = app
    async with app.run_test() as pilot:
        for _ in range(20):
            await pilot.pause()
            if isinstance(app.screen, DeviceLogin):
                break
        await settle(app, pilot)
        assert tokens[:2] == ["Bearer mw_k", "Bearer mw_new"]
        assert all(t == "Bearer mw_new" for t in tokens[1:])
        assert len(codes) == 1
        assert not isinstance(app.screen, DeviceLogin)
        assert app.conversation is not None and app.conversation.id == "c1"
        assert app.failure is None


@pytest.mark.asyncio
async def test_a_notice_shows_its_detail():
    script = happy_script()
    script.insert(1, fr.NoticeFrame("Older messages may be cut.", "compaction"))
    app = MatterWaveApp(FakeAPI(script))
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert shown == ["Older messages may be cut."]
        assert app.failure is None


@pytest.mark.asyncio
async def test_compact_with_nothing_to_fold_is_a_notice_not_a_failure():
    app = MatterWaveApp(FakeAPI())
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "c")
        await settle(app, pilot)
        assert shown == ["Nothing to compact yet."]
        assert app.failure is None


@pytest.mark.asyncio
async def test_compact_folds_the_thread_behind_a_divider():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert len(app.query(MessageView)) == 2
        await pilot.press("escape", "c")
        await settle(app, pilot)
        assert api.compacted == ["c1"]
        assert app.thread.leaf == "s1"
        assert len(app.query(DividerView)) == 1
        assert len(app.query(MessageView)) == 0
        assert app.failure is None
        await pilot.press("x")
        await settle(app, pilot)
        assert len(app.query(MessageView)) == 2


@pytest.mark.asyncio
async def test_a_rate_limit_and_a_plan_refusal_are_their_own_states():
    limited = RateLimitError(
        "Rate limit of 60 requests per minute exceeded for this API key.",
        status_code=429, request_id="req-429", retry_after=30,
    )
    app = MatterWaveApp(FakeAPI(refusal=limited))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert app.failure is not None and app.failure.kind == "rate_limited"
        assert "Too many requests" in banner(app) and "Retry in 30s." in banner(app)
        assert "rate limited · retry in 30s" in status(app)
        assert "Reference: req-429" in banner(app)

    gated = APIError(
        "The Free plan does not include the model 'grt-max'.", status_code=402, request_id="r402"
    )
    app = MatterWaveApp(FakeAPI(refusal=gated))
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert app.failure is not None and app.failure.kind == "plan"
        assert "not on your plan" in banner(app)
        assert "rate limited" not in status(app)


@pytest.mark.asyncio
async def test_the_picked_model_is_checked_then_kept_on_the_conversation():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "m")
        await settle(app, pilot)
        assert isinstance(app.screen, ModelPicker)
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, ModelPicker)

        app.apply_model("other-7b")
        await settle(app, pilot)
        assert api.model_updates == [("c1", "other-7b")]
        assert app.model == "other-7b"
        assert app.conversation is not None and app.conversation.default_model == "other-7b"
        assert "model other-7b" in status(app)

        app.apply_model("grt-max")  # listed, not on the plan
        app.apply_model("nope")  # not listed at all
        await settle(app, pilot)
        assert api.model_updates == [("c1", "other-7b")]
        assert app.model == "other-7b"
        assert len(shown) == 2

        app.query_one("#composer", TextArea).focus()
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert api.sent == [("c1", "hi", "other-7b")]


@pytest.mark.asyncio
async def test_switching_conversations_mid_stream_keeps_the_answer_out_of_the_new_one():
    gate = asyncio.Event()

    class SlowAPI(FakeAPI):
        """A's answer waits after its user turn until B is open, like a slow upstream."""

        async def tree(self, conversation_id: str) -> Conversation:
            self.tree_reads += 1
            conv = self._find(conversation_id)
            if conversation_id == "c2":
                return replace(conv, messages=[message("b1", "user", "B question")])
            return replace(conv, messages=[])

        async def _play(self) -> AsyncIterator[StreamItem]:
            try:
                for i, item in enumerate(self.script):
                    if i == 3:
                        await gate.wait()
                    await asyncio.sleep(0)
                    yield item
            finally:
                self.streams_closed += 1

    api = SlowAPI(happy_script("Answer for A"))
    api.conversations.append(conversation("c2", title="B"))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.conversation is not None and app.conversation.id == "c1"
        await pilot.press("h", "i", "ctrl+s")
        for _ in range(20):
            await pilot.pause()
            if "u1" in app.thread.messages:
                break
        assert app.streaming
        app.open_worker("c2")
        for _ in range(20):
            await pilot.pause()
            if app.conversation is not None and app.conversation.id == "c2":
                break
        gate.set()
        await settle(app, pilot)
        assert app.conversation is not None and app.conversation.id == "c2"
        assert set(app.thread.messages) == {"b1"}
        assert "Answer for A" not in transcript_text(app)
        assert "B question" in transcript_text(app)
        assert not app.streaming
        assert app.live is None
        assert app.failure is None

@pytest.mark.asyncio
async def test_pin_archive_and_the_list_filters_make_them_actions():
    api = FakeAPI()
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "p")
        await settle(app, pilot)
        assert api.pins == [("c1", True)]
        assert sidebar_captions(app) == ["📌 New chat"]
        await pilot.press("p")
        await settle(app, pilot)
        assert api.pins[-1] == ("c1", False)
        assert sidebar_captions(app) == ["New chat"]

        await pilot.press("a")
        await settle(app, pilot)
        assert api.archives == [("c1", True)]
        assert sidebar_captions(app) == []  # the default slice hides the archived
        await pilot.press("o", "o")  # pinned, then archived
        await settle(app, pilot)
        assert app.list_filter == "archived"
        assert sidebar_captions(app) == ["🗄 New chat"]
        await pilot.press("a")
        await settle(app, pilot)
        assert api.archives[-1] == ("c1", False)
        await pilot.press("o", "o")  # all, then back to default
        await settle(app, pilot)
        assert app.list_filter == "default"
        assert sidebar_captions(app) == ["New chat"]


@pytest.mark.asyncio
async def test_rename_asks_for_the_title_then_updates_sidebar_and_conversation():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "f2")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)
        box = app.screen.query_one("#prompt-input")
        assert box.value == "New chat"
        box.value = "Renamed"
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.renames == [("c1", "Renamed")]
        assert app.conversation is not None and app.conversation.title == "Renamed"
        assert sidebar_titles(app) == ["Renamed"]
        assert "Renamed." in shown


@pytest.mark.asyncio
async def test_delete_confirms_then_clears_the_open_conversation():
    api = FakeAPI()
    api.conversations.append(conversation("c2", title="Other"))
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "d")
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        await settle(app, pilot)
        assert api.deletes == []
        await pilot.press("d")
        await settle(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert api.deletes == ["c1"]
        assert app.conversation is None
        assert not app.thread.messages
        assert sidebar_titles(app) == ["Other"]
        assert any("Deleted" in s for s in shown)


@pytest.mark.asyncio
async def test_fork_copies_from_the_selected_message_and_opens_the_copy():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "f")
        await settle(app, pilot)
        assert api.forks == [("c1", "a1")]
        assert app.conversation is not None and app.conversation.id == "c2"
        assert app.conversation.title == "Fork of New chat"
        assert [c.id for c in app.conversations] == ["c2", "c1"]


@pytest.mark.asyncio
async def test_share_hands_back_a_link():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "s")
        await settle(app, pilot)
        assert api.shares == ["c1"]
        assert any("https://matterwave.test/share/tok-c1" in s for s in shown)


@pytest.mark.asyncio
async def test_export_writes_the_active_path_as_markdown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "What is TUI?"),
        message("a1", "assistant", "A terminal UI.", parent="u1", created_at="2", model="grt-nano"),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "w")
        await settle(app, pilot)
        files = list(tmp_path.glob("*.md"))
        assert len(files) == 1
        text = files[0].read_text(encoding="utf-8")
        assert text.startswith("# New chat")
        assert "Exported from MatterWave on " in text
        assert "## You" in text and "What is TUI?" in text
        assert "## Assistant (grt-nano)" in text and "A terminal UI." in text


@pytest.mark.asyncio
async def test_attach_uploads_then_sends_the_ids_with_the_next_turn(tmp_path):
    picture = tmp_path / "picture.png"
    picture.write_bytes(b"PNGDATA")
    api = FakeAPI(happy_script("Seen."))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "i")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)
        box = app.screen.query_one("#prompt-input")
        box.value = str(picture)
        await pilot.press("enter")
        await settle(app, pilot)
        assert [name for name, _ in api.uploads] == ["picture.png"]
        assert "attached: picture.png" in status(app)
        app.query_one("#composer", TextArea).focus()
        await pilot.press("h", "i", "ctrl+s")
        await settle(app, pilot)
        assert api.sent == [("c1", "hi", "grt-nano")]
        assert api.sent_attachment_ids == [["att1"]]
        assert "attached" not in status(app)


@pytest.mark.asyncio
async def test_the_status_line_shows_the_usage_balance():
    app = MatterWaveApp(FakeAPI())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "usage 1,234/100,000 (1%)" in status(app)
        app.usage_balance = replace(app.usage_balance, exhausted=True)
        app.render_status()
        assert "budget used up" in status(app)


@pytest.mark.asyncio
async def test_the_status_line_shows_the_overage_cap():
    plain = MatterWaveApp(FakeAPI())
    async with plain.run_test() as pilot:
        await settle(plain, pilot)
        assert "overage" not in status(plain)

    api = FakeAPI()
    api.budget_payload = {
        "period_start": "2026-10-01", "included_tokens": 100000, "tokens_used": 1234,
        "remaining_tokens": 98766, "overage_enabled": True, "overage_cap_micros": 1000000,
        "overage_micros": 250000,
    }
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "overage $0.25 of $1.00" in status(app)
        api.budget_payload["overage_micros"] = 1000000
        app.load_budget()
        await settle(app, pilot)
        assert "⚠ overage $1.00 of $1.00" in status(app)


@pytest.mark.asyncio
async def test_the_notifications_panel_reads_one_or_all(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(tmp_path))
    api = FakeAPI()
    api.notification_rows = [
        notification("n1"), notification("n2", read_at="2026-10-29T01:00:00Z")
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert "✉ 1 unread" in status(app)
        await pilot.press("escape", "N")
        await settle(app, pilot)
        assert isinstance(app.screen, Notifications)
        head = str(app.screen.query_one("#notifications-head", Static).render())
        assert "1 unread" in head
        await pilot.press("r")
        await settle(app, pilot)
        assert api.notifications_read == ["n1"]
        # Both rows are read now, so the badge is gone from the status line.
        assert "unread" not in status(app)
        await pilot.press("a")
        await settle(app, pilot)
        assert api.notifications_read_all == 1
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, Notifications)


@pytest.mark.asyncio
async def test_the_notifications_panel_pages_and_filters_unread(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(tmp_path))
    api = FakeAPI()
    api.notification_rows = [notification(f"n{i:02d}") for i in range(1, 46)]
    api.notification_rows[10]["read_at"] = "2026-10-29T01:00:00Z"
    api.notification_rows[40]["read_at"] = "2026-10-29T01:00:00Z"
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # The first page only: 30 of the 45 rows, and the badge counts
        # every unread row the server holds, loaded or not.
        assert "✉ 43 unread" in status(app)
        assert len(app.notification_rows) == 30
        await pilot.press("escape", "N")
        await settle(app, pilot)
        panel = app.screen
        assert isinstance(panel, Notifications)
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "29 unread of 30 loaded, page 1 of 1" in head
        # `n` asks for a longer limit and turns to what arrived.
        await pilot.press("n")
        await settle(app, pilot)
        assert len(panel.rows) == 45
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "43 unread of 45 loaded, page 2 of 2" in head
        # At the end of the history, `n` says so and stays.
        await pilot.press("n")
        await settle(app, pilot)
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "page 2 of 2" in head
        # `p` turns back.
        await pilot.press("p")
        await settle(app, pilot)
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "page 1 of 2" in head
        # `u` fetches only the unread ones, and `u` again lets the rest
        # back in; the badge counts the same either way.
        await pilot.press("u")
        await settle(app, pilot)
        assert panel.unread_only
        assert len(panel.rows) == 30
        assert all(not row.get("read_at") for row in panel.rows)
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "30 unread of 30 loaded, page 1 of 1" in head
        await pilot.press("n")
        await settle(app, pilot)
        assert len(panel.rows) == 43
        head = str(panel.query_one("#notifications-head", Static).render())
        assert "43 unread of 43 loaded, page 2 of 2" in head
        await pilot.press("u")
        await settle(app, pilot)
        assert not panel.unread_only
        assert len(panel.rows) == 30
        assert "✉ 43 unread" in status(app)


@pytest.mark.asyncio
async def test_the_library_screen_files_and_tags_and_the_captions_follow():
    api = FakeAPI()
    api.library_folders = [
        {"id": "f1", "name": "Work", "position": 0, "created_at": "1", "updated_at": "1"}
    ]
    api.library_tags = [{"id": "t1", "name": "draft", "created_at": "1"}]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "l")
        await settle(app, pilot)
        screen = app.screen
        assert isinstance(screen, Library)
        rows = screen.query_one("#library-rows", OptionList)

        def labels() -> list[str]:
            return [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]

        assert labels() == ["— Folders —", "  Work", "— Tags —", "  draft"]

        # Onto the folder row and `m`: the chat is filed, the caption
        # names the folder.
        rows.highlighted = 1
        await pilot.press("m")
        await settle(app, pilot)
        assert api.folder_moves == [("c1", "f1")]
        assert app.conversation is not None and app.conversation.folder_id == "f1"
        assert sidebar_captions(app) == ["New chat — 📁 Work"]
        assert "Filed." in shown
        assert labels()[1] == "● Work"

        # Onto the tag row and Space: the tag is set, the caption carries it.
        rows.highlighted = 3
        await pilot.press("space")
        await settle(app, pilot)
        assert api.tag_sets == [("c1", ["t1"])]
        assert sidebar_captions(app) == ["New chat — 📁 Work · #draft"]
        assert labels()[3] == "● draft"
        await pilot.press("space")
        await settle(app, pilot)
        assert api.tag_sets[-1] == ("c1", [])
        assert sidebar_captions(app) == ["New chat — 📁 Work"]

        # A new folder and a new tag land in the list as they are made.
        await pilot.press("f")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)
        box = app.screen.query_one("#prompt-input")
        box.value = "Archive"
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.folders_created == ["Archive"]
        assert isinstance(app.screen, Library)
        assert labels() == [
            "— Folders —", "● Work", "  Archive", "— Tags —", "  draft",
        ]

        # A folder of a taken name is the server's 409, said, not a banner.
        await pilot.press("f")
        await settle(app, pilot)
        box = app.screen.query_one("#prompt-input")
        box.value = "Work"
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.folders_created == ["Archive"]
        assert any("already have one" in s for s in shown)

        await pilot.press("t")
        await settle(app, pilot)
        box = app.screen.query_one("#prompt-input")
        box.value = "spam"
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.tags_created == ["spam"]
        assert labels()[-1] == "  spam"

        # `m` on the folder the chat is already in takes it back out.
        rows.highlighted = 1
        await pilot.press("m")
        await settle(app, pilot)
        assert api.folder_moves[-1] == ("c1", None)
        assert app.conversation is not None and app.conversation.folder_id is None
        assert sidebar_captions(app) == ["New chat"]
        assert "Back in the main list." in shown

        # `m` on a tag row files nothing; it says where to point instead.
        rows.highlighted = 3
        await pilot.press("m")
        await settle(app, pilot)
        assert len(api.folder_moves) == 2
        assert any("Highlight a folder" in s for s in shown)

        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, Library)


@pytest.mark.asyncio
async def test_announcements_show_once_and_stay_dismissed(tmp_path, monkeypatch):
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(tmp_path))
    api = FakeAPI()
    api.announcement_rows = [
        announcement("an1", message="Maintenance tonight.", severity="warning"),
        announcement(
            "an2", message="Breach — rotate your keys.", severity="critical", dismissible=False
        ),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        banner = str(app.query_one("#announcements", Static).render())
        assert "Maintenance tonight." in banner
        assert "Breach — rotate your keys." in banner
        await pilot.press("escape", "A")
        await settle(app, pilot)
        banner = str(app.query_one("#announcements", Static).render())
        assert "Maintenance" not in banner
        # The critical one is the operator's to end, not this device's.
        assert "Breach" in banner
        assert app.check_action("dismiss_announcements", ()) is True

    # The dismissed id survives the restart; the critical notice does not.
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        banner = str(app.query_one("#announcements", Static).render())
        assert "Maintenance" not in banner
        assert "Breach" in banner


@pytest.mark.asyncio
async def test_a_report_asks_the_kind_a_note_and_a_confirm():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # An unknown kind is refused; nothing is filed.
        await pilot.press("escape", "R")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)
        await pilot.press(*"maybe", "enter")
        await settle(app, pilot)
        assert api.reports == []
        assert any("unsafe or unhelpful" in s for s in shown)

        # A kind and a note, then a confirm that is declined.
        await pilot.press("escape", "R")
        await settle(app, pilot)
        await pilot.press(*"unhelpful", "enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)
        await pilot.press(*"spam", "enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        await settle(app, pilot)
        assert api.reports == []

        # The same again, confirmed; the note is optional.
        await pilot.press("escape", "R")
        await settle(app, pilot)
        await pilot.press(*"unhelpful", "enter")
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        await pilot.press("y")
        await settle(app, pilot)
        assert api.reports == [("c1", "unhelpful", None, None)]
        assert any("filed" in s for s in shown)


@pytest.mark.asyncio
async def test_my_shares_lists_and_revokes():
    api = FakeAPI()
    api.share_rows = [share_row("s1", title="New chat", view_count=3)]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "S")
        await settle(app, pilot)
        assert isinstance(app.screen, Shares)
        head = str(app.screen.query_one("#shares-head", Static).render())
        assert "1 live" in head
        await pilot.press("x")
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press("y")
        await settle(app, pilot)
        assert api.shares_revoked == ["s1"]
        assert "Share revoked." in shown
        # The row stays, marked revoked, and x on it does nothing further.
        await pilot.press("x")
        await settle(app, pilot)
        assert api.shares_revoked == ["s1"]
        assert any("not live" in s for s in shown)


@pytest.mark.asyncio
async def test_a_new_chat_offers_the_temporary_toggle_before_anything_is_sent():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # Esc first: a regular chat, as before.
        await pilot.press("escape", "n")
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press("n")
        await settle(app, pilot)
        assert api.creates == [(None, None, False)]
        assert app.conversation is not None and app.conversation.id == "c2"
        assert not app.temporary_chat
        assert not isinstance(app.screen, Confirm)

        # Enter this time: a temporary chat, created with the flag on.
        await pilot.press("escape", "n")
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.creates[-1] == (None, None, True)
        assert app.temporary_chat
        assert TEMPORARY_NOTICE in str(app.query_one("#temporary", Static).render())
        # The server keeps it out of every list, so the sidebar has neither.
        assert [c.id for c in app.conversations] == ["c1"]
        # Share and fork are not commands while the temporary chat is open.
        assert app.check_action("share", ()) is False
        assert app.check_action("fork", ()) is False
        await pilot.press("s")
        await settle(app, pilot)
        await pilot.press("f")
        await settle(app, pilot)
        assert api.shares == [] and api.forks == []
        # Pin reaches the server and its 409 detail is the message.
        await pilot.press("p")
        await settle(app, pilot)
        assert any("cannot be pinned" in s for s in shown)
        assert app.failure is None

        # Opening another conversation ends the temporary state.
        await app.open_conversation("c1")
        await settle(app, pilot)
        assert not app.temporary_chat
        assert app.check_action("share", ()) is True


@pytest.mark.asyncio
async def test_the_instructions_editor_saves_the_thread_prompt():
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "t")
        await settle(app, pilot)
        editor = app.screen
        assert isinstance(editor, InstructionsEditor)
        area = editor.query_one("#instructions", TextArea)
        assert area.text == ""
        counter = editor.query_one("#instructions-count", Static)
        assert "0 / 2000" in str(counter.render())
        # The effective instructions from the conversation GET are the reference.
        assert "Be brief." in str(app.screen.query(Collapsible).first().query_one(Static).render())
        await pilot.press(*"Be terse.")
        assert "9 / 2000" in str(counter.render())
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert api.instructions_puts == [("c1", "Be terse.")]
        assert "Instructions saved." in shown
        assert not isinstance(app.screen, InstructionsEditor)

        # The editor opens on the text as the server now holds it.
        await pilot.press("escape", "t")
        await settle(app, pilot)
        editor = app.screen
        assert isinstance(editor, InstructionsEditor)
        assert editor.query_one("#instructions", TextArea).text == "Be terse."


@pytest.mark.asyncio
async def test_thumbs_rate_and_withdraw_a_reply():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        views = app.query(MessageView)
        assert len(views) == 2
        # Only the assistant reply carries thumbs.
        assert not views.first().query("#thumb-up")
        up = views.last().query_one("#thumb-up")
        await pilot.click("#thumb-up")
        await settle(app, pilot)
        assert api.feedback_puts == [("a1", 1, [], None)]
        assert up.has_class("active")
        assert "Feedback sent." in shown
        # The same thumb again withdraws.
        await pilot.click("#thumb-up")
        await settle(app, pilot)
        assert api.feedback_deletes == ["a1"]
        assert not up.has_class("active")
        assert "Feedback withdrawn." in shown


@pytest.mark.asyncio
async def test_feedback_is_restored_on_open_and_the_dialog_edits_reasons():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
    ]
    api.feedback_rows = [
        {"message_id": "a1", "rating": -1, "reasons": ["wrong"], "comment": "no"}
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # Rehydrated from GET /api/conversations/c1/feedback.
        assert app.query(MessageView).last().query_one("#thumb-down").has_class("active")

        await pilot.press("escape", "b")
        await settle(app, pilot)
        dialog = app.screen
        assert isinstance(dialog, FeedbackDialog)
        assert dialog.query_one("#feedback-reasons", Input).value == "wrong"
        assert dialog.query_one("#feedback-comment", TextArea).text == "no"
        # An unknown reason is refused in place; the dialog stays open.
        reasons = dialog.query_one("#feedback-reasons", Input)
        reasons.value = "wrong, nonsense"
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert isinstance(app.screen, FeedbackDialog)
        assert any("nonsense" in s for s in shown)
        reasons.value = "wrong, unhelpful"
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert api.feedback_puts == [("a1", -1, ["wrong", "unhelpful"], "no")]
        assert "Feedback sent." in shown
        assert not isinstance(app.screen, FeedbackDialog)


@pytest.mark.asyncio
async def test_an_image_is_refused_when_the_model_cannot_see_and_a_document_is_not(
    tmp_path, monkeypatch
):
    api = FakeAPI()
    api.models_payload = [
        {"id": "grt-nano", "label": "GRT Nano", "supports_vision": False},
        {"id": "grt-see", "label": "GRT See", "supports_vision": True},
        {"id": "grt-too", "label": "GRT Too", "supports_vision": True},
        {"id": "grt-three", "label": "GRT Three", "supports_vision": True},
        {"id": "grt-four", "label": "GRT Four", "supports_vision": True},
        {"id": "mystery", "label": "Mystery"},
    ]
    monkeypatch.chdir(tmp_path)
    picture = tmp_path / "picture.png"
    picture.write_bytes(b"PNGDATA")
    (tmp_path / "notes.txt").write_bytes(b"notes")
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)

        def attach(name: str) -> None:
            app.apply_attach(name)

        attach("picture.png")
        await settle(app, pilot)
        assert api.uploads == []
        message = shown[-1]
        assert "grt-nano cannot read images" in message
        # Up to three vision-capable ids, and no more.
        assert "grt-see, grt-too, grt-three" in message
        assert "grt-four" not in message

        # A model the list does not know, and a non-image, both go through.
        app.model = "mystery"
        attach("notes.txt")
        await settle(app, pilot)
        assert [name for name, _ in api.uploads] == ["notes.txt"]


@pytest.mark.asyncio
async def test_the_feedback_dialog_carries_the_selected_replys_id():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
        message("a2", "assistant", "two", parent="a1", created_at="3"),
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # Select the first reply, not the last: the dialog is opened on it
        # and the rating it saves must be that reply's, by id.
        await pilot.press("escape", "up", "up", "b")
        await settle(app, pilot)
        dialog = app.screen
        assert isinstance(dialog, FeedbackDialog)
        assert dialog.message_id == "a1"
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert api.feedback_puts == [("a1", -1, [], None)]
        assert "Feedback sent." in shown


@pytest.mark.asyncio
async def test_a_report_targets_the_selected_reply_or_the_conversation():
    api = FakeAPI()
    api.tree_messages = [
        message("u1", "user", "q"),
        message("a1", "assistant", "one", parent="u1", created_at="2"),
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # With a reply selected, the report is about that reply.
        await pilot.press("escape", "up", "R")
        await settle(app, pilot)
        await pilot.press(*"unsafe", "enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Prompt)  # the note, then the confirm
        await pilot.press("enter")
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        texts = [str(node.render()) for node in app.screen.query(Static)]
        assert any("the selected reply" in text for text in texts)
        await pilot.press("y")
        await settle(app, pilot)
        assert api.reports == [("c1", "unsafe", "a1", None)]
        assert any("Report rep1 filed" in s for s in shown)

        # With nothing selected, it is the conversation as a whole, and
        # the body carries no message id at all.
        app.query_one("#transcript", Transcript).select(None)
        api.reports.clear()
        await pilot.press("escape", "R")
        await settle(app, pilot)
        await pilot.press(*"unhelpful", "enter")
        await settle(app, pilot)
        await pilot.press("enter", "y")
        await settle(app, pilot)
        assert api.reports == [("c1", "unhelpful", None, None)]


@pytest.mark.asyncio
async def test_the_account_switcher_offers_the_parked_and_reloads():
    api = FakeAPI()
    api.account_rows = [
        {"slot": 0, "current": True, "subject": "a", "email": "a@x.test",
         "name": "A", "org_name": "Work"},
        {"slot": 1, "current": False, "subject": "b", "email": "b@x.test",
         "name": "B", "org_name": "Home"},
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.action_switch_account()
        await settle(app, pilot)
        switcher = app.screen
        assert isinstance(switcher, Accounts)
        options = switcher.query_one("#account-rows", OptionList)
        # The acting account is marked, not offered; the parked one is.
        assert options.get_option_at_index(0).disabled
        assert not options.get_option_at_index(1).disabled
        reads_before = api.tree_reads
        options.highlighted = 1
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.switches == [1]
        assert any("Switched to b@x.test." in s for s in shown)
        # Everything the old account had open was dropped and read again
        # from the account that is acting now.
        assert not isinstance(app.screen, Accounts)
        assert api.tree_reads > reads_before

    # The rows the fake holds swapped slots, as the server's swap would.
    assert [row["slot"] for row in api.account_rows] == [1, 0]
    assert api.account_rows[0]["current"] is False


@pytest.mark.asyncio
async def test_the_switcher_says_when_nothing_is_parked():
    api = FakeAPI()
    api.account_rows = [
        {"slot": 0, "current": True, "subject": "a", "email": "a@x.test",
         "name": "A", "org_name": "Work"},
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.action_switch_account()
        await settle(app, pilot)
        assert any("No other account is signed in" in s for s in shown)
        assert not isinstance(app.screen, Accounts)


@pytest.mark.asyncio
async def test_an_import_is_uploaded_watched_and_summarised(tmp_path, monkeypatch):
    monkeypatch.setattr("matterwave_tui.app.IMPORT_POLL_S", 0.01)
    api = FakeAPI()
    api.import_done_after = 1  # the first poll finds the job done
    export = tmp_path / "chats.json"
    export.write_bytes(b"EXPORT")
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.apply_import(str(export))
        await settle(app, pilot)
        assert api.imports_started == [("chats.json", b"EXPORT")]
        assert api.import_status_calls == ["imp1"]
        assert any("Import starting" in s for s in shown)
        assert any("Imported 2 conversations, 9 messages from ChatGPT" in s for s in shown)

        # An oversized export is refused before the upload would go.
        api.imports_started.clear()
        big = tmp_path / "big.json"
        big.write_bytes(b"x" * (IMPORT_MAX_BYTES + 1))
        app.apply_import(str(big))
        await settle(app, pilot)
        assert api.imports_started == []
        assert any("100 MB limit" in s for s in shown)

        # A job that fails names the error it reported.
        api.import_done_after = None
        api.import_rows = [{"id": "imp2", "status": "failed",
                            "error": "This export holds no conversations."}]
        app._watch_import("imp2")
        await settle(app, pilot)
        assert any("Import failed: This export holds no conversations." in s for s in shown)


@pytest.mark.asyncio
async def test_dictation_without_the_microphone_extra_transcribes_a_file(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("matterwave_tui.app.mic_available", lambda: False)
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "v")
        await settle(app, pilot)
        # The extra is missing: the fallback says so and asks for a file.
        assert any("matterwave-tui[dictation]" in s for s in shown)
        assert isinstance(app.screen, Prompt)
        (tmp_path / "clip.wav").write_bytes(b"CLIPBYTES")
        await pilot.press(*"clip.wav", "enter")
        await settle(app, pilot)
        assert api.transcribes == [("clip.wav", "audio/wav", None, b"CLIPBYTES")]
        # The transcript is text in the composer, nothing sent.
        assert "hello there" in app.query_one("#composer", TextArea).text
        assert api.sent == []

        # A suffix the server refuses is caught before an upload.
        await pilot.press("escape", "v")
        await settle(app, pilot)
        (tmp_path / "notes.txt").write_bytes(b"words")
        await pilot.press(*"notes.txt", "enter")
        await settle(app, pilot)
        assert api.transcribes == [("clip.wav", "audio/wav", None, b"CLIPBYTES")]
        assert any("Send an audio clip" in s for s in shown)


@pytest.mark.asyncio
async def test_dictation_with_the_microphone_posts_the_clip(monkeypatch):
    monkeypatch.setattr("matterwave_tui.app.mic_available", lambda: True)
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "v")
        await settle(app, pilot)
        recorder_screen = app.screen
        assert isinstance(recorder_screen, Dictation)

        # The microphone itself is stubbed: enter "stops" it with a clip,
        # which goes to the route and lands as text in the composer.
        class FakeRecorder:
            def stop(self) -> bytes:
                return b"RIFF"

            def discard(self) -> None:
                return None

        recorder_screen.recorder = FakeRecorder()
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.transcribes[-1][:3] == ("dictation.wav", "audio/wav", None)
        assert "hello there" in app.query_one("#composer", TextArea).text
        assert not isinstance(app.screen, Dictation)

        # An empty clip is said in passing, not uploaded.
        await pilot.press("escape", "v")
        await settle(app, pilot)
        empty_screen = app.screen
        assert isinstance(empty_screen, Dictation)
        empty_screen.recorder = FakeRecorder()
        empty_screen.recorder.stop = lambda: b""  # type: ignore[method-assign]
        await pilot.press("enter")
        await settle(app, pilot)
        assert any("Nothing was captured" in s for s in shown)
        assert len(api.transcribes) == 1


@pytest.mark.asyncio
async def test_an_image_part_is_loaded_when_the_terminal_can_draw_it(monkeypatch):
    monkeypatch.setenv("MATTERWAVE_IMAGES", "kitty")
    api = FakeAPI()
    png = b"\x89PNG\r\n\x1a\n" + b"pixels"
    api.attachments_store["att1"] = png
    api.tree_messages = [
        message("u1", "user", "q"),
        message(
            "a1", "assistant", "look", parent="u1", created_at="2",
            parts=[{"type": "image", "attachment_id": "att1", "filename": "pic.png",
                    "content_type": "image/png"}],
        ),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.image_backend == "kitty"
        view = app.query(ImageView).first()
        assert view.data == png
        assert api.downloaded == ["att1"]
        # The bytes in hand, the layer queues the draw (transmit, place),
        # and the placement it keeps knows how to take the picture down.
        # A periodic refresh may already have placed it and flushed the
        # queue with the display, so start the layer over to see the
        # sequences a placement queues.
        app._image_placements.clear()
        app._refresh_images()
        placement = next(iter(app._image_placements.values()))
        assert any("a=T,f=100" in op for op in app._image_ops)
        assert "a=d,d=i" in placement.remove

        # A transcript that redraws keeps the loaded bytes; the part does
        # not download twice.
        await app.render_thread()
        await settle(app, pilot)
        assert api.downloaded == ["att1"]


@pytest.mark.asyncio
async def test_an_image_part_stays_a_placeholder_without_a_backend(monkeypatch):
    monkeypatch.setenv("MATTERWAVE_IMAGES", "off")
    api = FakeAPI()
    api.attachments_store["att1"] = b"\x89PNG\r\n\x1a\npixels"
    api.tree_messages = [
        message(
            "u1", "user", "q",
            parts=[{"type": "image", "attachment_id": "att1", "filename": "pic.png",
                    "content_type": "image/png"}],
        ),
    ]
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.image_backend is None
        view = app.query(ImageView).first()
        assert str(view.render()) == "[image: pic.png]"
        # Nothing is fetched when there is nothing to draw it on.
        assert api.downloaded == []

        # The download failing is a word on the placeholder, once. The view
        # learned its backend from the app at compose time; pointing the
        # app at kitty after the fact points the view too.
        app.image_backend = "kitty"
        view.backend = "kitty"
        api.attachments_store.clear()
        app.load_images()
        await settle(app, pilot)
        assert view.failed
        assert "could not be loaded" in str(view.render())


@pytest.mark.asyncio
async def test_the_command_palette_offers_the_tui_commands():
    api = FakeAPI()
    api.account_rows = [
        {"slot": 0, "current": True, "subject": "a", "email": "a@x.test", "name": "A"},
    ]
    app = MatterWaveApp(api)
    shown = capture_notify(app)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        provider = TuiCommands(app.screen)
        names = [hit.display async for hit in provider.discover()]
        assert any("Switch account" in str(name) for name in names)
        assert any("Import" in str(name) for name in names)
        assert any("Dictate" in str(name) for name in names)
        hits = [hit async for hit in provider.search("switch")]
        assert hits and callable(hits[0].command)
        hits[0].command()
        await settle(app, pilot)
        # With nothing parked, the switcher says so rather than showing an
        # empty screen.
        assert any("No other account is signed in" in s for s in shown)
