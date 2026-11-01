"""`run_shell`: one command in the workspace root, with a timeout, an
output cap and a whole-tree kill.

The shell is the contract's (`agent.shell_for`): `/bin/sh -c` on POSIX,
PowerShell on Windows. Stopping a command stops everything it started:

- POSIX: the command leads a new session, and the whole process group is
  sent SIGTERM, then SIGKILL a moment later.
- Windows: the command is put in a job object that kills its members when
  closed; Stop terminates the job. `taskkill /T /F` is the fallback when a
  job cannot be made.

The child's environment is the user's, less `MATTERWAVE_API_KEY`: the
command runs as the user, but the model has no business reading the key it
is billed through.

Confinement is not a sandbox, and a command can reach anything the user
can. The optional sandbox (`SandboxConfig`) runs it in a container that
sees only the root (podman or docker), under bubblewrap on Linux, or under
seatbelt on macOS, with no network unless the settings allow it.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from matterwave_sdk import agent as contract

from .settings import SandboxConfig

KILL_GRACE_S = 0.3
SCRUBBED_ENV = ("MATTERWAVE_API_KEY",)
CONTAINER_ROOT = "/workspace"
SANDBOX_ENGINES = ("none", "podman", "docker", "bwrap", "seatbelt")


@dataclass(frozen=True)
class ShellResult:
    exit_code: int | None
    output: str
    timed_out: bool = False
    omitted_bytes: int = 0

    def render(self, timeout_s: float) -> str:
        """The tool result: the exit code, then the output as it came."""
        if self.timed_out:
            head = f"Stopped after {timeout_s:g}s (the timeout); the command and its children were killed."
        else:
            head = f"Exit code {self.exit_code}."
        body = self.output if self.output else "(no output)"
        if self.omitted_bytes:
            body += f"\n(output cut: {self.omitted_bytes} bytes omitted from the middle)"
        return f"{head}\n{body}"


class SandboxUnavailable(RuntimeError):
    """The sandbox the settings ask for cannot run here; commands are
    refused rather than run outside it."""


def sandbox_available(config: SandboxConfig) -> str | None:
    """Why the sandbox cannot run here, or None when it can."""
    engine = config.engine
    if engine == "none":
        return None
    if engine not in SANDBOX_ENGINES:
        return f"unknown sandbox engine {engine!r}; one of {', '.join(SANDBOX_ENGINES)}"
    if engine in ("podman", "docker"):
        return None if shutil.which(engine) else f"{engine} is not installed"
    if engine == "bwrap":
        if not sys.platform.startswith("linux"):
            return "bwrap runs on Linux only"
        return None if shutil.which("bwrap") else "bwrap is not installed"
    if sys.platform != "darwin":
        return "seatbelt runs on macOS only"
    return None if shutil.which("sandbox-exec") else "sandbox-exec is not available"


def sandbox_platform(config: SandboxConfig) -> str | None:
    """The platform the command really runs on, for the system prompt: a
    container is Linux whatever the host is."""
    return "linux" if config.engine in ("podman", "docker") else None


def _seatbelt_profile(root: str) -> str:
    quoted = root.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "(version 1)(allow default)(deny network*)(deny file-write*)"
        f'(allow file-write* (subpath "{quoted}") (literal "/dev/null") (literal "/dev/tty"))'
    )


def build_argv(command: str, root: Path, sandbox: SandboxConfig) -> tuple[list[str], str]:
    """The argv to start and the working directory to start it in."""
    if sandbox.engine in ("podman", "docker"):
        argv = [sandbox.engine, "run", "--rm", "-i", "--read-only"]
        if sandbox.engine == "podman":
            # Podman mounts a writable /tmp on a read-only container unless told not to.
            argv.append("--read-only-tmpfs=false")
        if not sandbox.network:
            argv.append("--network=none")
        argv += ["-v", f"{root}:{CONTAINER_ROOT}:rw", "-w", CONTAINER_ROOT]
        argv += [sandbox.image, "/bin/sh", "-c", command]
        return argv, str(root)
    if sandbox.engine == "bwrap":
        argv = [
            "bwrap", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
            "--bind", str(root), str(root), "--chdir", str(root), "--die-with-parent",
        ]
        if not sandbox.network:
            argv.append("--unshare-net")
        return [*argv, "/bin/sh", "-c", command], str(root)
    if sandbox.engine == "seatbelt":
        profile = _seatbelt_profile(os.path.realpath(root))
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", command], str(root)
    _, prefix = contract.shell_for()
    return [*prefix, command], str(root)


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# --- the Windows job object -----------------------------------------------------


class _WindowsJob:
    """A job object holding the command and everything it starts. Closing
    or terminating it ends them all."""

    def __init__(self, pid: int) -> None:
        import ctypes
        from ctypes import wintypes

        self._handle: int | None = None
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.OpenProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObjectW failed")

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _Io(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _Extended()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
            kernel32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed")
        process = kernel32.OpenProcess(0x0001 | 0x0100, False, pid)  # TERMINATE | SET_QUOTA
        if not process:
            kernel32.CloseHandle(job)
            raise OSError("OpenProcess failed")
        try:
            if not kernel32.AssignProcessToJobObject(job, process):
                kernel32.CloseHandle(job)
                raise OSError("AssignProcessToJobObject failed")
        finally:
            kernel32.CloseHandle(process)
        self._kernel32 = kernel32
        self._handle = job

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


class ProcessTree:
    """A started command and the means to end it with everything below it."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc
        self._job: _WindowsJob | None = None
        if sys.platform == "win32":
            with contextlib.suppress(OSError):
                self._job = _WindowsJob(proc.pid)

    @property
    def pid(self) -> int:
        return self.proc.pid

    async def kill(self) -> None:
        """End the command and its children, and wait for the command."""
        if self.proc.returncode is None:
            if sys.platform == "win32":
                if self._job is not None:
                    self._job.terminate()
                else:
                    await _taskkill(self.proc.pid)
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(self.proc.wait(), KILL_GRACE_S)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.killpg(self.proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            if self.proc.returncode is None:
                self.proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 5)
        self.close()

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


