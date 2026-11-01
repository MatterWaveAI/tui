"""Children that detach from the command die with it.

`cmd /c start /b`, `Start-Process`, a child asking to break away from the
job (Windows), `setsid`, `nohup ... &` and a double-forked daemon (POSIX)
each leave the command's process tree or group in their own way; Stop, the
timeout and the command's own end must still take them. Windows: the shell
runs in a job object made while it is suspended. Linux: the keeper is a
child subreaper. macOS has no subreaper: the group and the descendants
still attached are ended, which these tests cover; a daemon already
orphaned to launchd is not (see shell.py)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

from matterwave_tui.agent import shell
from matterwave_tui.agent.shell import ProcessTree, run_command

from .test_agent_shell import alive

PY = sys.executable
WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="a Windows job object; POSIX has its own tests here")
LINUX = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="the keeper is a Linux child subreaper; Windows and macOS have their own tests here",
)
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sessions and process groups")


def child_script(tmp_path: Path) -> Path:
    """A program that writes its pid to the file it is given and sleeps."""
    script = tmp_path / "child.py"
    script.write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    return script


def _pid_in(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


async def pid_from(path: Path, *, within: float = 15.0) -> int:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        pid = _pid_in(path)
        if pid is not None:
            return pid
        await asyncio.sleep(0.05)
    raise AssertionError(f"no pid in {path}")


async def gone(pid: int, *, within: float = 2.0) -> bool:
    deadline = time.monotonic() + within
    while alive(pid) and time.monotonic() < deadline:  # noqa: ASYNC110 -- polling another process
        await asyncio.sleep(0.02)
    return not alive(pid)


def no_spaces(*paths: object) -> None:
    if any(" " in str(p) for p in paths):
        pytest.skip("cmd's start takes these paths unquoted here")


async def stop_after_child(command: str, tmp_path: Path, pid_file: Path) -> tuple[int, ProcessTree]:
    """Run `command`, wait for the child it starts to write its pid, check it
    is alive and in the command's tree, then Stop the run."""
    trees: list[ProcessTree] = []
    task = asyncio.create_task(
        run_command(command, tmp_path, timeout_s=120, output_max_bytes=65536, on_start=trees.append)
    )
    pid = await pid_from(pid_file)
    assert alive(pid)
    if sys.platform == "win32":
        assert trees[0].in_job(pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    return pid, trees[0]


# --- Windows -----------------------------------------------------------------------------


@WINDOWS
@pytest.mark.asyncio
async def test_a_cmd_start_b_child_dies_on_stop(tmp_path: Path):
    pid_file = tmp_path / "start-b.pid"
    child = child_script(tmp_path)
    no_spaces(PY, child, pid_file)
    pid, _ = await stop_after_child(f"cmd /c start /b {PY} {child} {pid_file}", tmp_path, pid_file)
    assert await gone(pid), "the start /b child outlived Stop"


@WINDOWS
@pytest.mark.asyncio
async def test_a_start_process_child_dies_on_stop(tmp_path: Path):
    pid_file = tmp_path / "start-process.pid"
    child = child_script(tmp_path)
    command = (
        f"Start-Process -WindowStyle Hidden -FilePath '{PY}' -ArgumentList "
        f"'\"{child}\"','\"{pid_file}\"'; Start-Sleep 60"
    )
    pid, _ = await stop_after_child(command, tmp_path, pid_file)
    assert await gone(pid), "the Start-Process child outlived Stop"


@WINDOWS
@pytest.mark.asyncio
async def test_a_child_that_asks_to_break_away_from_the_job_still_dies(tmp_path: Path):
    pid_file = tmp_path / "breakaway.pid"
    child = child_script(tmp_path)
    launcher = tmp_path / "breakaway.py"
    launcher.write_text(
        "import subprocess, sys\n"
        "flags = 0x01000000 | 0x00000008  # CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS\n"
        "try:\n"
        "    subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], creationflags=flags)\n"
        "except OSError:\n"
        "    subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], creationflags=0x00000008)\n",
        encoding="utf-8",
    )
    command = f"& '{PY}' '{launcher}' '{child}' '{pid_file}'; Start-Sleep 60"
    pid, _ = await stop_after_child(command, tmp_path, pid_file)
    assert await gone(pid), "a child that asked to break away outlived Stop"


@WINDOWS
@pytest.mark.asyncio
async def test_a_start_b_child_ends_with_its_command_without_waiting_for_the_timeout(tmp_path: Path):
    pid_file = tmp_path / "left.pid"
    child = child_script(tmp_path)
    no_spaces(PY, child, pid_file)
    started = time.monotonic()
    result = await run_command(
        f"cmd /c start /b {PY} {child} {pid_file}", tmp_path, timeout_s=60, output_max_bytes=65536
    )
    # The shell exits at once; the child holding its output does not keep
    # the call going to the timeout, and it is ended with the command.
    assert not result.timed_out and result.exit_code == 0
    assert time.monotonic() - started < 15
    pid = await pid_from(pid_file, within=1)
    assert await gone(pid)


@WINDOWS
@pytest.mark.asyncio
async def test_the_shell_is_in_the_job_before_it_runs_its_first_instruction(tmp_path: Path, monkeypatch):
    # Started suspended, assigned, then resumed: nothing the shell starts can
    # race the assignment.
    order: list[str] = []
    jobs: list[tuple[shell._WindowsJob, int]] = []
    real_assign = shell._WindowsJob.assign
    real_resume = shell.resume_process

    def assign(self: shell._WindowsJob, pid: int) -> None:
        real_assign(self, pid)
        jobs.append((self, pid))
        order.append("assign")

    def resume(pid: int) -> None:
        job, assigned = jobs[-1]
        assert assigned == pid and job.holds(pid)
        order.append("resume")
        real_resume(pid)

    monkeypatch.setattr(shell._WindowsJob, "assign", assign)
    monkeypatch.setattr(shell, "resume_process", resume)
    result = await run_command("Write-Output ran", tmp_path, timeout_s=60, output_max_bytes=65536)
    assert order == ["assign", "resume"]
    assert result.exit_code == 0 and result.output.strip() == "ran"


@WINDOWS
@pytest.mark.asyncio
async def test_without_a_job_the_shell_still_runs_and_stop_falls_back_to_taskkill(tmp_path: Path, monkeypatch):
    def refuse(self: shell._WindowsJob, pid: int) -> None:
        raise OSError("AssignProcessToJobObject failed")

    monkeypatch.setattr(shell._WindowsJob, "assign", refuse)
    result = await run_command("Write-Output ran; exit 4", tmp_path, timeout_s=60, output_max_bytes=65536)
    assert result.exit_code == 4 and result.output.strip() == "ran"


# --- Linux: the keeper ---------------------------------------------------------------------


@LINUX
@pytest.mark.asyncio
async def test_on_linux_setsid_nohup_and_double_forked_children_die_on_stop(tmp_path: Path):
    setsid_pid, nohup_pid, daemon_pid = (tmp_path / n for n in ("setsid.pid", "nohup.pid", "daemon.pid"))
    child = child_script(tmp_path)
    daemon = tmp_path / "daemon.py"
    daemon.write_text(
        "import os, sys, time\n"
        "if os.fork():\n    sys.exit(0)\n"
        "os.setsid()\n"
        "if os.fork():\n    sys.exit(0)\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = (
        f"setsid '{PY}' '{child}' '{setsid_pid}' > /dev/null 2>&1 < /dev/null & "
        f"nohup '{PY}' '{child}' '{nohup_pid}' > /dev/null 2>&1 & "
        f"'{PY}' '{daemon}' '{daemon_pid}' > /dev/null 2>&1; sleep 60"
    )
    trees: list[ProcessTree] = []
    task = asyncio.create_task(
        run_command(command, tmp_path, timeout_s=120, output_max_bytes=65536, on_start=trees.append)
    )
    pids = [await pid_from(p) for p in (setsid_pid, nohup_pid, daemon_pid)]
    assert trees[0].keeper and all(alive(p) for p in pids)
    # The daemon's parent has exited: it now belongs to the keeper, not init.
    assert pids[2] in shell.descendants(trees[0].pid)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [p for p in pids if not await gone(p)] == []


@LINUX
@pytest.mark.asyncio
async def test_on_linux_what_a_finished_command_detached_is_ended(tmp_path: Path):
    pid_file = tmp_path / "left.pid"
    child = child_script(tmp_path)
    started = time.monotonic()
    result = await run_command(
        f"setsid '{PY}' '{child}' '{pid_file}' & sleep 1; exit 3", tmp_path, timeout_s=60,
        output_max_bytes=65536,
    )
    assert result.exit_code == 3 and not result.timed_out
    assert time.monotonic() - started < 15
    assert await gone(await pid_from(pid_file, within=1))


@LINUX
@pytest.mark.asyncio
async def test_on_linux_the_keeper_reports_a_signal_as_the_command_did(tmp_path: Path):
    result = await run_command("kill -TERM $$", tmp_path, timeout_s=30, output_max_bytes=65536)
    assert result.exit_code is None
    assert result.render().startswith("Exit code none: the process was ended by a signal.")
    piped = await run_command("yes | head -n 2", tmp_path, timeout_s=30, output_max_bytes=65536)
    assert piped.exit_code == 0 and piped.output == "y\ny\n"


# --- POSIX without a subreaper (macOS), and Linux too ---------------------------------------


@POSIX
@pytest.mark.asyncio
async def test_on_posix_a_nohup_background_child_dies_on_stop(tmp_path: Path):
    pid_file = tmp_path / "nohup.pid"
    child = child_script(tmp_path)
    command = f"nohup '{PY}' '{child}' '{pid_file}' > /dev/null 2>&1 & sleep 60"
    pid, _ = await stop_after_child(command, tmp_path, pid_file)
    assert await gone(pid)


@POSIX
@pytest.mark.asyncio
async def test_on_posix_a_child_in_its_own_session_dies_on_stop_while_attached(tmp_path: Path):
    # Python's own setsid, since macOS has no setsid command: the child
    # leaves the process group, and is found as a descendant while its
    # parent lives (on Linux the keeper finds it in any case).
    pid_file = tmp_path / "own-session.pid"
    launcher = tmp_path / "own_session.py"
    launcher.write_text(
        "import os, subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], start_new_session=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    command = f"'{PY}' '{launcher}' '{child_script(tmp_path)}' '{pid_file}'"
    pid, _ = await stop_after_child(command, tmp_path, pid_file)
    assert await gone(pid)
