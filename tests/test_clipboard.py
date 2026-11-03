"""Clipboard copy: `y` copies the selected message or the last answer,
`Y` copies just its fenced code. In the test driver the write lands in
`app.clipboard`, which is what a terminal's OSC 52 would carry."""

from __future__ import annotations

import pytest
from matterwave_sdk import frames as fr
from textual.widgets import TextArea

from matterwave_tui.app import MatterWaveApp
from matterwave_tui.widgets import code_blocks

from .fake_api import FakeAPI, happy_script, message


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    while list(app.workers):
        await app.workers.wait_for_complete()
        await pilot.pause()


def send_and_settle(app: MatterWaveApp, pilot, prompt: str = "hi") -> None:
    app.query_one("#composer", TextArea).text = prompt
    app.action_send()


@pytest.mark.asyncio
async def test_y_copies_the_last_answer():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        send_and_settle(app, pilot)
        await settle(app, pilot)
        # Esc moves the focus off the composer, where y is a key and not
        # a character.
        await pilot.press("escape", "y")
        await settle(app, pilot)
        assert app.clipboard == "Hello there"
        assert "Copied." in shown


@pytest.mark.asyncio
async def test_a_selected_message_is_what_y_copies():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        send_and_settle(app, pilot)
        await settle(app, pilot)
        await pilot.press("escape")
        # Up into the transcript's selection: the last message, then the
        # user's turn before it.
        await pilot.press("up", "up", "y")
        await settle(app, pilot)
        assert app.clipboard == "hi"


@pytest.mark.asyncio
async def test_y_copies_the_code_blocks_only():
    answer = message(
        "a1",
        "assistant",
        "Run these:\n\n```bash\ngit status\n```\n\n```python\nprint(1)\n```",
        parent="u1",
        model="grt-nano",
    )
    api = FakeAPI()
    api.script = happy_script()
    api.script[-1] = fr.DoneFrame(answer)
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        send_and_settle(app, pilot)
        await settle(app, pilot)
        await pilot.press("escape", "Y")
        await settle(app, pilot)
        assert app.clipboard == "git status\n\nprint(1)"
        assert "Code copied." in shown


@pytest.mark.asyncio
async def test_copy_code_without_code_says_so():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        send_and_settle(app, pilot)
        await settle(app, pilot)
        await pilot.press("escape", "Y")
        await settle(app, pilot)
        assert any("No code blocks" in s for s in shown)
        assert app.clipboard == ""


def test_code_blocks_extracts_contents():
    text = "before\n```bash\necho one\n```\nmiddle\n```\nplain\n```\nafter"
    assert code_blocks(text) == ["echo one", "plain"]
    # An unclosed fence runs to the end.
    assert code_blocks("start\n```\ntail") == ["tail"]
    assert code_blocks("no code here") == []
