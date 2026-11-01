"""The agent loop against the scripted /v1: approvals and diffs, the
modes, the caps, the budget copy, availability, stored runs and resume,
compaction, context files and trust, @mentions, checkpoints, subagents,
MCP servers, web fetch and the todo list."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent import settings as agent_settings
from matterwave_tui.agent.context import expand_mentions, load_commands
from matterwave_tui.agent.loop import AgentSession, ApprovalRequest, RunEvents
from matterwave_tui.agent.session import OpenError, SessionOptions, open_session, remember_run
from matterwave_tui.failures import COPY

from .fake_api import AGENT_MODELS, FakeV1, call, calls, refuse, say

HERE = Path(__file__).parent


class Recorder(RunEvents):
    """Approvals answered from a list (no when it runs out), and everything
    the run said, kept for the assertions."""

    def __init__(self, *answers: bool) -> None:
        self.answers = list(answers)
        self.asked: list[ApprovalRequest] = []
        self.notices: list[str] = []
        self.todos: list[list[dict[str, str]]] = []
        self.modes: list[str] = []

    async def approve(self, request: ApprovalRequest) -> bool:
        self.asked.append(request)
        return self.answers.pop(0) if self.answers else False

    def on_notice(self, text: str) -> None:
        self.notices.append(text)

    def on_todos(self, todos: list[dict[str, str]]) -> None:
        self.todos.append(todos)

    def on_mode(self, mode: str) -> None:
        self.modes.append(mode)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    workspace = tmp_path / "project"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "app.py").write_bytes(b"def main():\r\n    return 1\r\n")
    (workspace / "src" / "util.py").write_bytes(b"VALUE = 2\n")
    (workspace / ".env").write_text("KEY=1\n", encoding="utf-8")
    return workspace


async def opened(
    fake: FakeV1, root: Path, events: RunEvents | None = None, **options
) -> AgentSession:
    result = await open_session(fake.api(), SessionOptions(root=root, **options), events)
    return result.session


def system_prompt(fake: FakeV1, index: int = 0) -> str:
    return fake.requests[index]["messages"][0]["content"]


# --- ask mode: the diff first, and a rejected change changes nothing ------------------------


@pytest.mark.asyncio
async def test_ask_mode_shows_the_diff_and_a_rejected_edit_leaves_the_file_byte_identical(
    root: Path,
):
    before = (root / "src" / "app.py").read_bytes()
    fake = FakeV1(
        [
            call("edit", path="src/app.py", old_string="return 1", new_string="return 2"),
            say("Understood, leaving it."),
        ]
    )
    events = Recorder(False)
    session = await opened(fake, root, events)
    outcome = await session.run("change the return value")
    assert outcome.status == "done"
    assert [r.kind for r in events.asked] == ["edit"]
    diff = events.asked[0].detail
    assert "-    return 1" in diff and "+    return 2" in diff
    assert (root / "src" / "app.py").read_bytes() == before
    assert "rejected" in fake.tool_results()[-1]
    assert session.checkpoints is None or session.checkpoints.entries() == []


@pytest.mark.asyncio
async def test_an_approved_edit_is_applied_after_the_diff(root: Path):
    fake = FakeV1(
        [
            call("edit", path="src/app.py", old_string="return 1", new_string="return 2"),
            say("Done."),
        ]
    )
    events = Recorder(True)
    session = await opened(fake, root, events)
    outcome = await session.run("change it")
    assert outcome.status == "done" and outcome.text == "Done."
    assert (root / "src" / "app.py").read_bytes() == b"def main():\r\n    return 2\r\n"
    assert fake.tool_results()[-1] == "Edited src/app.py."


@pytest.mark.asyncio
async def test_reading_needs_no_approval_and_refused_paths_never_reach_the_model(root: Path):
    outside = root.parent / "outside.txt"
    outside.write_text("outside secret", encoding="utf-8")
    fake = FakeV1(
        [
            calls(
                ("read_file", {"path": "src/util.py"}),
                ("read_file", {"path": ".env"}),
                ("read_file", {"path": "../outside.txt"}),
                ("read_file", {"path": str(outside)}),
            ),
            say("Read."),
        ]
    )
    events = Recorder()
    session = await opened(fake, root, events)
    await session.run("read things")
    results = fake.tool_results()
    assert events.asked == []
    assert results[0] == "1\tVALUE = 2"
    assert "is protected" in results[1] and "KEY=1" not in json.dumps(fake.requests)
    assert "goes through '..'" in results[2]
    assert "outside the workspace root" in results[3]
    assert "outside secret" not in json.dumps(fake.requests)


# --- the modes -----------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_mode_offers_the_model_read_tools_only(root: Path):
    fake = FakeV1(
        [
            call("edit", path="src/app.py", old_string="return 1", new_string="return 2"),
            say("I would change the return value."),
        ]
    )
    events = Recorder(True)
    session = await opened(fake, root, events, mode="plan")
    await session.run("plan a change")
    for index in range(len(fake.requests)):
        names = set(fake.tool_names(index))
        assert {"read_file", "list_dir", "glob", "grep"} <= names
        assert not names & {"edit", "write_file", "run_shell", "web_fetch"}
        assert names <= set(contract.READ_ONLY_TOOLS) | {"todo_write", "exit_plan_mode"}
    assert events.asked == []
    assert "not available in plan mode" in fake.tool_results()[-1]
    assert (root / "src" / "app.py").read_bytes() == b"def main():\r\n    return 1\r\n"
    assert "Approval mode: plan." in system_prompt(fake)


@pytest.mark.asyncio
async def test_auto_edit_applies_edits_without_asking_and_still_asks_before_run_shell(root: Path):
    fake = FakeV1(
        [
            call("edit", path="src/util.py", old_string="VALUE = 2", new_string="VALUE = 3"),
            call("run_shell", command="echo hi"),
            say("Edited; did not run the command."),
        ]
    )
    events = Recorder(False)
    session = await opened(fake, root, events, mode="auto-edit")
    await session.run("edit and test")
    assert [r.kind for r in events.asked] == ["shell"]
    assert events.asked[0].detail == "echo hi"
    assert (root / "src" / "util.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert "rejected" in fake.tool_results()[-1]


@pytest.mark.asyncio
async def test_yolo_needs_its_flag_and_is_never_saved(root: Path):
    fake = FakeV1()
    with pytest.raises(OpenError, match="--yolo"):
        await open_session(fake.api(), SessionOptions(root=root, mode="yolo"))
    session = await opened(fake, root)
    assert session.set_mode("yolo") is not None
    assert "yolo" not in session.cycle_modes()
    assert agent_settings.save_default_mode("yolo") is not None
    assert not agent_settings.user_settings_path().exists()
    assert agent_settings.save_default_mode("auto-edit") is None
    saved = json.loads(agent_settings.user_settings_path().read_text(encoding="utf-8"))
    assert saved == {"agent": {"default_mode": "auto-edit"}}
    # A yolo written into the file by hand is ignored, with a notice.
    agent_settings.user_settings_path().write_text(json.dumps({"agent": {"default_mode": "yolo"}}))
    loaded = agent_settings.load_settings(root, trusted=False)
    assert loaded.default_mode == "ask" and any("never saved" in n for n in loaded.notices)
    flagged = await opened(fake, root, yolo=True)
    assert flagged.set_mode("yolo") is None
    assert flagged.cycle_modes() == ["plan", "ask", "auto-edit", "yolo"]


@pytest.mark.asyncio
async def test_yolo_runs_commands_without_asking(root: Path):
    fake = FakeV1([call("run_shell", command="echo from-yolo"), say("Ran it.")])
    events = Recorder()
    session = await opened(fake, root, events, mode="yolo", yolo=True)
    await session.run("run it")
    assert events.asked == []
    assert "from-yolo" in fake.tool_results()[-1]
    assert any(
        "no sandbox" in n
        for n in (await open_session(fake.api(), SessionOptions(root=root, yolo=True))).notices
    )


@pytest.mark.asyncio
async def test_exit_plan_mode_switches_to_ask_on_a_yes(root: Path):
    fake = FakeV1([call("exit_plan_mode", plan="1. change it"), say("Switching.")])
    events = Recorder(True)
    session = await opened(fake, root, events, mode="plan")
    await session.run("plan")
    assert events.asked[0].kind == "plan" and events.asked[0].detail == "1. change it"
    assert session.mode == "ask" and events.modes == ["ask"]
    assert "edit" in fake.tool_names(-1)


# --- the caps, the budget and availability ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_step_cap_ends_a_looping_model_with_a_clear_message(root: Path):
    fake = FakeV1(repeat=call("list_dir"))
    session = await opened(fake, root, Recorder(), max_turns=3)
    outcome = await session.run("loop forever")
    assert outcome.status == "step_cap"
    assert "3 model calls, its cap" in (outcome.message or "")
    assert len(fake.requests) == 3


@pytest.mark.asyncio
async def test_the_token_cap_ends_a_looping_model_with_a_clear_message(root: Path):
    fake = FakeV1(repeat=call("list_dir", usage=(5000, 100)))
    session = await opened(fake, root, Recorder(), max_tokens=12_000)
    outcome = await session.run("loop forever")
    assert outcome.status == "token_cap"
    assert "15,300 tokens and its cap is 12,000" in (outcome.message or "")
    assert len(fake.requests) == 3
    assert outcome.usage["total_tokens"] == 15_300


@pytest.mark.asyncio
async def test_a_402_mid_run_ends_it_with_the_budget_copy(root: Path):
    fake = FakeV1(
        [
            call("list_dir"),
            refuse(402, "The organisation's token budget is used up.", code="budget"),
        ]
    )
    session = await opened(fake, root, Recorder())
    outcome = await session.run("work")
    assert outcome.status == "budget"
    assert COPY["budget"].title in (outcome.message or "")
    assert COPY["budget"].guidance in (outcome.message or "")
    assert "req-refused-2" in (outcome.message or "")


@pytest.mark.asyncio
async def test_a_429_out_of_quota_is_the_budget_and_a_429_window_is_a_rate_limit(root: Path):
    quota = FakeV1(
        [
            refuse(
                429,
                "Your organisation's monthly token budget is used up.",
                code="insufficient_quota",
            )
        ]
    )
    out = await (await opened(quota, root, Recorder())).run("x")
    assert out.status == "budget" and COPY["budget"].title in (out.message or "")
    window = FakeV1(
        [refuse(429, "Rate limit reached.", code="rate_limit_exceeded", **{"retry-after": "7"})]
    )
    out = await (await opened(window, root, Recorder())).run("x")
    assert out.status == "rate_limited" and COPY["rate_limited"].title in (out.message or "")


@pytest.mark.asyncio
async def test_agent_unavailable_for_a_model_without_tools_or_an_org_that_switched_it_off(
    root: Path,
):
    fake = FakeV1()
    result = await open_session(fake.api(), SessionOptions(root=root, model="grt-nano"))
    assert result.unavailable and "GRT Nano cannot call tools" in result.unavailable
    off = FakeV1(me={"subject": "u1", "kind": "user", "local_agent_tools": False})
    result = await open_session(off.api(), SessionOptions(root=root))
    assert result.unavailable and "switched off local agent tools" in result.unavailable
    # An older server sends neither field: the agent is allowed.
    old_models = {"data": [{"id": "m1", "label": "M1", "included": True}], "default_model": "m1"}
    older = FakeV1(me={"subject": "u1", "kind": "user"}, models=old_models)
    result = await open_session(older.api(), SessionOptions(root=root))
    assert result.unavailable is None and result.session.model == "m1"


@pytest.mark.asyncio
async def test_the_default_model_is_one_that_can_call_tools(root: Path):
    models = {"data": AGENT_MODELS["data"], "default_model": "grt-nano"}
    fake = FakeV1(models=models)
    result = await open_session(fake.api(), SessionOptions(root=root))
    assert result.session.model == "coder" and result.unavailable is None


# --- stored runs and resume ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_run_is_stored_and_resume_continues_it_with_its_tool_rounds(root: Path):
    fake = FakeV1([call("read_file", path="src/util.py"), say("VALUE is 2.")])
    session = await opened(fake, root, Recorder())
    await session.run("what is VALUE?")
    remember_run(session)
    cid = session.conversation_id
    assert cid and all(r["metadata"] == {"conversation_id": cid} for r in fake.requests)
    stored = fake.stored[cid]
    assert [m["role"] for m in stored] == ["user", "assistant"]
    assert [p["type"] for p in stored[1]["parts"]] == ["tool_call", "tool_result", "text"]

    fake.script = [say("It was 2; now I know.")]
    later = await opened(fake, root, Recorder(), resume=cid)
    await later.run("and before?")
    messages = fake.requests[-1]["messages"]
    roles = [m["role"] for m in messages]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert messages[2]["tool_calls"][0]["function"]["name"] == "read_file"
    assert messages[3]["content"] == "1\tVALUE = 2"
    assert later.conversation_id == cid

    fake.script = [say("Continued.")]
    again = await opened(fake, root, Recorder(), continue_last=True)
    assert again.conversation_id == cid


@pytest.mark.asyncio
async def test_continue_with_no_earlier_run_says_so(root: Path):
    with pytest.raises(OpenError, match="no earlier run"):
        await opened(FakeV1(), root, continue_last=True)


# --- compaction ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_old_tool_output_is_folded_before_anything_is_summarised(root: Path):
    big = "x = 1  # " + "padding " * 400 + "\n"
    (root / "big.py").write_text(big, encoding="utf-8")
    models = {
        "data": [
            {
                "id": "small",
                "label": "Small",
                "included": True,
                "supports_tools": True,
                "context_window": 4000,
            }
        ],
        "default_model": "small",
    }
    fake = FakeV1(
        [call("read_file", path="big.py") for _ in range(4)] + [say("Read it all.")], models=models
    )
    session = await opened(fake, root, Recorder())
    await session.run("read it four times")
    last = fake.tool_results()
    assert last[0].startswith("(folded:")
    assert not last[-1].startswith("(folded:")
    assert fake.compactions == 0


@pytest.mark.asyncio
async def test_compact_summarises_a_stored_run_with_the_conversations_compaction(root: Path):
    fake = FakeV1([call("read_file", path="src/util.py"), say("Done.")])
    session = await opened(fake, root, Recorder())
    await session.run("look")
    note = await session.compact(force=True)
    assert fake.compactions == 1 and "summarised" in note
    assert session.summary == "The run so far, summarised."
    assert session.messages[0] == {"role": "user", "content": "look"}
    fake.script.append(say("ok"))
    await session.run("next")
    # The summary rides in the system prompt; the stored user turn is the
    # user's own words, not the summary pasted over them.
    assert "The run so far, summarised." in system_prompt(fake, -1)
    stored = fake.stored[session.conversation_id]
    assert [m["content"] for m in stored if m["role"] == "user"] == ["look", "next"]
    assert fake.branched == 0


# --- context files, trust, mentions, commands ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_context_file_loads_in_a_trusted_folder_only(root: Path):
    (root / "MATTERWAVE.md").write_text("Use four-space indents.", encoding="utf-8")
    (root / "AGENTS.md").write_text("Run pytest -q.", encoding="utf-8")
    fake = FakeV1([say("ok"), say("ok")])
    result = await open_session(fake.api(), SessionOptions(root=root), Recorder())
    assert result.trust_unknown and not result.trusted
    assert any("not loaded: this folder is not trusted" in n for n in result.notices)
    await result.session.run("hi")
    assert "four-space" not in system_prompt(fake, 0)

    agent_settings.set_folder_trust(root, "trusted")
    trusted = await open_session(fake.api(), SessionOptions(root=root), Recorder())
    assert trusted.trusted and not trusted.trust_unknown
    await trusted.session.run("hi")
    prompt = system_prompt(fake, 1)
    assert (
        '<project-instructions path="MATTERWAVE.md">\nUse four-space indents.\n</project-instructions>'
        in prompt
    )
    assert (
        '<project-instructions path="AGENTS.md">\nRun pytest -q.\n</project-instructions>' in prompt
    )
    # The contract's slot: the same text the SDK makes of the same files.
    files = trusted.session.context.files
    extras = trusted.session.offered_extras()
    assert (
        contract.system_prompt("ask", str(root), extra_tools=extras, context_files=files) in prompt
    )


@pytest.mark.asyncio
async def test_the_personal_override_and_account_instructions_layer_on_top(root: Path):
    (root / "MATTERWAVE.local.md").write_text("I prefer short answers.", encoding="utf-8")
    fake = FakeV1([say("ok")], instructions="Answer in British English.")
    session = await opened(fake, root, Recorder(), trust=True)
    await session.run("hi")
    prompt = system_prompt(fake)
    assert (
        '<project-instructions path="MATTERWAVE.local.md">\nI prefer short answers.\n</project-instructions>'
        in prompt
    )
    assert prompt.rstrip().endswith("Answer in British English.")
    assert any("not in .gitignore" in n for n in session.context.notices)
    assert "never as instructions" in prompt


def test_at_mentions_attach_files_under_the_same_confinement(root: Path):
    out = expand_mentions("compare @src/util.py with @.env and mail me@example.com", root)
    assert out.included == ["src/util.py"]
    assert '<file path="src/util.py">\nVALUE = 2\n' in out.text
    assert "KEY=1" not in out.text
    assert any(".env was not attached" in n for n in out.notices)


def test_custom_commands_are_markdown_files(root: Path, private_dirs):
    _, config = private_dirs
    folder = agent_settings.config_dir() / "commands" / "git"
    folder.mkdir(parents=True)
    (folder / "review.md").write_text(
        "---\ndescription: Review a change\n---\nReview $ARGUMENTS for bugs.", encoding="utf-8"
    )
    project = root / ".matterwave" / "commands"
    project.mkdir(parents=True)
    (project / "lint.md").write_text("Run the linter.", encoding="utf-8")
    untrusted = load_commands(root, trusted=False)
    assert set(untrusted) == {"git:review"}
    assert untrusted["git:review"].expand("src/app.py") == "Review src/app.py for bugs."
    trusted = load_commands(root, trusted=True)
    assert trusted["lint"].expand("now") == "Run the linter.\n\nnow"


# --- checkpoints -------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    return subprocess.run(  # noqa: S603
        ["git", "-c", "commit.gpgsign=false", *args],  # noqa: S607
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.asyncio
async def test_restore_after_two_edits_brings_both_files_back_and_leaves_git_alone(root: Path):
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "start")
    head_before = git(root, "rev-parse", "HEAD")
    index_before = digest(root / ".git" / "index")
    head_file_before = digest(root / ".git" / "HEAD")
    app_before = (root / "src" / "app.py").read_bytes()
    util_before = (root / "src" / "util.py").read_bytes()

    fake = FakeV1(
        [
            call("edit", path="src/app.py", old_string="return 1", new_string="return 10"),
            call("write_file", path="src/util.py", content="VALUE = 20\n"),
            call("write_file", path="src/new.py", content="NEW = 1\n"),
            say("Three changes."),
        ]
    )
    session = await opened(fake, root, Recorder(), mode="auto-edit")
    await session.run("change things")
    assert (root / "src" / "app.py").read_bytes() != app_before
    assert session.checkpoints is not None
    rows = session.checkpoints.entries()
    assert [list(r.files) for r in rows] == [["src/app.py"], ["src/util.py"], ["src/new.py"]]
    assert not str(session.checkpoints.store).startswith(str(root))

    restored = session.checkpoints.restore(rows[0].id)
    assert sorted(restored) == ["src/app.py", "src/new.py", "src/util.py"]
    assert (root / "src" / "app.py").read_bytes() == app_before
    assert (root / "src" / "util.py").read_bytes() == util_before
    assert not (root / "src" / "new.py").exists()
    assert git(root, "rev-parse", "HEAD") == head_before
    assert digest(root / ".git" / "index") == index_before
    assert digest(root / ".git" / "HEAD") == head_file_before
    assert git(root, "status", "--porcelain") == ""
    # The restore checkpointed what it replaced, so it can be undone.
    assert session.checkpoints.entries()[-1].label == f"before /restore {rows[0].id}"


# --- subagents ----------------------------------------------------------------------------------------


def write_agent(name: str, tools: str, max_turns: str | None = None) -> None:
    folder = agent_settings.config_dir() / "agents"
    folder.mkdir(parents=True, exist_ok=True)
    extra = f"max_turns: {max_turns}\n" if max_turns else ""
    (folder / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: Reads code and reports\ntools: [{tools}]\n{extra}---\n"
        "You only read. Report what you find in one line.",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_a_subagent_keeps_to_its_own_tool_allow_list(root: Path):
    write_agent("reader", "read_file, grep")
    fake = FakeV1(
        [
            call("agent", agent="reader", task="Find VALUE in src/util.py"),
            call("run_shell", command="rm -rf src"),
            call("read_file", path="src/util.py"),
            say("VALUE is 2."),
            say("The subagent says VALUE is 2."),
        ]
    )
    events = Recorder(True)
    session = await opened(fake, root, events, mode="yolo", yolo=True)
    outcome = await session.run("delegate it")
    assert outcome.status == "done"
    assert set(fake.tool_names(1)) == {"read_file", "grep"}
    sub_prompt = fake.requests[1]["messages"]
    assert "You only read." in sub_prompt[0]["content"]
    assert sub_prompt[1] == {"role": "user", "content": "Find VALUE in src/util.py"}
    assert "no tool named run_shell" in fake.tool_results(2)[0]
    assert (root / "src").is_dir()
    assert "metadata" not in fake.requests[1]
    assert fake.tool_results(-1)[-1] == "VALUE is 2."
    assert session.budget.turns == 5


@pytest.mark.asyncio
async def test_a_subagents_calls_count_against_the_parents_caps(root: Path):
    write_agent("looper", "list_dir")
    fake = FakeV1([call("agent", agent="looper", task="list forever")], repeat=call("list_dir"))
    session = await opened(fake, root, Recorder(), max_turns=4)
    outcome = await session.run("delegate")
    assert outcome.status == "step_cap"
    assert len(fake.requests) == 4
    assert session.budget.turns == 4


# --- MCP ------------------------------------------------------------------------------------------------


def project_mcp(root: Path, log: Path, **extra) -> None:
    settings = root / ".matterwave" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    server = {
        "command": sys.executable,
        "args": [str(HERE / "mcp_stdio_server.py"), str(log)],
        "tools": ["lookup", "deploy"],
        **extra,
    }
    settings.write_text(
        json.dumps({"mcpServers": {"demo": server}, "matterwaveMcp": {"enabled": False}}),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_an_mcp_stdio_server_from_project_settings_lists_its_allowed_tools(
    root: Path, tmp_path: Path
):
    log = tmp_path / "mcp-calls.jsonl"
    project_mcp(root, log)
    fake = FakeV1(
        [
            call("mcp__demo__deploy", target="prod"),
            call("mcp__demo__lookup", word="agent"),
            call("mcp__demo__drop_database"),
            say("Done."),
        ]
    )
    events = Recorder(False, True)
    session = await opened(fake, root, events, trust=True)
    try:
        await session.run("use the server")
    finally:
        await session.aclose()
    names = fake.tool_names(0)
    assert "mcp__demo__lookup" in names and "mcp__demo__deploy" in names
    assert not any("drop_database" in n for n in names)
    # The server is not trusted, so its own read-only hint does not skip
    # the question: both calls ask.
    assert [r.kind for r in events.asked] == ["mcp", "mcp"]
    assert '"target": "prod"' in events.asked[0].detail
    results = fake.tool_results()
    assert "rejected" in results[0]
    assert results[1] == 'lookup ran with {"word": "agent"}'
    assert "no tool named mcp__demo__drop_database" in results[2]
    ran = [json.loads(line)["name"] for line in log.read_text(encoding="utf-8").splitlines()]
    assert ran == ["lookup"]


@pytest.mark.asyncio
async def test_project_mcp_servers_do_not_start_in_an_untrusted_folder(root: Path, tmp_path: Path):
    project_mcp(root, tmp_path / "log.jsonl")
    fake = FakeV1([say("ok")])
    session = await opened(fake, root, Recorder())
    await session.run("hi")
    assert not any(n.startswith("mcp__demo") for n in fake.tool_names(0))


@pytest.mark.asyncio
async def test_every_mcp_call_asks_and_a_trusted_servers_read_only_tool_asks_in_plan(
    root: Path, tmp_path: Path
):
    log = tmp_path / "calls.jsonl"
    project_mcp(root, log, trust=True)
    fake = FakeV1(
        [
            call("mcp__demo__deploy", target="staging"),
            call("mcp__demo__lookup", word="x"),
            say("Deployed."),
            call("mcp__demo__lookup", word="y"),
            say("Looked."),
        ]
    )
    events = Recorder(True, True, True)
    session = await opened(fake, root, events, trust=True)
    try:
        await session.run("deploy")
        # Trust takes the server's read-only hint; it does not skip the
        # question: every MCP call asks in ask and auto-edit.
        assert [r.tool for r in events.asked] == ["mcp__demo__deploy", "mcp__demo__lookup"]
        assert "deploy ran" in fake.tool_results()[0]
        session.set_mode("plan")
        offered = [t["function"]["name"] for t in session.tools_for_request()]
        assert "mcp__demo__deploy" not in offered and "mcp__demo__lookup" in offered
        await session.run("look it up")
        assert [r.tool for r in events.asked][-1] == "mcp__demo__lookup"
        assert fake.tool_results()[-1] == 'lookup ran with {"word": "y"}'
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_an_untrusted_servers_read_only_tool_is_not_offered_in_plan(
    root: Path, tmp_path: Path
):
    project_mcp(root, tmp_path / "calls.jsonl")
    fake = FakeV1([say("ok")])
    session = await opened(fake, root, Recorder(), trust=True, mode="plan")
    try:
        await session.run("plan")
        assert not any(n.startswith("mcp__demo") for n in fake.tool_names(0))
    finally:
        await session.aclose()


@pytest.mark.asyncio
async def test_matterwaves_own_mcp_is_prewired_with_the_users_credential(root: Path):
    tools = [
        {
            "name": "list_conversations",
            "description": "List",
            "inputSchema": {"type": "object"},
            "annotations": {"readOnlyHint": True},
        },
        {"name": "send_message", "description": "Send", "inputSchema": {"type": "object"}},
    ]
    fake = FakeV1(
        [
            call("mcp__matterwave__list_conversations"),
            call("mcp__matterwave__send_message", conversation_id="c9", content="hi"),
            say("Listed."),
        ],
        mcp_tools=tools,
    )
    events = Recorder(True, False)
    session = await opened(fake, root, events)
    await session.run("what did I talk about?")
    assert {"mcp__matterwave__list_conversations", "mcp__matterwave__send_message"} <= set(
        fake.tool_names(0)
    )
    assert fake.mcp_called == [("list_conversations", {})]
    # Read-only or not, an MCP call asks in ask mode.
    assert [r.kind for r in events.asked] == ["mcp", "mcp"]


# --- web fetch and the todo list -------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_fetch_asks_first_and_takes_only_http(root: Path):
    fake = FakeV1(
        [
            call("web_fetch", url="file:///etc/passwd"),
            call("web_fetch", url="https://example.com/x"),
            say("Skipped."),
        ]
    )
    events = Recorder(False)
    session = await opened(fake, root, events)
    await session.run("fetch")
    results = fake.tool_results()
    assert "http:// or https://" in results[0]
    assert [r.kind for r in events.asked] == ["web"]
    assert events.asked[0].detail == "https://example.com/x"
    assert "rejected" in results[1]


@pytest.mark.asyncio
async def test_the_todo_list_is_kept_and_shown(root: Path):
    todos = [
        {"content": "read", "status": "completed"},
        {"content": "edit", "status": "in_progress"},
    ]
    fake = FakeV1([call("todo_write", todos=todos), say("Working.")])
    events = Recorder()
    session = await opened(fake, root, events, mode="plan")
    await session.run("plan it")
    assert events.todos == [todos] and session.todos == todos
    assert fake.tool_results()[-1] == "Task list updated:\n[x] read\n[~] edit"
