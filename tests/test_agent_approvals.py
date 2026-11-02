"""The approval decisions of agent contract 1.1, as the TUI applies them:
its own tools gated by kind through `agent.allowed` (read-only MCP and
network tools ask in plan, every MCP tool asks in ask and auto-edit), a
protected write asks in every mode, yolo included, a rejection is an
ordinary result, the refusals are the contract's sentences, and a command
Stop ended answers with the contract's stopped text."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from matterwave_sdk import agent as contract
from matterwave_sdk.models import ToolCall

from matterwave_tui.agent.loop import AgentSession, ApprovalRequest, RunEvents
from matterwave_tui.agent.mcp import McpManager, McpServerConfig
from matterwave_tui.agent.session import SessionOptions, open_session
from matterwave_tui.agent.shell import ProcessTree
from matterwave_tui.agent.tools import ToolOutcome

from .fake_api import FakeV1, call, say


class Seen(RunEvents):
    """Approvals answered from a list (no once it runs out), and every
    tool result the run produced."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[ApprovalRequest] = []
        self.results: list[tuple[str, ToolOutcome]] = []

    async def approve(self, request: ApprovalRequest) -> bool:
        self.asked.append(request)
        return self.answers.pop(0) if self.answers else False

    def on_tool_end(self, call: ToolCall, outcome: ToolOutcome, agent: str | None) -> None:
        self.results.append((call.function.name, outcome))


