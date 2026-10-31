"""Prompt queueing: typing while an answer streams queues the prompt, and
it sends itself when the turn is done. Esc cancels the stream and clears
the queue, with the text back in the composer."""

from __future__ import annotations

import asyncio

import pytest
from textual.widgets import Static, TextArea

from matterwave_tui.app import MatterWaveApp

from .fake_api import FakeAPI, conversation, happy_script


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def status(app: MatterWaveApp) -> str:
    return str(app.query_one("#status", Static).render())


@pytest.mark.asyncio
async def test_a_prompt_during_a_stream_queues_and_sends_on_done():
    gate = asyncio.Event()
    api = FakeAPI(happy_script("First answer"), gate=gate)
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "first question"
        app.action_send()
        # The gate holds the turn open.
        for _ in range(10):
            await pilot.pause()
        assert app.streaming
        assert api.sent == [("c1", "first question", "grt-nano")]

        # A second send queues instead of going.
        composer.text = "second question"
        app.action_send()
        await pilot.pause()
        assert app.queued_prompt == "second question"
        assert composer.text == ""
        assert api.sent == [("c1", "first question", "grt-nano")]
        assert any("Queued" in s for s in shown)
        assert "queued" in status(app)

        # The answer finishes; the queued prompt sends itself.
        gate.set()
        await settle(app, pilot)
        await settle(app, pilot)
        assert api.sent == [
            ("c1", "first question", "grt-nano"),
            ("c1", "second question", "grt-nano"),
        ]
        assert app.queued_prompt is None
        assert not app.streaming
        assert composer.text == ""


@pytest.mark.asyncio
async def test_esc_cancels_the_stream_and_clears_the_queue():
    gate = asyncio.Event()
    api = FakeAPI(happy_script("First answer"), gate=gate)
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "first question"
        app.action_send()
        for _ in range(10):
            await pilot.pause()
        composer.text = "second question"
        app.action_send()
        await pilot.pause()
        assert app.queued_prompt == "second question"

        # Esc: the stream stops and the queue clears, text kept.
        await pilot.press("escape")
        await settle(app, pilot)
        assert not app.streaming
        assert app.queued_prompt is None
        assert composer.text == "second question"
        assert any("Stopped." in s for s in shown)
        # Nothing of the second question was ever sent.
        assert api.sent == [("c1", "first question", "grt-nano")]


@pytest.mark.asyncio
async def test_a_queued_prompt_survives_a_conversation_switch():
    gate = asyncio.Event()
    api = FakeAPI(happy_script("First answer"), gate=gate)
    api.conversations.append(conversation("c2", title="Other"))
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "first question"
        app.action_send()
        for _ in range(10):
            await pilot.pause()
        composer.text = "second question"
        app.action_send()
        await pilot.pause()
        assert app.queued_prompt == "second question"

        # Switching away mid-answer detaches the turn; the queued text is
        # the old conversation's draft.
        await app.open_conversation("c2")
        await settle(app, pilot)
        assert app.queued_prompt is None
        assert app.drafts.get("c1") == "second question"

        # Back: the draft is in the composer, unqueued (the stream it
        # belonged to is gone; sending is the person's choice).
        await app.open_conversation("c1")
        await settle(app, pilot)
        assert composer.text == "second question"
        assert app.queued_prompt is None
