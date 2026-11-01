"""grep runs in a process of its own: a pattern that backtracks without end
is killed at its deadline while the TUI's event loop keeps running, Stop
kills it, a search process left alone ends itself, and where no process
can start the search still answers."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent import grep as grep_module
from matterwave_tui.agent.grep import KILL_AFTER_S, READY, GrepProcess
from matterwave_tui.agent.tools import LocalTools, PendingWrite

# Exponential in CPython's `re`: each `a` doubles the ways `(a+)+` can split
# the run before `$` fails at the `!` (24 of them take seconds).
CATASTROPHIC = "(a+)+$"
LINE = "a" * 40 + "!"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (tmp_path / "slow.txt").write_text(f"{LINE}\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
def grep() -> Iterator[GrepProcess]:
    process = GrepProcess()
    yield process
    process.close()


@pytest.mark.asyncio
async def test_a_catastrophic_pattern_is_killed_at_its_deadline_and_the_loop_runs_on(
    root: Path, grep: GrepProcess
):
    tools = LocalTools(root, grep=grep, grep_timeout_s=1)
    warm = await tools.read("grep", {"pattern": "A = "})
    assert warm.text == "src/a.py:1: A = 1"
    first = grep.pid
    assert first is not None

    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.05)
            ticks += 1

    ticker = asyncio.create_task(tick())
    started = time.monotonic()
    out = await tools.read("grep", {"pattern": CATASTROPHIC})
    took = time.monotonic() - started
    ticker.cancel()

    assert out.is_error
    assert out.text == str(contract.ToolError("timeout", pattern=CATASTROPHIC, timeout_s="1"))
    assert 1 <= took < 1 + KILL_AFTER_S + 3
    # The event loop (the screen, Stop) kept running throughout.
    assert ticks >= int(took / 0.05) // 2
    assert grep.pid is None
    again = await tools.read("grep", {"pattern": "A = "})
    assert again.text == "src/a.py:1: A = 1"
    assert grep.pid not in (None, first)


@pytest.mark.asyncio
async def test_stop_kills_the_search(root: Path, grep: GrepProcess):
    tools = LocalTools(root, grep=grep, grep_timeout_s=60)
    await tools.read("grep", {"pattern": "A"})
    assert grep.pid is not None
    search = asyncio.create_task(tools.read("grep", {"pattern": CATASTROPHIC}))
    await asyncio.sleep(0.5)
    started = time.monotonic()
    search.cancel()
    with pytest.raises(asyncio.CancelledError):
        await search
    assert time.monotonic() - started < 5
    assert grep.pid is None


def test_a_search_process_left_alone_ends_itself(root: Path):
    # As when the TUI died mid-search: no one kills it, and it ends a
    # moment after the search's deadline even inside one endless match.
    proc = subprocess.Popen(  # noqa: S603 -- this interpreter, the search module
        GrepProcess().command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        assert proc.stdout.readline().strip() == READY
        request = {"root": str(root), "pattern": CATASTROPHIC, "path": ".", "timeout_s": 0.5}
        proc.stdin.write(json.dumps(request).encode() + b"\n")
        proc.stdin.flush()
        started = time.monotonic()
        assert proc.wait(timeout=0.5 + 2 * KILL_AFTER_S + 10) != 0
        assert time.monotonic() - started < 0.5 + 2 * KILL_AFTER_S + 5
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.asyncio
async def test_a_slow_search_ends_itself_and_keeps_the_process(
    root: Path, grep: GrepProcess, monkeypatch: pytest.MonkeyPatch
):
    # Many lines rather than one endless match: the SDK's deadline ends it
    # between lines, so the process answers and stays. (A longer grace
    # before the kill, so a busy machine cannot turn this into one.)
    monkeypatch.setattr(grep_module, "KILL_AFTER_S", 5.0)
    (root / "many.txt").write_text(f"{'a' * 18}!\n" * 400, encoding="utf-8")
    tools = LocalTools(root, grep=grep, grep_timeout_s=0.5)
    await tools.read("grep", {"pattern": "A", "path": "src"})
    first = grep.pid
    out = await tools.read("grep", {"pattern": CATASTROPHIC, "path": "many.txt"})
    assert out.is_error and out.text.startswith(
        f"The search for {CATASTROPHIC} stopped after 0.5 s"
    )
    assert grep.pid == first


@pytest.mark.asyncio
async def test_where_no_process_starts_the_search_runs_here(root: Path):
    grep = GrepProcess([sys.executable, "-c", "import sys; sys.exit(3)"])
    tools = LocalTools(root, grep=grep)
    out = await tools.read("grep", {"pattern": "A = ", "glob": "*.py"})
    assert out.text == "src/a.py:1: A = 1" and grep.pid is None
    bad = await tools.read("grep", {"pattern": "(", "path": "src"})
    assert bad.is_error and bad.text.startswith("The pattern ( is not a valid regular expression")


@pytest.mark.asyncio
async def test_grep_answers_the_contracts_errors_from_the_process(root: Path, grep: GrepProcess):
    tools = LocalTools(root, grep=grep)
    out = await tools.read("grep", {"pattern": "x", "glob": "[z-a]"})
    assert out.is_error and out.text == str(contract.ToolError("invalid_glob", glob="[z-a]"))
    out = await tools.read("grep", {"pattern": "x", "path": "../elsewhere"})
    assert out.is_error and out.text.startswith("Refused:")
    out = await tools.read("grep", {"pattern": ""})
    assert out.is_error and out.text == str(contract.ToolError("empty_pattern"))
    out = await tools.read("grep", {})
    assert out.is_error and out.text == "`pattern` is required."


def test_a_bad_ignore_line_is_skipped_and_the_rest_still_applies(root: Path):
    # An invalid range in .matterwaveignore no longer fails every search and
    # every confine: the line is skipped, as git skips it.
    (root / "private").mkdir()
    (root / "private" / "notes.txt").write_text("A private note\n", encoding="utf-8")
    (root / ".matterwaveignore").write_text("\ufeff[z-a].py\nprivate/\n", encoding="utf-8")
    tools = LocalTools(root)
    out = asyncio.run(tools.read("grep", {"pattern": "^A"}))
    assert out.text == "src/a.py:1: A = 1"
    refused = asyncio.run(tools.read("read_file", {"path": "private/notes.txt"}))
    assert refused.is_error and refused.text.startswith("Refused:")
    pending = tools.prepare_write("write_file", {"path": "src/b.py", "content": "B = 2\n"})
    assert isinstance(pending, PendingWrite), pending
