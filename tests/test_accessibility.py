"""Marks a screen reader can say: each symbol that stands alone carries its
word, `--plain` draws the words alone, and NO_COLOR is honoured."""

from __future__ import annotations

import argparse

import pytest
from matterwave_sdk import frames as fr
from textual.widgets import ListView, Static, TextArea

from matterwave_tui import cli, markers
from matterwave_tui.app import MatterWaveApp

from .fake_api import FakeAPI, announcement, conversation, happy_script, notification


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def test_each_mark_has_its_word_and_plain_has_only_the_word():
    assert markers.badge("pinned") == "📌 pinned"
    assert markers.lead("folder") == "📁 "
    assert markers.icon("unread") == "✉ "
    markers.configure(True)
    assert markers.badge("pinned") == "pinned"
    assert markers.lead("folder") == "folder: "
    assert markers.icon("unread") == ""


@pytest.mark.parametrize("plain", [False, True])
@pytest.mark.asyncio
async def test_the_screen_says_its_marks_in_words(plain):
    api = FakeAPI(happy_script())
    api.conversations = [conversation("c1", pinned_at="2026-10-27T00:00:00Z")]
    api.notification_rows = [notification("n1")]
    api.announcement_rows = [announcement("an1", severity="critical", message="Down at 22:00.")]
    app = MatterWaveApp(api, plain=plain)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        caption = app.query_one("#conversations", ListView).children[0].caption()
        status = str(app.query_one("#status", Static).render())
        banner = str(app.query_one("#announcements", Static).render())
        app.query_one("#composer", TextArea).text = "hi"
        app.action_send()
        await settle(app, pilot)
        thumbs = [str(t.render()) for t in app.query(".thumb")]
    if plain:
        assert caption == "pinned · New chat"
        assert "1 unread" in status and "✉" not in status
        assert banner.startswith("critical: Down at 22:00.")
        assert thumbs == ["good", "bad"]
    else:
        assert caption == "📌 pinned · New chat"
        assert "✉ 1 unread" in status
        assert banner.startswith("⛔ critical: Down at 22:00.")
        assert thumbs == ["👍 good", "👎 bad"]


def test_a_tool_line_says_what_it_is_in_plain():
    from matterwave_tui.widgets import tool_call_line, tool_result_line

    markers.configure(True)
    assert tool_call_line({"name": "search", "arguments": '{"q": "x"}'}) == 'tool: search(q="x")'
    assert tool_result_line({"output": "boom", "is_error": True}) == "error: boom"
    assert tool_result_line({"output": "ok"}) == "result: ok"


def test_plain_reaches_the_app_and_turns_colour_off(monkeypatch):
    handed: dict[str, object] = {}
    monkeypatch.setattr(cli, "run_app", lambda base_url, credential, **kw: handed.update(kw))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    assert cli.main(["--base-url", "https://matterwave.test", "--key", "mw_k", "--plain"]) == 0
    assert handed["plain"] is True


def test_run_app_with_plain_sets_no_color_before_textual_reads_it(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "")
    monkeypatch.delenv("NO_COLOR")
    seen: dict[str, bool] = {}

    def run(self: MatterWaveApp) -> None:
        seen["no_color"] = self.no_color

    monkeypatch.setattr(MatterWaveApp, "run", run)
    cli.run_app("https://matterwave.test", cli.Credential("mw_k", "flag"), plain=True)
    assert seen == {"no_color": True}


@pytest.mark.asyncio
async def test_no_color_is_honoured(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    app = MatterWaveApp(FakeAPI([fr.DoneFrame(happy_script()[-1].message)]))
    assert app.no_color
