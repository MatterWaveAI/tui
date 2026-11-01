"""run_shell for real: Stop ends the command and everything it started
within two seconds, the timeout does the same, the output is capped, the
API key stays out of the child's environment, and the sandbox confines
writes to the root."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from matterwave_tui.agent.settings import SandboxConfig
from matterwave_tui.agent.shell import ProcessTree, build_argv, run_command

PY = sys.executable


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        out = subprocess.run(  # noqa: S603
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],  # noqa: S607
            capture_output=True, text=True, check=False,
        ).stdout
        return f'"{pid}"' in out
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); it is dead for this purpose.
    try:
        with open(f"/proc/{pid}/stat", encoding="ascii") as handle:
            return handle.read().split()[2] != "Z"
    except OSError:
        return True


def quoted(path: str) -> str:
    return f"'{path}'" if sys.platform == "win32" else f'"{path}"'


def runner(command: str) -> str:
    """`python ...` the way each platform's shell takes a quoted exe."""
    exe = quoted(PY)
    return f"& {exe} {command}" if sys.platform == "win32" else f"{exe} {command}"


@pytest.mark.asyncio
async def test_stop_during_sleep_60_ends_the_child_within_two_seconds(tmp_path: Path):
    started: list[ProcessTree] = []
    task = asyncio.create_task(
        run_command("sleep 60", tmp_path, timeout_s=120, output_max_bytes=65536,
                    on_start=started.append)
    )
    for _ in range(100):
        if started:
            break
        await asyncio.sleep(0.05)
    assert started, "the command never started"
    await asyncio.sleep(0.5)  # let the shell get going
    pid = started[0].pid
    assert alive(pid)
    stop = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - stop
    assert elapsed < 2.0, f"Stop took {elapsed:.2f}s"
    assert started[0].proc.returncode is not None
    assert not alive(pid)


@pytest.mark.asyncio
async def test_stop_kills_the_whole_tree(tmp_path: Path):
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open(r'{pid_file}', 'w').write(str(child.pid))\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    task = asyncio.create_task(
        run_command(runner(quoted(str(script))), tmp_path, timeout_s=120, output_max_bytes=65536)
    )
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert alive(grandchild)
    stop = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    deadline = time.monotonic() + 2.0
    while alive(grandchild) and time.monotonic() < deadline:  # noqa: ASYNC110 -- polling another process
        await asyncio.sleep(0.05)
    assert not alive(grandchild), "the grandchild outlived Stop"
    assert time.monotonic() - stop < 2.5


@pytest.mark.asyncio
async def test_the_timeout_stops_the_command(tmp_path: Path):
    started = time.monotonic()
    result = await run_command("sleep 30", tmp_path, timeout_s=1, output_max_bytes=65536)
    assert result.timed_out
    assert time.monotonic() - started < 5
    assert "Stopped after 1s" in result.render(1)


@pytest.mark.asyncio
async def test_output_is_capped_keeping_head_and_tail(tmp_path: Path):
    command = runner("-c \"print('A' * 5000); print('B' * 5000)\"")
    result = await run_command(command, tmp_path, timeout_s=30, output_max_bytes=1000)
    assert result.exit_code == 0
    assert result.output.startswith("A" * 400)
    assert result.output.rstrip().endswith("B" * 400)
    assert result.omitted_bytes > 8000
    assert "bytes omitted" in result.render(30)


@pytest.mark.asyncio
async def test_the_api_key_is_not_in_the_commands_environment(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MATTERWAVE_API_KEY", "mw_secret_value")
    command = runner("-c \"import os; print(os.environ.get('MATTERWAVE_API_KEY', 'absent'))\"")
    result = await run_command(command, tmp_path, timeout_s=30, output_max_bytes=65536)
    assert result.output.strip() == "absent"


@pytest.mark.asyncio
async def test_the_command_runs_in_the_root_and_reports_its_exit_code(tmp_path: Path):
    (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
    listing = "Get-ChildItem -Name" if sys.platform == "win32" else "ls"
    result = await run_command(f"{listing}; exit 3", tmp_path, timeout_s=30, output_max_bytes=65536)
    assert "marker.txt" in result.output
    assert result.exit_code == 3


def test_a_container_sandbox_mounts_only_the_root_read_write(tmp_path: Path):
    argv, _ = build_argv("touch x", tmp_path, SandboxConfig(engine="podman", image="img"))
    assert argv[:5] == ["podman", "run", "--rm", "-i", "--read-only"]
    assert "--read-only-tmpfs=false" in argv and "--network=none" in argv
    assert f"{tmp_path}:/workspace:rw" in argv
    assert argv[-4:] == ["img", "/bin/sh", "-c", "touch x"]
    bwrap, _ = build_argv("touch x", tmp_path, SandboxConfig(engine="bwrap"))
    assert bwrap[:3] == ["bwrap", "--ro-bind", "/"] and "--unshare-net" in bwrap


SANDBOX_IMAGES = (
    os.environ.get("MATTERWAVE_TEST_SANDBOX_IMAGE", ""),
    SandboxConfig().image,
    "docker.io/library/alpine:3",
    "docker.io/library/redis:7-alpine",
    "docker.io/library/postgres:16-alpine",
)


def _podman_image() -> str | None:
    """A local image podman can run here (never pulled: the test must not
    need the network), or None."""
    if shutil.which("podman") is None:
        return None
    for image in filter(None, SANDBOX_IMAGES):
        try:
            ran = subprocess.run(  # noqa: S603
                ["podman", "run", "--rm", "--pull=never", image, "/bin/sh", "-c", "true"],  # noqa: S607
                capture_output=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if ran.returncode == 0:
            return image
    return None


@pytest.mark.asyncio
async def test_with_the_sandbox_on_a_write_outside_the_root_fails_and_inside_succeeds(tmp_path: Path):
    image = _podman_image()
    if image is None:
        pytest.skip("podman with the sandbox image is not available here")
    box = SandboxConfig(engine="podman", image=image)
    outside = await run_command("touch /tmp/x", tmp_path, timeout_s=120, output_max_bytes=65536, sandbox=box)
    assert outside.exit_code != 0, outside.output
    assert "Read-only file system" in outside.output
    inside = await run_command("touch inside.txt", tmp_path, timeout_s=120, output_max_bytes=65536, sandbox=box)
    assert inside.exit_code == 0, inside.output
    assert (tmp_path / "inside.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
                    reason="bubblewrap runs on Linux, where it is installed")
async def test_with_bwrap_a_write_outside_the_root_fails(tmp_path: Path):
    box = SandboxConfig(engine="bwrap")
    outside = await run_command("touch /tmp/mw-sandbox-x", tmp_path, timeout_s=30, output_max_bytes=65536, sandbox=box)
    assert outside.exit_code != 0
    inside = await run_command("touch inside.txt", tmp_path, timeout_s=30, output_max_bytes=65536, sandbox=box)
    assert inside.exit_code == 0 and (tmp_path / "inside.txt").exists()
