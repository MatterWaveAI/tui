"""Composing and getting about: the composer in a real editor, the help
generated from the bindings, keys overridden from the config file, the
theme remembered, and several files attached at once."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from textual.widgets import DirectoryTree, OptionList, TextArea

from matterwave_tui.app import MatterWaveApp, editor_command, expand_paths
from matterwave_tui.screens import FilePicker, HelpScreen, NewChat

from .fake_api import FakeAPI


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    while list(app.workers):
        await app.workers.wait_for_complete()
        await pilot.pause()


def test_the_editor_is_visual_then_editor_then_the_platforms_own(monkeypatch):
    assert editor_command({"VISUAL": "code --wait", "EDITOR": "vim"}) == ["code", "--wait"]
    assert editor_command({"EDITOR": "nano"}) == ["nano"]
    monkeypatch.setattr("matterwave_tui.app.os.name", "posix")
    assert editor_command({}) == ["vi"]


@pytest.mark.asyncio
async def test_ctrl_e_edits_the_composer_in_the_editor():
    app = MatterWaveApp(FakeAPI())
    ran: list[list[str]] = []

    def editor(argv: list[str]) -> int:
        ran.append(argv)
        path = Path(argv[-1])
        assert path.read_text(encoding="utf-8") == "a draft"
        path.write_text("a draft, rewritten\nover two lines\n", encoding="utf-8")
        return 0

    app._run_editor = editor
    async with app.run_test() as pilot:
        await settle(app, pilot)
        composer = app.query_one("#composer", TextArea)
        composer.text = "a draft"
        composer.focus()
        await pilot.press("ctrl+e")
        await settle(app, pilot)
        assert composer.text == "a draft, rewritten\nover two lines"
        # The draft file is gone once the text is back.
        assert len(ran) == 1
        assert not await asyncio.to_thread(Path(ran[0][-1]).exists)

        # An editor that fails leaves the composer alone.
        app._run_editor = lambda argv: 1
        await pilot.press("ctrl+e")
        await settle(app, pilot)
        assert composer.text == "a draft, rewritten\nover two lines"


@pytest.mark.asyncio
async def test_f1_lists_every_key_with_the_config_files_overrides():
    app = MatterWaveApp(FakeAPI(), keymap={"new": "ctrl+n"})
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "f1")
        await settle(app, pilot)
        assert isinstance(app.screen, HelpScreen)
        rows = app.screen.query_one("#help-rows", OptionList)
        lines = [str(rows.get_option_at_index(i).prompt) for i in range(rows.option_count)]
        assert any("Editor" in line and "[editor]" in line for line in lines)
        assert any("Pick files" in line for line in lines)
        new = next(line for line in lines if "[new]" in line)
        assert "^n" in new.lower() or "ctrl+n" in new.lower()
        await pilot.press("escape")
        await settle(app, pilot)
        # The override works, and the old key no longer does.
        await pilot.press("ctrl+n")
        await settle(app, pilot)
        assert isinstance(app.screen, NewChat)


@pytest.mark.asyncio
async def test_an_override_for_no_key_is_said():
    app = MatterWaveApp(FakeAPI(), keymap={"launch_rockets": "z"})
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert any("launch_rockets" in s for s in shown)


@pytest.mark.asyncio
async def test_the_theme_chosen_is_the_theme_next_time():
    app = MatterWaveApp(FakeAPI())
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.theme = "nord"
        await settle(app, pilot)
    again = MatterWaveApp(FakeAPI())
    async with again.run_test() as pilot:
        await settle(again, pilot)
        assert again.theme == "nord"
    # The config file's theme wins over the remembered one.
    configured = MatterWaveApp(FakeAPI(), theme="gruvbox")
    async with configured.run_test() as pilot:
        await settle(configured, pilot)
        assert configured.theme == "gruvbox"


def test_a_glob_names_files_and_a_path_is_not_one(tmp_path: Path):
    for name in ("b.txt", "a.txt", "c.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    (tmp_path / "d.txt").mkdir()
    assert expand_paths(str(tmp_path / "*.txt")) == [
        str(tmp_path / "a.txt"),
        str(tmp_path / "b.txt"),
    ]
    assert expand_paths(str(tmp_path / "a.txt")) is None
    assert expand_paths(str(tmp_path / "*.png")) == []


@pytest.mark.asyncio
async def test_a_glob_attaches_every_file_it_matches(tmp_path: Path):
    for name in ("one.txt", "two.txt", "skip.md"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    api = FakeAPI()
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.apply_attach(f'"{tmp_path / "*.txt"}"')
        await settle(app, pilot)
        assert [name for name, _ in api.uploads] == ["one.txt", "two.txt"]


@pytest.mark.asyncio
async def test_ctrl_o_picks_files_from_a_tree(tmp_path: Path, monkeypatch):
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    api = FakeAPI()
    app = MatterWaveApp(api)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "ctrl+o")
        # The tree loads in a worker that lives as long as the picker, so
        # the test waits on the tree's rows rather than on every worker.
        for _ in range(50):
            await pilot.pause(0.05)
            picker = app.screen
            if isinstance(picker, FilePicker):
                tree = picker.query_one("#files", DirectoryTree)
                if len(tree.root.children) == 2:
                    break
        assert isinstance(picker, FilePicker)
        # The root's children: one.txt, then two.txt. Mark the first, then
        # Enter on the second attaches both.
        tree.move_cursor(tree.root.children[0])
        await pilot.press("space")
        tree.move_cursor(tree.root.children[1])
        await pilot.press("enter")
        await settle(app, pilot)
        assert not isinstance(app.screen, FilePicker)
        assert [name for name, _ in api.uploads] == ["one.txt", "two.txt"]
