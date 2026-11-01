"""`matterwave-tui -p` in agent mode, end to end through the CLI, the real
adapter and the SDK over the scripted /v1: the three output formats
(golden files), the exit codes, the refusals a headless run makes instead
of prompting, and --continue / --resume. Plain `-p` stays the chat
one-shot.

UPDATE_GOLDEN=1 rewrites the golden files from the current output."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from matterwave_tui import cli
from matterwave_tui.agent.headless import (
    EXIT_BUDGET,
    EXIT_CAP,
    EXIT_ERROR,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
)

from .fake_api import FakeV1, call, refuse, say

GOLDEN = Path(__file__).parent / "golden"
BASE = "https://matterwave.test"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "repo"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "util.py").write_bytes(b"VALUE = 2\n")
    return workspace


# The class itself, taken before any test patches it (the patch lands on the
# httpx module, so a second patch in one test would otherwise wrap the first).
REAL_CLIENT = httpx.AsyncClient


def wire(monkeypatch, fake: FakeV1) -> None:
    def client(*args, **kwargs) -> httpx.AsyncClient:
        return REAL_CLIENT(transport=httpx.MockTransport(fake.handle))

    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", client)


def run(monkeypatch, capsys, fake: FakeV1, *argv: str) -> tuple[int, str, str]:
    wire(monkeypatch, fake)
    code = cli.main(["--base-url", BASE, "--key", "mw_k", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def normalise(text: str, root: Path) -> str:
    escaped = json.dumps(str(root))[1:-1]
    return text.replace(escaped, "<root>").replace(str(root), "<root>")


def golden(name: str, text: str) -> None:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    assert text == path.read_text(encoding="utf-8"), f"{name} differs from its golden file"


def script() -> list:
    return [call("read_file", "Reading it.", path="src/util.py"), say("VALUE is 2.")]


# --- the three output formats -----------------------------------------------------------


def test_text_output_is_the_answer_on_stdout_and_the_tool_lines_on_stderr(monkeypatch, capsys, root):
    code, out, err = run(monkeypatch, capsys, FakeV1(script()), "code", str(root), "-p", "what is VALUE?")
    assert code == EXIT_OK
    golden("agent_text.stdout.txt", normalise(out, root))
    golden("agent_text.stderr.txt", normalise(err, root))


def test_json_output_is_one_document_with_result_usage_and_an_error_flag(monkeypatch, capsys, root):
    monkeypatch.chdir(root)
    code, out, _ = run(monkeypatch, capsys, FakeV1(script()), "-p", "what is VALUE?",
                       "--output-format", "json")
    assert code == EXIT_OK
    document = json.loads(out)
    assert document["result"] == "VALUE is 2." and document["is_error"] is False
    assert document["usage"] == {"prompt_tokens": 200, "completion_tokens": 40,
                                 "total_tokens": 240, "turns": 2}
    golden("agent_json.json", normalise(out, root))


def test_stream_json_is_one_event_per_line_then_the_result(monkeypatch, capsys, root):
    code, out, _ = run(monkeypatch, capsys, FakeV1(script()), "code", str(root), "-p", "what is VALUE?",
                       "--output-format", "stream-json")
    assert code == EXIT_OK
    events = [json.loads(line) for line in out.splitlines()]
    assert [e["type"] for e in events][0] == "init" and events[-1]["type"] == "result"
    golden("agent_stream.jsonl", normalise(out, root))


# --- exit codes ---------------------------------------------------------------------------


def test_a_budget_stop_exits_non_zero_with_the_budget_copy(monkeypatch, capsys, root):
    fake = FakeV1([call("list_dir"), refuse(402, "Token budget exhausted.", code="budget")])
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "go", "--output-format", "json")
    assert code == EXIT_BUDGET
    document = json.loads(out)
    assert document["status"] == "budget" and document["is_error"] is True
    assert "This workspace is out of tokens" in document["message"]


def test_a_write_under_the_default_mode_is_refused_not_prompted_and_exits_non_zero(monkeypatch, capsys, root):
    before = (root / "src" / "util.py").read_bytes()
    fake = FakeV1([call("edit", path="src/util.py", old_string="VALUE = 2", new_string="VALUE = 3"),
                   say("unreachable")])
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "bump it", "--output-format", "json")
    assert code == EXIT_REFUSED
    document = json.loads(out)
    assert document["status"] == "refused" and document["mode"] == "ask"
    assert "never prompts" in document["message"]
    assert (root / "src" / "util.py").read_bytes() == before
    assert len(fake.requests) == 1


def test_auto_edit_applies_the_write_headless(monkeypatch, capsys, root):
    fake = FakeV1([call("edit", path="src/util.py", old_string="VALUE = 2", new_string="VALUE = 3"),
                   say("Bumped.")])
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "bump it",
                       "--approval-mode", "auto-edit")
    assert code == EXIT_OK and out == "Bumped.\n"
    assert (root / "src" / "util.py").read_bytes() == b"VALUE = 3\n"


def test_max_turns_ends_a_looping_model(monkeypatch, capsys, root):
    fake = FakeV1(repeat=call("list_dir"))
    code, _, err = run(monkeypatch, capsys, fake, "code", str(root), "-p", "loop", "--max-turns", "2")
    assert code == EXIT_CAP
    assert "2 model calls, its cap" in err
    assert len(fake.requests) == 2


def test_max_tokens_ends_a_looping_model(monkeypatch, capsys, root):
    fake = FakeV1(repeat=call("list_dir", usage=(900, 100)))
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "loop", "--max-tokens", "2500",
                       "--output-format", "json")
    assert code == EXIT_CAP
    assert json.loads(out)["status"] == "token_cap"


def test_yolo_needs_its_flag_headless_too(monkeypatch, capsys, root):
    code, _, err = run(monkeypatch, capsys, FakeV1(), "code", str(root), "-p", "x", "--approval-mode", "yolo")
    assert code == EXIT_USAGE and "--yolo" in err
    fake = FakeV1([call("run_shell", command="echo yolo-ran"), say("Ran.")])
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "x", "--approval-mode", "yolo",
                       "--yolo", "--output-format", "json")
    assert code == EXIT_OK
    assert "yolo-ran" in fake.tool_results()[-1]
    assert any("no sandbox" in n for n in json.loads(out)["notices"])


def test_an_unavailable_model_exits_with_the_reason(monkeypatch, capsys, root):
    code, _, err = run(monkeypatch, capsys, FakeV1(), "code", str(root), "-p", "x", "-m", "grt-nano")
    assert code == EXIT_ERROR and "cannot call tools" in err


def test_continue_and_resume_pick_up_the_stored_run(monkeypatch, capsys, root):
    fake = FakeV1(script())
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "what is VALUE?",
                       "--output-format", "json")
    assert code == EXIT_OK
    cid = json.loads(out)["conversation_id"]
    fake.script = [say("Still 2.")]
    code, _, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "and now?", "--continue")
    assert code == EXIT_OK
    roles = [m["role"] for m in fake.requests[-1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert fake.requests[-1]["metadata"] == {"conversation_id": cid}
    fake.script = [say("Yes.")]
    code, _, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "sure?", "--resume", cid)
    assert code == EXIT_OK
    assert [m["role"] for m in fake.requests[-1]["messages"]].count("tool") == 1
    assert fake.requests[-1]["messages"][-1] == {"role": "user", "content": "sure?"}


def test_resume_of_a_run_that_is_not_there_is_a_usage_error(monkeypatch, capsys, root):
    code, _, err = run(monkeypatch, capsys, FakeV1(), "code", str(root), "-p", "x", "--resume", "nope")
    assert code == EXIT_USAGE and "could not be read" in err


def test_no_store_keeps_the_run_out_of_the_conversations(monkeypatch, capsys, root):
    fake = FakeV1([say("ok")])
    code, out, _ = run(monkeypatch, capsys, fake, "code", str(root), "-p", "x", "--no-store",
                       "--output-format", "json")
    assert code == EXIT_OK and json.loads(out)["conversation_id"] is None
    assert fake.conversations == {} and "metadata" not in fake.requests[0]


# --- the chat one-shot stands -------------------------------------------------------------


def test_plain_p_is_still_the_chat_one_shot(monkeypatch, capsys, root):
    seen: list[str] = []
    fake = FakeV1()
    real_handle = fake.handle

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/api/conversations/conv-1/messages":
            body = ('event: message\ndata: {"id": "u1", "role": "user"}\n\n'
                    'event: delta\ndata: {"content": "4", "index": 0}\n\n'
                    'event: done\ndata: {"id": "a1", "role": "assistant", "truncated": false}\n\n')
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)
        return real_handle(request)

    fake.handle = handle  # type: ignore[method-assign]
    code, out, _ = run(monkeypatch, capsys, fake, "-p", "two plus two?")
    assert code == 0 and out == "4\n"
    assert "/v1/chat/completions" not in seen


def test_agent_options_without_code_or_p_are_a_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exited:
        cli.main(["--base-url", BASE, "--key", "mw_k", "--yolo"])
    assert exited.value.code == 2
    assert "need `code`" in capsys.readouterr().err
