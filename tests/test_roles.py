"""The prompt library: roles are added, persisted to the state directory,
and applied to the open conversation as its instructions (the same PUT the
instructions editor uses)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from textual.widgets import OptionList, Static, TextArea

from matterwave_tui.app import MatterWaveApp
from matterwave_tui.roles import load_roles, save_roles
from matterwave_tui.screens import Confirm, Prompt, RoleEditor, Roles

from .fake_api import FakeAPI, happy_script


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "state"
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(root))
    return root


def role_file(state_dir: Path) -> Path:
    return state_dir / "matterwave-tui" / "roles.json"


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def type_command(app: MatterWaveApp, pilot, command: str) -> None:
    app.query_one("#composer", TextArea).text = command


async def send_command(app: MatterWaveApp, pilot, command: str) -> None:
    type_command(app, pilot, command)
    app.action_send()
    await pilot.pause()


def test_roles_round_trip_and_junk(tmp_path: Path):
    path = tmp_path / "roles.json"
    assert load_roles(path) == {}
    assert save_roles(path, {"terse": "Be terse."})
    assert load_roles(path) == {"terse": "Be terse."}
    path.write_text("{not json", encoding="utf-8")
    assert load_roles(path) == {}
    path.write_text('["a list"]', encoding="utf-8")
    assert load_roles(path) == {}


def test_save_roles_reports_a_refused_write(tmp_path: Path):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory should be", encoding="utf-8")
    blocked = blocker / "roles.json"  # inside a file: mkdir and write both fail
    assert save_roles(blocked, {"x": "y"}) is False


@pytest.mark.asyncio
async def test_a_role_is_added_applied_and_persisted(state_dir):
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        # Add: the name, then the system text.
        await send_command(app, pilot, "/role --add terse")
        await pilot.pause()
        assert isinstance(app.screen, RoleEditor)
        await pilot.press(*"Be terse.")
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert app.roles == {"terse": "Be terse."}
        assert json.loads(role_file(state_dir).read_text(encoding="utf-8")) == {
            "terse": "Be terse."
        }
        assert "Role terse saved." in shown

        # Apply: the thread instructions PUT carries the role's text.
        await send_command(app, pilot, "/role terse")
        await settle(app, pilot)
        assert api.instructions_puts == [("c1", "Be terse.")]
        assert "Role terse applied." in shown
        # The composer is clear; nothing was sent as a turn.
        assert api.sent == []
        assert app.query_one("#composer", TextArea).text == ""

        # A fresh app reads the same library from the state directory.
        reopened = MatterWaveApp(api)
        assert reopened.roles == {"terse": "Be terse."}


@pytest.mark.asyncio
async def test_role_commands_are_refused_cleanly(state_dir):
    api = FakeAPI()
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await send_command(app, pilot, "/role nope")
        await settle(app, pilot)
        assert "No role named nope." in shown
        assert api.instructions_puts == []

        await send_command(app, pilot, "/nope")
        await settle(app, pilot)
        assert any("Unknown command" in s for s in shown)
        assert api.sent == []


@pytest.mark.asyncio
async def test_an_unknown_command_keeps_the_text_and_says_how_to_send_it(state_dir):
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "/etc/hosts is empty, why?"
        app.action_send()
        await settle(app, pilot)
        assert composer.text == "/etc/hosts is empty, why?"
        assert api.sent == []
        assert any("Start with // to send" in s for s in shown)


@pytest.mark.asyncio
async def test_a_double_slash_sends_the_text_with_one_slash(state_dir):
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "//etc/hosts is empty, why?"
        app.action_send()
        await settle(app, pilot)
        assert api.sent == [("c1", "/etc/hosts is empty, why?", "grt-nano")]
        assert composer.text == ""
        # The escape survives the queue: a `//` typed while an answer
        # streams still goes with a single slash.
        api.gate = asyncio.Event()
        composer.text = "first"
        app.action_send()
        await pilot.pause()
        assert app.streaming
        composer.text = "//role is not a command here"
        app.action_send()
        assert app.queued_prompt == "//role is not a command here"
        api.gate.set()
        await settle(app, pilot)
        await settle(app, pilot)
        assert [content for _, content, _ in api.sent] == [
            "/etc/hosts is empty, why?",
            "first",
            "/role is not a command here",
        ]


@pytest.mark.asyncio
async def test_the_roles_screen_applies_adds_and_deletes(state_dir):
    api = FakeAPI()
    app = MatterWaveApp(api)
    app.roles = {"terse": "Be terse.", "friendly": "Be friendly."}
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "L")
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, Roles)
        rows = screen.query_one("#role-rows", OptionList)
        # Sorted by name: friendly first.
        assert rows.get_option_at_index(0).id == "role-0"
        assert "2 saved" in str(screen.query_one("#roles-head", Static).render())

        # Enter applies the highlighted role.
        await pilot.press("enter")
        await settle(app, pilot)
        assert api.instructions_puts == [("c1", "Be friendly.")]
        assert "Role friendly applied." in shown

        # `a` makes one: the name, then the text.
        await pilot.press("escape", "L")
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert isinstance(app.screen, Prompt)
        await pilot.press(*"polite")
        await pilot.press("enter")
        await pilot.pause()
        editor = app.screen
        assert isinstance(editor, RoleEditor)
        await pilot.press(*"Say please.")
        await pilot.press("ctrl+t")
        await settle(app, pilot)
        assert app.roles["polite"] == "Say please."
        assert load_roles(role_file(state_dir))["polite"] == "Say please."

        # `d` deletes after a confirm.
        screen = app.screen if isinstance(app.screen, Roles) else None
        assert screen is not None
        rows = screen.query_one("#role-rows", OptionList)
        assert rows.option_count == 3
        # Highlight "terse" (index 2 in sorted order).
        rows.highlighted = 2
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, Confirm)
        await pilot.press("enter")
        await settle(app, pilot)
        assert "terse" not in app.roles
        assert load_roles(role_file(state_dir)) == {
            "friendly": "Be friendly.",
            "polite": "Say please.",
        }
