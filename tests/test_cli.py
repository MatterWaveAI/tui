"""One-shot mode: `-p`, a positional prompt and a piped-in stdin each print
the answer to stdout and exit, with no interface. The wire is the mocked
transport, the way test_api.py mocks it."""

from __future__ import annotations

import argparse
import json
from collections.abc import Callable
from typing import Any

import httpx
import keyring

from matterwave_tui import cli

BASE = "https://matterwave.test"

CONV = {"id": "c1", "title": "t", "created_at": "1", "updated_at": "1"}

ANSWER = (
    'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
    'id: g1:0\nevent: part_start\ndata: {"index": 0, "type": "text"}\n\n'
    'id: g1:1\nevent: delta\ndata: {"content": "Two", "index": 0}\n\n'
    'id: g1:2\nevent: delta\ndata: {"content": " two", "index": 0}\n\n'
    'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n'
)


def mock_api(seen: list[httpx.Request], body: str = ANSWER) -> Callable[[], httpx.AsyncClient]:
    """An HttpChatAPI's httpx.AsyncClient factory over the mocked transport,
    recording every request the one-shot makes."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/conversations" and request.method == "POST":
            return httpx.Response(200, json=CONV)
        if request.url.path == "/api/conversations/c1/messages":
            return httpx.Response(
                200,
                headers={"x-request-id": "req-1", "content-type": "text/event-stream"},
                text=body,
            )
        return httpx.Response(404, json={"detail": "Not Found"})

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def client() -> httpx.AsyncClient:
        return real_client(transport=transport)

    return client


def send_body(seen: list[httpx.Request]) -> dict[str, Any]:
    send = next(r for r in seen if r.url.path == "/api/conversations/c1/messages")
    return json.loads(send.content)


def test_p_prints_the_answer_and_exits(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    rc = cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "two plus two?", "-m", "grt-nano"])
    assert rc == 0
    assert capsys.readouterr().out == "Two two\n"
    posts = [r for r in seen if r.method == "POST"]
    assert [r.url.path for r in posts] == ["/api/conversations", "/api/conversations/c1/messages"]
    assert send_body(seen) == {"content": "two plus two?", "model": "grt-nano"}
    assert posts[1].headers["authorization"] == "Bearer mw_k"


def test_a_bare_prompt_argument_answers_the_same(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    rc = cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hello"])
    assert rc == 0
    assert capsys.readouterr().out == "Two two\n"
    assert send_body(seen) == {"content": "hello"}


def test_piped_stdin_becomes_the_prompt(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    pipe = argparse.Namespace(isatty=lambda: False, read=lambda: "count to three\n")
    monkeypatch.setattr(cli.sys, "stdin", pipe)
    rc = cli.main(["--base-url", BASE, "--key", "mw_k"])
    assert rc == 0
    assert capsys.readouterr().out == "Two two\n"
    assert send_body(seen) == {"content": "count to three"}


def test_an_empty_prompt_is_a_usage_error(capsys, monkeypatch):
    pipe = argparse.Namespace(isatty=lambda: False, read=lambda: "   \n")
    monkeypatch.setattr(cli.sys, "stdin", pipe)
    rc = cli.main(["--base-url", BASE, "--key", "mw_k"])
    assert rc == 2
    assert "Nothing to send" in capsys.readouterr().err


def test_a_tty_opens_the_app(capsys, monkeypatch):
    opened: list[str] = []

    def run_app(base_url: str, credential: object, *, shell_mode: bool = False) -> None:
        opened.append(base_url)

    monkeypatch.setattr(cli, "run_app", run_app)
    tty = argparse.Namespace(isatty=lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", tty)
    rc = cli.main(["--base-url", BASE, "--key", "mw_k"])
    assert rc == 0
    assert opened == [BASE]


def test_shell_flag_reaches_the_app(monkeypatch):
    opened: list[bool] = []

    def run_app(base_url: str, credential: object, *, shell_mode: bool = False) -> None:
        opened.append(shell_mode)

    monkeypatch.setattr(cli, "run_app", run_app)
    tty = argparse.Namespace(isatty=lambda: True)
    monkeypatch.setattr(cli.sys, "stdin", tty)
    assert cli.main(["--base-url", BASE, "--key", "mw_k", "--shell"]) == 0
    assert opened == [True]


def test_a_cut_stream_warns_and_exits_one(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    body = (
        'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
        'id: g1:0\nevent: delta\ndata: {"content": "Hal", "index": 0}\n\n'
        "event: done\n\n"
    )
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen, body))
    rc = cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hi"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Hal" in captured.out
    assert "cut short" in captured.err


def test_a_key_is_stored_in_the_test_keyring_never_the_os_one(memory_keyring, monkeypatch):
    """`--key` stores the key in the keyring; in the suite that is the
    in-memory one, so a run leaves nothing in the developer's keyring and
    needs none on a headless runner."""
    assert keyring.get_keyring() is memory_keyring
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    assert cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hi"]) == 0
    assert memory_keyring.entries == {("matterwave", BASE): "mw_k"}
    assert cli.main(["--base-url", BASE, "logout"]) == 0
    assert memory_keyring.entries == {}
