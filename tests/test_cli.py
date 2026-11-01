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
import pytest

from matterwave_tui import cli
from matterwave_tui.auth import DeviceCode, DeviceToken

BASE = "https://matterwave.test"

CONV = {"id": "c1", "title": "t", "created_at": "1", "updated_at": "1"}

ANSWER = (
    'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
    'id: g1:0\nevent: part_start\ndata: {"index": 0, "type": "text"}\n\n'
    'id: g1:1\nevent: delta\ndata: {"content": "Two", "index": 0}\n\n'
    'id: g1:2\nevent: delta\ndata: {"content": " two", "index": 0}\n\n'
    'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n'
)


def mock_api(
    seen: list[httpx.Request], body: str = ANSWER, create: httpx.Response | None = None
) -> Callable[[], httpx.AsyncClient]:
    """An HttpChatAPI's httpx.AsyncClient factory over the mocked transport,
    recording every request the one-shot makes. `create` stands in for the
    conversation create's answer."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/api/conversations" and request.method == "POST":
            return create or httpx.Response(200, json=CONV)
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


def test_a_cut_stream_warns_and_exits_truncated(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    body = (
        'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
        'id: g1:0\nevent: delta\ndata: {"content": "Hal", "index": 0}\n\n'
        "event: done\n\n"
    )
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen, body))
    rc = cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hi"])
    assert rc == cli.EXIT_TRUNCATED
    captured = capsys.readouterr()
    # The mocked resume replays the same body, so the part shows twice.
    assert captured.out.startswith("Hal") and captured.out.endswith("\n")
    assert "cut short" in captured.err


USER = 'event: message\ndata: {"id": "u1", "role": "user"}\n\n'
DELTA = 'id: g1:0\nevent: delta\ndata: {"content": "Half", "index": 0}\n\n'


@pytest.mark.parametrize(
    ("body", "status", "note"),
    [
        (
            USER + DELTA + 'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": true}\n\n',
            cli.EXIT_TRUNCATED,
            "cut short",
        ),
        (
            USER + 'event: error\ndata: {"detail": "The monthly token budget is used up", "code": "budget"}\n\n',
            cli.EXIT_REFUSED,
            "budget",
        ),
        (
            USER + 'event: moderation_block\ndata: {"detail": "No.", "code": "moderation"}\n\n',
            cli.EXIT_REFUSED,
            "moderation",
        ),
        (
            USER + DELTA + 'event: error\ndata: {"detail": "Lost the connection to the model gateway"}\n\n',
            cli.EXIT_ERROR,
            "gateway",
        ),
    ],
)
def test_each_ending_has_its_exit_status(capsys, monkeypatch, body, status, note):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen, body))
    assert cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hi"]) == status
    captured = capsys.readouterr()
    assert captured.out in ("", "Half\n")
    assert note in captured.err


@pytest.mark.parametrize(
    ("answer", "status"),
    [
        (httpx.Response(402, json={"detail": "GRT Max is on Pro."}), cli.EXIT_REFUSED),
        (httpx.Response(429, json={"detail": "Rate limit exceeded."}), cli.EXIT_REFUSED),
        (httpx.Response(500, json={"detail": "boom"}), cli.EXIT_ERROR),
    ],
)
def test_a_refused_request_is_told_apart_from_a_failed_one(capsys, monkeypatch, answer, status):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen, create=answer))
    assert cli.main(["--base-url", BASE, "--key", "mw_k", "-p", "hi"]) == status
    assert capsys.readouterr().out == ""


def test_a_device_sign_in_prints_nothing_to_stdout_but_the_answer(capsys, monkeypatch):
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    monkeypatch.delenv("MATTERWAVE_API_KEY", raising=False)

    async def device_login(base_url, on_code, **_):
        on_code(
            DeviceCode("dc", "ABCD-EFGH", f"{BASE}/device", f"{BASE}/device?code=ABCD", 600, 1)
        )
        return DeviceToken("mw_device", "bearer", [])

    monkeypatch.setattr(cli, "device_login", device_login)
    assert cli.main(["--base-url", BASE, "-p", "hi"]) == cli.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == "Two two\n"
    assert "ABCD-EFGH" in captured.err and "Signed in" in captured.err


def test_help_names_the_exit_statuses(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    for line in ("0  the whole answer", "3  the platform refused", "4  the answer was cut short"):
        assert line in out


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