class Server:
    """An MCP server with a read-only tool and one that changes things."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[str] = []

    async def start(self) -> None:
        return None

    async def list_tools(self) -> list[dict]:
        return [{"name": "look", "annotations": {"readOnlyHint": True}}, {"name": "change"}]

    async def call(self, tool: str, arguments: dict) -> tuple[str, bool]:
        self.calls.append(tool)
        return f"{tool} done", False

    async def close(self) -> None:
        return None


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "w"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "a.py").write_bytes(b"A = 1\n")
    return workspace


async def session_for(
    fake: FakeV1, root: Path, events: RunEvents, *, trust: bool = True, **options
) -> tuple[AgentSession, Server]:
    session = (await open_session(fake.api(), SessionOptions(root=root, **options), events)).session
    server = Server("srv")
    session.mcp = McpManager(
        (McpServerConfig(name="srv", trust=trust),), cwd=str(root), factory=lambda config: server
    )
    await session.mcp.start()
    return session, server


# --- the kind table -----------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", contract.MODE_NAMES)
async def test_the_tuis_own_tools_get_the_contracts_decision_for_their_kind(root: Path, mode: str):
    session, _ = await session_for(FakeV1(), root, Seen(), mode=mode, yolo=True)
    registry = session.registry()
    expected = {
        "web_fetch": contract.allowed("web_fetch", mode, "network"),
        "todo_write": contract.allowed("todo_write", mode, "meta"),
        "exit_plan_mode": contract.allowed("exit_plan_mode", mode, "meta"),
        "mcp__srv__look": contract.allowed("mcp__srv__look", mode, "mcp", read_only=True),
        "mcp__srv__change": contract.allowed("mcp__srv__change", mode, "mcp"),
    }
    assert {name: session.decide(registry[name]) for name in expected} == expected
    # Read-only MCP and network tools ask in plan; every MCP tool asks in
    # ask and auto-edit; yolo runs them.
    if mode == "plan":
        assert expected["mcp__srv__look"] == "ask" and expected["mcp__srv__change"] == "deny"
    elif mode in ("ask", "auto-edit"):
        assert expected["mcp__srv__look"] == expected["mcp__srv__change"] == "ask"
    else:
        assert expected["mcp__srv__look"] == expected["mcp__srv__change"] == "allow"


@pytest.mark.asyncio
async def test_plan_mode_asks_before_a_read_only_mcp_tool_and_does_not_offer_the_rest(root: Path):
    fake = FakeV1([call("mcp__srv__look"), call("mcp__srv__change"), say("planned")])
    events = Seen(True)
    session, server = await session_for(fake, root, events, mode="plan")
    await session.run("plan")
    offered = fake.tool_names(0)
    assert "mcp__srv__look" in offered and "mcp__srv__change" not in offered
    assert "web_fetch" not in offered and "exit_plan_mode" in offered
    assert [r.tool for r in events.asked] == ["mcp__srv__look"]
    assert server.calls == ["look"]
    assert fake.tool_results()[-1] == contract.refusal("mode", tool="mcp__srv__change", mode="plan")


@pytest.mark.asyncio
async def test_an_untrusted_servers_read_only_claim_is_not_taken(root: Path):
    session, _ = await session_for(FakeV1(), root, Seen(), mode="plan", trust=False)
    assert session.decide(session.registry()["mcp__srv__look"]) == "deny"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ask", "auto-edit"])
async def test_every_mcp_call_asks_in_ask_and_auto_edit(root: Path, mode: str):
    fake = FakeV1([call("mcp__srv__look"), call("mcp__srv__change"), say("ok")])
    events = Seen(True, False)
    session, server = await session_for(fake, root, events, mode=mode)
    await session.run("go")
    assert [r.tool for r in events.asked] == ["mcp__srv__look", "mcp__srv__change"]
    assert server.calls == ["look"]


@pytest.mark.asyncio
async def test_yolo_runs_mcp_tools_without_asking(root: Path):
    fake = FakeV1([call("mcp__srv__change"), say("ok")])
    events = Seen()
    session, server = await session_for(fake, root, events, mode="yolo", yolo=True)
    await session.run("go")
    assert events.asked == [] and server.calls == ["change"]


# --- protected writes ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_protected_write_asks_in_yolo_and_is_written_only_on_a_yes(root: Path):
    fake = FakeV1(
        [
            call("write_file", path=".bashrc", content="alias ll='ls -l'\n"),
            call("edit", path="src/a.py", old_string="A = 1", new_string="A = 2"),
            call("write_file", path="AGENTS.md", content="Be brief.\n"),
            say("done"),
        ]
    )
    events = Seen(False, True)
    session, _ = await session_for(fake, root, events, mode="yolo", yolo=True)
    await session.run("set things up")
    assert [r.title for r in events.asked] == ["Create .bashrc", "Create AGENTS.md"]
    assert not (root / ".bashrc").exists()
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "Be brief.\n"
    assert (root / "src" / "a.py").read_bytes() == b"A = 2\n"


@pytest.mark.asyncio
async def test_a_rejection_is_the_contracts_sentence_as_an_ordinary_result(root: Path):
    fake = FakeV1(
        [call("edit", path="src/a.py", old_string="A = 1", new_string="A = 2"), say("ok")]
    )
    events = Seen(False)
    session, _ = await session_for(fake, root, events)
    await session.run("change it")
    ((name, outcome),) = events.results
    assert name == "edit" and not outcome.is_error
    assert outcome.text == contract.refusal("rejected", tool="edit")
    assert fake.tool_results()[-1] == outcome.text


@pytest.mark.asyncio
async def test_headless_a_protected_write_is_refused_with_needs_approval_even_in_yolo(root: Path):
    fake = FakeV1([call("write_file", path=".matterwave/settings.json", content="{}"), say("no")])
    events = Seen()
    session, _ = await session_for(fake, root, events, mode="yolo", yolo=True, interactive=False)
    outcome = await session.run("configure")
    sentence = contract.refusal("needs_approval", path=".matterwave/settings.json")
    assert outcome.status == "refused" and outcome.message is not None
    assert outcome.message.startswith(sentence) and "every mode" in outcome.message
    assert session.messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": sentence}
    assert not (root / ".matterwave").exists()


@pytest.mark.asyncio
async def test_headless_a_call_the_mode_asks_about_is_refused_with_the_headless_sentence(
    root: Path,
):
    fake = FakeV1([call("run_shell", command="echo hi"), say("no")])
    session, _ = await session_for(fake, root, Seen(), mode="auto-edit", interactive=False)
    outcome = await session.run("run it")
    assert outcome.status == "refused"
    assert outcome.message is not None and outcome.message.startswith(
        contract.refusal("headless", tool="run_shell", mode="auto-edit")
    )


@pytest.mark.asyncio
async def test_an_invented_tool_gets_the_unknown_tool_sentence(root: Path):
    fake = FakeV1([call("delete_everything"), say("ok")])
    session, _ = await session_for(fake, root, Seen())
    await session.run("go")
    assert fake.tool_results()[-1] == contract.refusal("unknown_tool", tool="delete_everything")


@pytest.mark.asyncio
async def test_a_bad_timeout_is_the_contracts_error(root: Path):
    fake = FakeV1([call("run_shell", command="echo hi", timeout_s=0), say("ok")])
    session, _ = await session_for(fake, root, Seen(), mode="yolo", yolo=True)
    await session.run("go")
    assert fake.tool_results()[-1] == "timeout_s must be a whole number of seconds from 1 to 600."


# --- Stop answers the command's call ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_command_stopped_by_the_user_answers_with_what_it_printed(root: Path):
    command = (
        "Write-Output started; Start-Sleep 60"
        if sys.platform == "win32"
        else "echo started; sleep 60"
    )
    fake = FakeV1([call("run_shell", command=command), say("never")])
    session, _ = await session_for(fake, root, Seen(), mode="yolo", yolo=True)
    started: list[ProcessTree] = []
    session.local.on_shell_start = started.append
    task = asyncio.create_task(session.run("wait"))
    for _ in range(200):
        if started:
            break
        await asyncio.sleep(0.05)
    assert started
    await asyncio.sleep(2)  # the shell is up and has printed
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    answer = session.messages[-1]
    assert answer["role"] == "tool" and answer["tool_call_id"] == "call_1"
    assert answer["content"].startswith(
        "Stopped by the user: the command and its child processes were ended.\n"
    )
    assert "started" in answer["content"]
