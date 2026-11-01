"""The edges a review found: files that steer the agent always ask, a link
to a denied file is not searched through, a fetch does not follow a
redirect, a bad glob is a failed call, compaction in the middle of a run
takes effect, the no-store fallback keeps the user's messages, and a
command's background children end with it."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

from matterwave_tui.agent.extras import web_fetch
from matterwave_tui.agent.loop import AgentSession, ApprovalRequest, RunEvents
from matterwave_tui.agent.session import SessionOptions, open_session
from matterwave_tui.agent.settings import SandboxConfig
from matterwave_tui.agent.shell import build_argv, run_command
from matterwave_tui.agent.tools import LocalTools

from .fake_api import FakeV1, call, say
from .test_agent_shell import alive, quoted, runner


class Asked(RunEvents):
    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[ApprovalRequest] = []

    async def approve(self, request: ApprovalRequest) -> bool:
        self.asked.append(request)
        return self.answers.pop(0) if self.answers else False


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "w"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_bytes(b"A = 1\n")
    return workspace


async def session_for(fake: FakeV1, root: Path, events: RunEvents | None = None, **options) -> AgentSession:
    return (await open_session(fake.api(), SessionOptions(root=root, **options), events)).session


@pytest.mark.asyncio
async def test_writes_to_the_agents_own_settings_and_context_files_ask_even_in_yolo(root: Path):
    fake = FakeV1([
        call("write_file", path=".matterwave/settings.json", content='{"mcpServers": {}}'),
        call("write_file", path="agents.md", content="new rules"),
        call("write_file", path="src/b.py", content="B = 2\n"),
        say("done"),
    ])
    events = Asked(False, False)
    session = await session_for(fake, root, events, mode="yolo", yolo=True)
    await session.run("configure yourself")
    assert [r.title for r in events.asked] == ["Create .matterwave/settings.json", "Create agents.md"]
    assert not (root / ".matterwave").exists() and not (root / "agents.md").exists()
    assert (root / "src" / "b.py").exists()


@pytest.mark.asyncio
async def test_headless_yolo_refuses_a_write_to_the_agents_settings(root: Path):
    fake = FakeV1([call("write_file", path=".matterwave/commands/x.md", content="x"), say("no")])
    session = await session_for(fake, root, mode="yolo", yolo=True, interactive=False)
    outcome = await session.run("x")
    assert outcome.status == "refused"
    assert not (root / ".matterwave").exists()


def _link_dir(target: Path, link: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        return
    os.symlink(target, link, target_is_directory=True)


def test_search_does_not_read_through_a_link_to_a_denied_folder(root: Path):
    private = root / "private"
    private.mkdir()
    (private / "notes.txt").write_text("TOKEN=hunter2\n", encoding="utf-8")
    (root / ".matterwaveignore").write_text("private/\n", encoding="utf-8")
    _link_dir(private, root / "alias")
    tools = LocalTools(root)
    grep = asyncio.run(tools.read("grep", {"pattern": "TOKEN"}))
    assert "hunter2" not in grep.text
    found = asyncio.run(tools.read("glob", {"pattern": "**/*.txt"}))
    assert "notes.txt" not in found.text


def test_a_bad_glob_is_a_failed_call_not_a_crash(root: Path):
    tools = LocalTools(root)
    out = asyncio.run(tools.read("glob", {"pattern": "[z-a]"}))
    assert out.is_error and "not valid" in out.text
    out = asyncio.run(tools.read("grep", {"pattern": "A", "glob": "[z-a]"}))
    assert out.is_error


@pytest.mark.asyncio
async def test_a_fetch_reports_a_redirect_instead_of_following_it():
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    text, failed = await web_fetch("https://public.test/start", client=client)
    assert failed and "redirects to http://169.254.169.254/latest/meta-data" in text
    assert "not followed" in text


@pytest.mark.asyncio
async def test_compaction_in_the_middle_of_a_run_shortens_the_next_request(root: Path):
    (root / "big.txt").write_text("word " * 1200, encoding="utf-8")
    models = {"data": [{"id": "small", "label": "Small", "included": True, "supports_tools": True,
                        "context_window": 3000}], "default_model": "small"}
    fake = FakeV1([call("read_file", path="big.txt") for _ in range(4)] + [say("ok")], models=models)
    session = await session_for(fake, root, Asked())
    await session.run("read it")
    sizes = [len(r["messages"]) for r in fake.requests]
    # Uncompacted, the fifth request would carry 10 messages.
    assert len(sizes) == 5 and max(sizes) <= 7, sizes
    assert len(session.messages) == len(fake.requests[-1]["messages"])
    # Mid-round, nothing is summarised on the server: a summary written
    # there before the round's results would put them on a branch. The
    # stored run keeps every call and its result on one path.
    assert fake.compactions == 0 and fake.branched == 0
    parts = [p for m in fake.stored[session.conversation_id] for p in m["parts"]]
    assert sum(p["type"] == "tool_call" for p in parts) == 4
    assert sum(p["type"] == "tool_result" for p in parts) == 4


@pytest.mark.asyncio
async def test_a_long_history_is_summarised_before_the_next_prompt_not_mid_round(root: Path):
    (root / "big.txt").write_text("word " * 2000, encoding="utf-8")
    models = {"data": [{"id": "small", "label": "Small", "included": True, "supports_tools": True,
                        "context_window": 3000}], "default_model": "small"}
    fake = FakeV1([call("read_file", path="big.txt"), say("read"), say("ok")], models=models)
    session = await session_for(fake, root, Asked())
    await session.run("read it")
    await session.run("now the next thing")
    assert fake.compactions == 1 and fake.branched == 0
    last = fake.requests[-1]["messages"]
    assert last[-1] == {"role": "user", "content": "now the next thing"}
    assert "The run so far, summarised." in last[0]["content"]
    stored = fake.stored[session.conversation_id]
    assert [m["content"] for m in stored if m["role"] == "user"] == ["read it", "now the next thing"]


def test_the_no_store_fallback_drops_rounds_and_keeps_the_users_messages(root: Path):
    session = AgentSession(FakeV1().api(), root, store=False, context_window=400)
    session.messages = [
        {"role": "user", "content": "FIRST TASK"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1", "type": "function",
                                                               "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "1", "content": "o" * 2000},
        {"role": "user", "content": "SECOND TASK"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2", "type": "function",
                                                               "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "2", "content": "p" * 50},
    ]
    dropped = session._drop_oldest_rounds(200)
    assert dropped == 1
    contents = [m.get("content") for m in session.messages]
    assert "FIRST TASK" in contents and "SECOND TASK" in contents
    assert [m.get("tool_call_id") for m in session.messages if m["role"] == "tool"] == ["2"]


@pytest.mark.asyncio
async def test_a_commands_background_children_end_with_it(tmp_path: Path):
    pid_file = tmp_path / "bg.pid"
    script = tmp_path / "bg.py"
    script.write_text(
        "import subprocess, sys\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n",
        encoding="utf-8",
    )
    result = await run_command(runner(quoted(str(script))), tmp_path, timeout_s=60, output_max_bytes=65536)
    assert result.exit_code == 0
    child = int(pid_file.read_text())
    deadline = time.monotonic() + 2.0
    while alive(child) and time.monotonic() < deadline:  # noqa: ASYNC110 -- polling another process
        await asyncio.sleep(0.05)
    assert not alive(child)


def test_the_sandbox_mounts_git_and_the_agents_folder_read_only(root: Path):
    (root / ".git").mkdir()
    (root / ".matterwave").mkdir()
    argv, _ = build_argv("true", root, SandboxConfig(engine="docker", image="img"), name="n1")
    assert "--init" in argv and argv[argv.index("--name"):argv.index("--name") + 2] == ["--name", "n1"]
    assert f"{root / '.git'}:/workspace/.git:ro" in argv
    assert f"{root / '.matterwave'}:/workspace/.matterwave:ro" in argv
    bwrap, _ = build_argv("true", root, SandboxConfig(engine="bwrap"))
    assert ["--ro-bind", str(root / ".git"), str(root / ".git")] == bwrap[
        bwrap.index(str(root / ".git")) - 1: bwrap.index(str(root / ".git")) + 2
    ]
    seatbelt, _ = build_argv("true", root, SandboxConfig(engine="seatbelt", network=True))
    assert "(deny network*)" not in seatbelt[2] and ".git" in seatbelt[2]
