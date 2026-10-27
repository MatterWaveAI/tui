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
from textual.widgets import ListView, Markdown, Static, TextArea

from matterwave_tui.api import Cut, HttpChatAPI, Opened, StreamItem
from matterwave_tui.app import MatterWaveApp
from matterwave_tui.auth import DeviceCode
from matterwave_tui.screens import Confirm, DeviceLogin, ModelPicker, Prompt
from matterwave_tui.widgets import DividerView, MessageView

from .fake_api import FakeAPI, conversation, happy_script, message

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
    if path == "/api/me/usage":
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
        assert "## You" in text and "What is TUI?" in text
        assert "## Assistant · grt-nano" in text and "A terminal UI." in text


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
