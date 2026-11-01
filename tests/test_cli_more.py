"""The command line's other half: --version, --list-models, choosing the
one-shot's conversation, stdin as context, export, completion, the debug
log and the update check."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from matterwave_tui import __version__, cli, update
from matterwave_tui.app import MatterWaveApp

from .fake_api import FakeAPI

BASE = "https://matterwave.test"
CONV = {"id": "c1", "title": "Notes", "created_at": "1", "updated_at": "1"}
ANSWER = (
    'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
    'id: g1:0\nevent: delta\ndata: {"content": "Fine.", "index": 0}\n\n'
    'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n'
)
TREE = {
    **CONV,
    "current_leaf_message_id": "a1",
    "messages": [
        {
            "id": "u1",
            "conversation_id": "c1",
            "role": "user",
            "parts": [{"type": "text", "text": "Is <b>this</b> safe?"}],
            "content": "Is <b>this</b> safe?",
            "created_at": "1",
            "parent_id": None,
        },
        {
            "id": "a1",
            "conversation_id": "c1",
            "role": "assistant",
            "parts": [{"type": "text", "text": "Yes."}],
            "content": "Yes.",
            "created_at": "2",
            "parent_id": "u1",
            "model": "grt-nano",
        },
    ],
}
MODELS = {
    "data": [
        {"id": "grt-nano", "label": "GRT Nano", "included": True, "context_window": 8192},
        {"id": "grt-max", "label": "GRT Max", "included": False, "upgrade_to": "Pro"},
    ],
    "default_model": "grt-nano",
}


def platform(seen: list[httpx.Request]) -> Callable[..., httpx.AsyncClient]:
    """The routes the command line reaches, over a mocked transport."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        method, path = request.method, request.url.path
        if (method, path) == ("GET", "/api/conversations"):
            return httpx.Response(200, json={"data": [{**CONV, "id": "c9"}]})
        if (method, path) == ("POST", "/api/conversations"):
            return httpx.Response(201, json=CONV)
        if path.endswith("/messages"):
            return httpx.Response(
                200,
                text=ANSWER,
                headers={"content-type": "text/event-stream", "x-request-id": "r1"},
            )
        if path == "/api/models":
            return httpx.Response(200, json=MODELS)
        if path == "/api/conversations/c1/tree":
            return httpx.Response(200, json=TREE)
        return httpx.Response(404, json={"detail": path})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=transport, **kwargs)  # type: ignore[arg-type]

    return client


