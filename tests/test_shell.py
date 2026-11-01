"""Shell assistant mode: each turn tells the model the OS and shell, and a
finished answer is a proposed command, shown in a gate that runs it only on
an explicit yes. The gate's tests stand the runner in; one test runs `echo`
in this machine's own shell, so the runner itself is proven."""

from __future__ import annotations

import os

import pytest
from matterwave_sdk import frames as fr
from textual.widgets import Static, TextArea

from matterwave_tui.app import MatterWaveApp, shell_command
from matterwave_tui.screens import Confirm
from matterwave_tui.shell import (
    SHELL_MARK,
    Shell,
    detect_shell,
    run_shell_command,
    shell_request,
)

from .fake_api import FakeAPI, happy_script, message


def status(app: MatterWaveApp) -> str:
    return str(app.query_one("#status", Static).render())


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


def transcript_text(app: MatterWaveApp) -> str:
    from textual.widgets import Markdown

    from matterwave_tui.widgets import MessageView

    return "\n".join(md.source for view in app.query(MessageView) for md in view.query(Markdown))


@pytest.mark.asyncio
async def test_the_gate_refuses_without_a_yes():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api, shell_mode=True)
    ran: list[str] = []

    async def runner(command: str) -> str:
        ran.append(command)
        return "ran"

    app._run_shell = runner
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#composer", TextArea).text = "list the files"
        app.action_send()
        await settle(app, pilot)
        # The answer "Hello there" stands as the proposed command.
        gate = app.screen
        assert isinstance(gate, Confirm)
        assert "Hello there" in str(gate.query_one(Static).render())
        assert "shell: answers are commands" in status(app)
        # Esc: nothing runs, and the gate says so by being gone.
        await pilot.press("escape")
        await settle(app, pilot)
        assert ran == []
        assert not isinstance(app.screen, Confirm)
        assert api.sent == [("c1", shell_request("list the files", app.shell), "grt-nano")]


@pytest.mark.asyncio
async def test_a_yes_runs_the_command_and_shows_the_output():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api, shell_mode=True)

    async def runner(command: str) -> str:
        return f"did {command}"

    app._run_shell = runner
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#composer", TextArea).text = "list the files"
        app.action_send()
        await settle(app, pilot)
        await pilot.press("enter")
        await settle(app, pilot)
        text = transcript_text(app)
        assert "$ Hello there" in text
        assert "did Hello there" in text


@pytest.mark.asyncio
async def test_a_fenced_answer_proposes_its_contents_only():
    api = FakeAPI()
    answer = message(
        "a1", "assistant", "```bash\ngit status --short\n```", parent="u1", model="grt-nano"
    )
    api.script = happy_script()
    api.script[-1] = fr.DoneFrame(answer)
    app = MatterWaveApp(api, shell_mode=True)
    ran: list[str] = []

    async def runner(command: str) -> str:
        ran.append(command)
        return ""

    app._run_shell = runner
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#composer", TextArea).text = "what changed?"
        app.action_send()
        await settle(app, pilot)
        assert isinstance(app.screen, Confirm)
        await pilot.press("enter")
        await settle(app, pilot)
        assert ran == ["git status --short"]


@pytest.mark.asyncio
async def test_slash_shell_toggles():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    shown = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert not app.shell_mode
        app.query_one("#composer", TextArea).text = "/shell"
        app.action_send()
        await pilot.pause()
        assert app.shell_mode
        assert any("Shell assistant on" in s for s in shown)
        assert "shell: answers are commands" in status(app)
        # A toggle is not a turn.
        assert api.sent == []
        app.query_one("#composer", TextArea).text = "/shell"
        app.action_send()
        await pilot.pause()
        assert not app.shell_mode


@pytest.mark.asyncio
async def test_a_turn_without_shell_mode_asks_nothing():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api)
    ran: list[str] = []

    async def runner(command: str) -> str:
        ran.append(command)
        return "ran"

    app._run_shell = runner
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.query_one("#composer", TextArea).text = "list the files"
        app.action_send()
        await settle(app, pilot)
        assert ran == []
        assert not isinstance(app.screen, Confirm)


@pytest.mark.asyncio
async def test_a_shell_turn_names_the_os_and_shell():
    api = FakeAPI(happy_script())
    app = MatterWaveApp(api, shell_mode=True)
    app._run_shell = lambda command: _never(command)
    async with app.run_test() as pilot:
        await settle(app, pilot)
        app.shell = Shell("Windows", "PowerShell", ("powershell", "-NoProfile", "-Command"))
        app.query_one("#composer", TextArea).text = "free disk space"
        app.action_send()
        await settle(app, pilot)
        sent = api.sent[0][1]
        assert sent.startswith(SHELL_MARK)
        assert "one PowerShell command for Windows" in sent
        assert sent.endswith("\n\nfree disk space")
        assert "in PowerShell?" in str(app.screen.query_one(Static).render())


async def _never(command: str) -> str:
    raise AssertionError(f"ran {command!r} without a yes")


def test_the_shell_is_detected_per_platform():
    ps = os.pathsep.join(["user", "machine", "install"])
    powershell = detect_shell({"PSModulePath": ps}, "Windows")
    assert (powershell.name, powershell.argv[0]) == ("PowerShell", "powershell")
    pwsh = detect_shell({"PSModulePath": ps, "POWERSHELL_DISTRIBUTION_CHANNEL": "MSI"}, "Windows")
    assert pwsh.argv[0] == "pwsh"
    cmd = detect_shell({"PSModulePath": "machine", "COMSPEC": "C:\\Windows\\cmd.exe"}, "Windows")
    assert (cmd.os_name, cmd.name, cmd.argv) == (
        "Windows",
        "cmd.exe",
        ("C:\\Windows\\cmd.exe", "/c"),
    )
    zsh = detect_shell({"SHELL": "/bin/zsh"}, "Darwin")
    assert (zsh.os_name, zsh.name, zsh.argv) == ("macOS", "zsh", ("/bin/zsh", "-c"))
    assert detect_shell({}, "Linux") == Shell("Linux", "sh", ("/bin/sh", "-c"))
    # Named outright, detection is not asked.
    forced = detect_shell({"PSModulePath": ps, "MATTERWAVE_SHELL": "cmd"}, "Windows")
    assert (forced.name, forced.argv) == ("cmd.exe", ("cmd", "/c"))
    forced = detect_shell({"MATTERWAVE_SHELL": "pwsh"}, "Linux")
    assert (forced.name, forced.argv) == ("PowerShell", ("pwsh", "-NoProfile", "-Command"))
    assert detect_shell({"MATTERWAVE_SHELL": "/usr/bin/fish"}, "Linux").name == "fish"


def test_a_shell_request_is_wrapped_once():
    bash = Shell("Linux", "bash", ("/bin/bash", "-c"))
    once = shell_request("list the files", bash)
    assert once.startswith(SHELL_MARK) and "one bash command for Linux" in once
    assert shell_request(once, bash) == once


@pytest.mark.asyncio
async def test_the_runner_runs_in_this_machines_shell():
    output, status = await run_shell_command("echo hello", detect_shell(os.environ))
    assert "hello" in output
    assert status == 0


def test_shell_command_takes_a_single_fenced_block():
    assert shell_command("```\necho hi\n```") == "echo hi"
    assert shell_command("```bash\ngit status\n```") == "git status"
    assert shell_command("git status") == "git status"
    # Prose with a fence inside is the whole text; the gate shows it and a
    # person decides.
    assert shell_command("Try:\n```bash\necho hi\n```\nfirst") == (
        "Try:\n```bash\necho hi\n```\nfirst"
    )