async def _taskkill(pid: int) -> None:
    with contextlib.suppress(OSError):
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/T", "/F", "/PID", str(pid),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()


async def start(argv: list[str], cwd: str) -> ProcessTree:
    kwargs: dict[str, object] = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=child_env(),
        **kwargs,  # type: ignore[arg-type]
    )
    return ProcessTree(proc)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        import locale

        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


async def run_command(
    command: str,
    root: Path,
    *,
    timeout_s: float,
    output_max_bytes: int,
    sandbox: SandboxConfig | None = None,
    on_start: Callable[[ProcessTree], None] | None = None,
) -> ShellResult:
    """Run `command` in `root` to its end, its timeout, or its caller's
    cancellation. Cancelled (Stop), the whole tree is killed before the
    cancellation goes on up. The output keeps its head and its tail when it
    is longer than the cap."""
    box = sandbox or SandboxConfig()
    reason = sandbox_available(box)
    if reason is not None:
        raise SandboxUnavailable(reason)
    argv, cwd = build_argv(command, root, box)
    tree = await start(argv, cwd)
    if on_start is not None:
        on_start(tree)
    stdout = tree.proc.stdout
    if stdout is None:
        await tree.kill()
        raise OSError("the command has no output pipe")
    head = bytearray()
    tail = bytearray()
    half = max(1, output_max_bytes // 2)
    omitted = 0

    async def drain() -> None:
        nonlocal omitted
        while True:
            chunk = await stdout.read(65536)
            if not chunk:
                return
            room = half - len(head)
            if room > 0:
                head.extend(chunk[:room])
                chunk = chunk[room:]
            if chunk:
                tail.extend(chunk)
                if len(tail) > half:
                    cut = len(tail) - half
                    omitted += cut
                    del tail[:cut]

    timed_out = False
    try:
        await asyncio.wait_for(drain(), timeout_s)
        await tree.proc.wait()
    except TimeoutError:
        timed_out = True
        await tree.kill()
    except asyncio.CancelledError:
        await asyncio.shield(tree.kill())
        raise
    finally:
        tree.close()
    text = _decode(bytes(head))
    if tail:
        text += _decode(bytes(tail))
    return ShellResult(
        exit_code=tree.proc.returncode,
        output=text.rstrip("\n"),
        timed_out=timed_out,
        omitted_bytes=omitted,
    )