@pytest.fixture
def seen(monkeypatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", platform(requests))
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    return requests


def run(*argv: str) -> int:
    return cli.main(["--base-url", BASE, "--key", "mw_k", *argv])


def test_version_names_the_package_and_the_sdk(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["--version"])
    assert exit_.value.code == 0
    out = capsys.readouterr().out
    assert f"matterwave-tui {__version__} (matterwave-sdk " in out


def test_list_models_marks_the_default_and_the_plan_gate(seen, capsys):
    assert run("--list-models") == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "grt-nano\tGRT Nano\t8,192 tokens\t(default)"
    assert lines[1] == "grt-max\tGRT Max\t(on Pro)"


@pytest.mark.parametrize(
    ("flags", "posts", "target"),
    [
        ((), ["/api/conversations"], "c1"),
        (("--continue",), [], "c9"),
        (("--conversation", "c7"), [], "c7"),
        (("--temporary",), ["/api/conversations"], "c1"),
    ],
)
def test_the_one_shot_picks_its_conversation(seen, capsys, flags, posts, target):
    assert run(*flags, "-p", "hi") == 0
    creates = [r for r in seen if (r.method, r.url.path) == ("POST", "/api/conversations")]
    assert [r.url.path for r in creates] == posts
    if "--temporary" in flags:
        assert json.loads(creates[0].content) == {"temporary": True}
    send = next(r for r in seen if r.url.path.endswith("/messages"))
    assert send.url.path == f"/api/conversations/{target}/messages"
    assert capsys.readouterr().out == "Fine.\n"


def test_the_choice_of_conversation_means_nothing_to_the_app(seen, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_app", lambda *a, **k: None)
    assert run("--continue") == 0
    assert "--continue does nothing with the app" in capsys.readouterr().err


def test_a_piped_stdin_is_the_context_of_p(seen, monkeypatch):
    pipe = argparse.Namespace(isatty=lambda: False, read=lambda: "diff --git a/x b/x\n+new\n")
    monkeypatch.setattr(cli.sys, "stdin", pipe)
    assert run("-p", "review this") == 0
    send = next(r for r in seen if r.url.path.endswith("/messages"))
    assert json.loads(send.content)["content"] == "review this\n\ndiff --git a/x b/x\n+new"


@pytest.mark.parametrize(
    ("fmt", "expect"),
    [
        ("md", "## Assistant (grt-nano)\n\nYes."),
        ("json", '"role": "assistant"'),
        ("html", "<h2>Assistant (grt-nano)</h2>"),
    ],
)
def test_export_prints_a_conversation_in_each_format(seen, capsysbinary, fmt, expect):
    assert run("export", "c1", "--format", fmt) == 0
    out = capsysbinary.readouterr().out.decode("utf-8")
    assert expect in out
    if fmt == "html":
        # The model's and the person's raw HTML is shown, never run.
        assert "&lt;b&gt;this&lt;/b&gt;" in out and "<b>this</b>" not in out
    if fmt == "json":
        assert [m["id"] for m in json.loads(out)["messages"]] == ["u1", "a1"]


def test_export_to_a_file_never_writes_over_one(seen, tmp_path, capsys):
    target = tmp_path / "notes.md"
    assert run("export", "c1", "-o", str(target)) == 0
    assert target.read_text(encoding="utf-8").startswith("# Notes")
    target.write_text("mine", encoding="utf-8")
    assert run("export", "c1", "-o", str(target)) == cli.EXIT_ERROR
    assert target.read_text(encoding="utf-8") == "mine"
    assert "nothing was written over it" in capsys.readouterr().err


def test_completion_scripts_carry_every_flag_and_command(capsys):
    for shell in ("bash", "zsh", "fish", "powershell"):
        assert cli.main(["completion", shell]) == 0
        out = capsys.readouterr().out
        flag = "-l list-models" if shell == "fish" else "--list-models"
        for word in (flag, "export", "completion", "logout"):
            assert word in out, (shell, word)
        assert "powershell" in out  # the completion command's own choices


@pytest.mark.skipif(shutil.which("bash") is None, reason="no bash here")
def test_the_bash_script_parses(capsys):
    cli.main(["completion", "bash"])
    # On stdin rather than by path: on Windows the bash found first may be
    # WSL's, which cannot open a Windows temp path.
    script = capsys.readouterr().out.encode("utf-8")
    checked = subprocess.run(["bash", "-n"], input=script, capture_output=True)  # noqa: S603, S607
    assert checked.returncode == 0, checked.stderr


@pytest.mark.skipif(
    shutil.which("pwsh") is None and shutil.which("powershell") is None, reason="no PowerShell here"
)
def test_the_powershell_script_parses(tmp_path, capsys):
    cli.main(["completion", "powershell"])
    script = tmp_path / "completion.ps1"
    script.write_text(capsys.readouterr().out, encoding="utf-8")
    shell = shutil.which("pwsh") or shutil.which("powershell") or ""
    command = (
        "$errors = $null; [System.Management.Automation.Language.Parser]::ParseFile("
        f"'{script}', [ref]$null, [ref]$errors) | Out-Null; exit $errors.Count"
    )
    checked = subprocess.run([shell, "-NoProfile", "-Command", command], capture_output=True)  # noqa: S603
    assert checked.returncode == 0, checked.stdout + checked.stderr


def test_debug_logs_requests_but_never_what_was_said(seen, tmp_path):
    log = tmp_path / "debug.log"
    assert run("--debug", str(log), "-p", "my secret question") == 0
    text = log.read_text(encoding="utf-8")
    assert "POST /api/conversations 201" in text
    assert "POST /api/conversations/c1/messages 200 r1" in text
    assert "secret" not in text and "Fine." not in text


@pytest.mark.asyncio
async def test_the_update_check_looks_once_a_day(tmp_path):
    asked: list[str] = []

    def pypi(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, json={"info": {"version": "0.10.0"}})

    state = tmp_path / "update-check.json"
    async with httpx.AsyncClient(transport=httpx.MockTransport(pypi)) as http:
        assert await update.check_for_update("0.4.0", state, http=http, now=1000.0) == "0.10.0"
        assert await update.check_for_update("0.4.0", state, http=http, now=2000.0) is None
        day = 1000.0 + update.CHECK_EVERY_S
        assert await update.check_for_update("0.10.0", state, http=http, now=day) is None
    assert asked == [update.PYPI_URL, update.PYPI_URL]


@pytest.mark.asyncio
async def test_an_index_that_cannot_answer_is_silence(tmp_path):
    def missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing)) as http:
        assert await update.check_for_update("0.4.0", tmp_path / "s.json", http=http) is None


def test_versions_compare_by_their_numbers():
    assert update.newer("0.10.0", "0.9.9")
    assert not update.newer("0.4.0", "0.4.0")
    assert not update.newer("0.4.0rc1", "0.4.0")


@pytest.mark.asyncio
async def test_the_app_says_when_a_newer_release_is_out(monkeypatch):
    async def found(current: str, state: Path, *, http: httpx.AsyncClient) -> str:
        return "9.9.9"

    monkeypatch.setattr("matterwave_tui.app.check_for_update", found)
    app = MatterWaveApp(FakeAPI(), update_check=True)
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        await app.workers.wait_for_complete()
        assert any("matterwave-tui 9.9.9 is out" in s for s in shown)
