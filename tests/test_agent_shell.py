"""run_shell for real: Stop ends the command and everything it started
within two seconds, the timeout does the same, the output is capped, the
API key stays out of the child's environment, and the sandbox confines
writes to the root."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent.settings import SandboxConfig
from matterwave_tui.agent.shell import OutputBuffer, ProcessTree, build_argv, run_command

PY = sys.executable


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32")
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
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
        run_command(
            "sleep 60", tmp_path, timeout_s=120, output_max_bytes=65536, on_start=started.append
        )
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
    while alive(grandchild) and time.monotonic() - stop < 2.0:  # noqa: ASYNC110 -- polling another process
        await asyncio.sleep(0.02)
    assert not alive(grandchild), "the grandchild outlived Stop by two seconds"


@pytest.mark.asyncio
async def test_the_timeout_stops_the_command(tmp_path: Path):
    started = time.monotonic()
    result = await run_command("sleep 30", tmp_path, timeout_s=1, output_max_bytes=65536)
    assert result.timed_out
    assert time.monotonic() - started < 5
    assert result.render(1).startswith("Stopped after 1 s: the command ran past its timeout")


@pytest.mark.asyncio
async def test_output_is_capped_keeping_head_and_tail(tmp_path: Path):
    command = runner("-c \"print('A' * 5000); print('B' * 5000)\"")
    result = await run_command(command, tmp_path, timeout_s=30, output_max_bytes=1001)
    assert result.exit_code == 0
    status, head, marker, tail = result.render(30).split("\n", 3)
    assert status == "Exit code 0" and head == "A" * 500
    assert re.fullmatch(r"\[\.\.\. \d+ bytes of output cut from the middle \.\.\.\]", marker)
    assert len(tail.encode()) == 501 and tail.rstrip().endswith("B" * 400)


@pytest.mark.parametrize("cap", [5, 10, 11, 1001])
def test_the_output_kept_is_cut_as_the_contract_cuts_the_whole(cap: int):
    # The buffer holds only a head and a tail; what it renders is what the
    # SDK makes of the whole output, multi-byte characters at the cuts included.
    for whole in ("0123456789ABCDEF" * 100, "\u00e9" * 3000 + "x" + "\u20ac" * 2000, "short\n", ""):
        data = whole.encode()
        buffer = OutputBuffer(cap)
        for i in range(0, len(data), 7):
            buffer.feed(data[i : i + 7])
        limits = {"shell_output_max_bytes": cap}
        expected = contract.format_shell(whole, exit_code=0, limits=limits)
        assert buffer.result(0).render(limits=limits) == expected, (cap, whole[:20])


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
    assert argv[:6] == ["podman", "run", "--rm", "-i", "--init", "--read-only"]
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
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if ran.returncode == 0:
            return image
    return None


@pytest.mark.asyncio
async def test_with_the_sandbox_on_a_write_outside_the_root_fails_and_inside_succeeds(
    tmp_path: Path,
):
    image = _podman_image()
    if image is None:
        pytest.skip("podman with the sandbox image is not available here")
    box = SandboxConfig(engine="podman", image=image)
    outside = await run_command(
        "touch /tmp/x", tmp_path, timeout_s=120, output_max_bytes=65536, sandbox=box
    )
    assert outside.exit_code != 0, outside.output
    assert "Read-only file system" in outside.output
    inside = await run_command(
        "touch inside.txt", tmp_path, timeout_s=120, output_max_bytes=65536, sandbox=box
    )
    assert inside.exit_code == 0, inside.output
    assert (tmp_path / "inside.txt").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not sys.platform.startswith("linux") or shutil.which("bwrap") is None,
    reason="bubblewrap runs on Linux, where it is installed",
)
async def test_with_bwrap_a_write_outside_the_root_fails(tmp_path: Path):
    box = SandboxConfig(engine="bwrap")
    outside = await run_command(
        "touch /tmp/mw-sandbox-x", tmp_path, timeout_s=30, output_max_bytes=65536, sandbox=box
    )
    assert outside.exit_code != 0
    inside = await run_command(
        "touch inside.txt", tmp_path, timeout_s=30, output_max_bytes=65536, sandbox=box
    )
    assert inside.exit_code == 0 and (tmp_path / "inside.txt").exists()


@pytest.mark.asyncio
async def test_stop_in_the_sandbox_removes_the_container(tmp_path: Path):
    image = _podman_image()
    if image is None:
        pytest.skip("podman with a runnable local image is not available here")
    started: list[ProcessTree] = []
    task = asyncio.create_task(
        run_command(
            "sleep 60",
            tmp_path,
            timeout_s=120,
            output_max_bytes=65536,
            sandbox=SandboxConfig(engine="podman", image=image),
            on_start=started.append,
        )
    )
    for _ in range(200):
        if started:
            break
        await asyncio.sleep(0.05)
    assert started and started[0].container is not None
    _, name = started[0].container
    await asyncio.sleep(3)  # the container is up
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = subprocess.run(  # noqa: S603, ASYNC221 -- a check after the fact
        ["podman", "ps", "-a", "--filter", f"name={name}", "--format", "{{.Names}}"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ).stdout
    assert name not in left
