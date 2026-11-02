"""`run_shell`: one command in the workspace root, with a timeout, an
output cap and a whole-tree kill.

The shell is the contract's (`agent.shell_for`): `/bin/sh -c` on POSIX,
PowerShell on Windows. Its result is the contract's text
(`agent.format_shell`): `Exit code N`, then the output, its middle cut out
past the cap. Stopping a command stops everything it started, including
what it detached (`cmd /c start /b`, `Start-Process`, `setsid`,
`nohup ... &`):

- Windows: the command starts suspended, goes into a job object that kills
  its members when closed (KILL_ON_JOB_CLOSE), and only then runs, so
  everything it starts is in the job from its first instruction. Stop
  terminates the job; if the TUI itself dies, closing the handle ends
  them. `taskkill /T /F` is the fallback when a job cannot be made.
- Linux: the command runs under a keeper (`reaper.py`) in a new session.
  The keeper is a child subreaper (PR_SET_CHILD_SUBREAPER), so an orphaned
  descendant comes back to it rather than to init, and on Stop, at the
  command's end or at the TUI's death it ends every descendant, whatever
  its session or process group.
- macOS and other POSIX systems, which have no subreaper: a new session;
  Stop signals the process group and every live descendant found by
  walking the process table. A daemon whose parent has already exited
  belongs to launchd by then and is not found; only the container sandbox
  contains that.

When the command itself exits, the output still in the pipe is read for a
moment and what it left running ends with it, so a background process that
holds the output open cannot keep the call going until the timeout. A
program handed to another service (a scheduled task, WMI, a system
service, `launchctl`) is not a descendant on any platform.

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
import codecs
import contextlib
import functools
import locale
import os
import secrets
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matterwave_sdk import agent as contract

from .settings import SandboxConfig

KILL_GRACE_S = 0.3
# After the command exits: how long the output still in the pipe is read
# before what it left running is ended.
DRAIN_GRACE_S = 0.5
EXIT_POLL_S = 0.05
# How long the Linux keeper gets to end everything before it is killed.
KEEPER_WAIT_S = 5.0
SCRUBBED_ENV = ("MATTERWAVE_API_KEY",)
CONTAINER_ROOT = "/workspace"
SANDBOX_ENGINES = ("none", "podman", "docker", "bwrap", "seatbelt")
READ_ONLY_FOLDERS = (".git", ".matterwave")
REAPER = Path(__file__).with_name("reaper.py")


# --- the output and the result ---------------------------------------------------


def _decode(data: bytes) -> str:
    """A command's bytes as text: UTF-8, else the system's code page (a
    Windows console program writes in it)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode(locale.getpreferredencoding(False) or "utf-8", errors="replace")


def _decode_head(data: bytes) -> tuple[str, int]:
    """The start of a stream as text, a character cut off at its end left
    out, and how many of the bytes that text took."""
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        text = decoder.decode(data, final=False)
    except UnicodeDecodeError:
        return _decode(data), len(data)
    return text, len(data) - len(decoder.getstate()[0])


def _decode_tail(data: bytes) -> tuple[str, int]:
    """The end of a stream as text, a character cut off at its start left
    out, and how many of the bytes that text took."""
    start = 0
    while start < min(3, len(data)) and (data[start] & 0xC0) == 0x80:
        start += 1
    return _decode(data[start:]), len(data) - start


class OutputBuffer:
    """A command's output as it arrives, held to the cap: the whole of it
    when it fits, else only its head and its tail (a few bytes more than
    the contract shows of each, so a character cut at a boundary can be
    finished or left out), and the count of everything."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self._head_room = cap // 2 + 3
        self._tail_room = cap - cap // 2 + 3
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self._head_room - len(self.head)
        if room > 0:
            self.head.extend(chunk[:room])
            chunk = chunk[room:]
        if chunk:
            self.tail.extend(chunk)
            if len(self.tail) > self._tail_room:
                del self.tail[: len(self.tail) - self._tail_room]

    def result(
        self, exit_code: int | None, *, timed_out: bool = False, stopped: bool = False
    ) -> ShellResult:
        if self.total == len(self.head) + len(self.tail):
            text = _decode(bytes(self.head + self.tail))
            return ShellResult(exit_code, text, timed_out=timed_out, stopped=stopped, cap=self.cap)
        head, head_bytes = _decode_head(bytes(self.head))
        tail, tail_bytes = _decode_tail(bytes(self.tail))
        return ShellResult(
            exit_code,
            head + tail,
            timed_out=timed_out,
            stopped=stopped,
            omitted_bytes=self.total - head_bytes - tail_bytes,
            cap=self.cap,
        )


@dataclass(frozen=True)
class ShellResult:
    """A command's end and its output: the whole output, or, when it was
    over the cap, its start and its end joined (`output`) and the bytes
    dropped from between them (`omitted_bytes`). `exit_code` is None for a
    command ended by a signal. `cap` is the output cap the buffer kept it
    to; the result is cut at that cap, whatever `limits` says."""

    exit_code: int | None
    output: str
    timed_out: bool = False
    stopped: bool = False
    omitted_bytes: int = 0
    cap: int | None = None

    def render(
        self, timeout_s: float | None = None, *, limits: Mapping[str, int] | None = None
    ) -> str:
        """The tool result, as the contract words it: the contract makes
        the cut, from the start and the end that were kept."""
        if self.cap is not None:
            limits = {**(limits or {}), "shell_output_max_bytes": self.cap}
        return contract.format_shell(
            self.output,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            stopped=self.stopped,
            timeout_s=timeout_s,
            limits=limits,
            omitted=self.omitted_bytes,
        )


def exit_code_of(returncode: int | None) -> int | None:
    """The exit code as the contract reports it: None for an end by a
    signal (a negative return code on POSIX)."""
    if returncode is None or (returncode < 0 and sys.platform != "win32"):
        return None
    return returncode


# --- the sandbox -----------------------------------------------------------------


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


def _quoted(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _seatbelt_profile(root: str, read_only: list[str], network: bool) -> str:
    rules = ["(version 1)", "(allow default)", "(deny file-write*)"]
    if not network:
        rules.append("(deny network*)")
    rules.append(
        f'(allow file-write* (subpath {_quoted(root)}) (literal "/dev/null") (literal "/dev/tty"))'
    )
    rules.extend(f"(deny file-write* (subpath {_quoted(path)}))" for path in read_only)
    return "".join(rules)


def read_only_folders(root: Path) -> list[str]:
    """The folders under the root a sandboxed command may read but not
    change: the repository's `.git` (its hooks run later, outside any
    sandbox) and the agent's own `.matterwave` (its settings can start
    programs)."""
    return [name for name in READ_ONLY_FOLDERS if (root / name).is_dir()]


def build_argv(
    command: str, root: Path, sandbox: SandboxConfig, *, name: str | None = None
) -> tuple[list[str], str]:
    """The argv to start and the working directory to start it in. `name`
    names a container, so Stop can remove it as well as its client."""
    if sandbox.engine in ("podman", "docker"):
        # --init: the container's PID 1 passes a stop on to the command.
        argv = [sandbox.engine, "run", "--rm", "-i", "--init", "--read-only"]
        if name:
            argv += ["--name", name]
        if sandbox.engine == "podman":
            # Podman mounts a writable /tmp on a read-only container unless told not to.
            argv.append("--read-only-tmpfs=false")
        if not sandbox.network:
            argv.append("--network=none")
        argv += ["-v", f"{root}:{CONTAINER_ROOT}:rw"]
        for folder in read_only_folders(root):
            argv += ["-v", f"{root / folder}:{CONTAINER_ROOT}/{folder}:ro"]
        argv += ["-w", CONTAINER_ROOT, sandbox.image, "/bin/sh", "-c", command]
        return argv, str(root)
    if sandbox.engine == "bwrap":
        argv = [
            "bwrap",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--bind",
            str(root),
            str(root),
            "--chdir",
            str(root),
            "--die-with-parent",
        ]
        for folder in read_only_folders(root):
            argv += ["--ro-bind", str(root / folder), str(root / folder)]
        if not sandbox.network:
            argv.append("--unshare-net")
        return [*argv, "/bin/sh", "-c", command], str(root)
    if sandbox.engine == "seatbelt":
        real = os.path.realpath(root)
        profile = _seatbelt_profile(
            real, [os.path.join(real, f) for f in read_only_folders(root)], sandbox.network
        )
        return ["sandbox-exec", "-p", profile, "/bin/sh", "-c", command], str(root)
    _, prefix = contract.shell_for()
    return [*prefix, command], str(root)


def child_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUBBED_ENV}
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


# --- Windows: a job object made before the command runs -------------------------------

CREATE_SUSPENDED = 0x00000004
_KILL_ON_JOB_CLOSE = 0x2000
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_THREAD_SUSPEND_RESUME = 0x0002
_SNAPTHREAD = 0x00000004


@functools.cache
def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    handle, dword, boolean = wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL
    for name, restype, argtypes in (
        ("CreateJobObjectW", handle, [ctypes.c_void_p, wintypes.LPCWSTR]),
        ("SetInformationJobObject", boolean, [handle, ctypes.c_int, ctypes.c_void_p, dword]),
        ("AssignProcessToJobObject", boolean, [handle, handle]),
        ("TerminateJobObject", boolean, [handle, ctypes.c_uint]),
        ("OpenProcess", handle, [dword, boolean, dword]),
        ("OpenThread", handle, [dword, boolean, dword]),
        ("ResumeThread", dword, [handle]),
        ("CloseHandle", boolean, [handle]),
        ("CreateToolhelp32Snapshot", handle, [dword, dword]),
        ("Thread32First", boolean, [handle, ctypes.c_void_p]),
        ("Thread32Next", boolean, [handle, ctypes.c_void_p]),
        ("IsProcessInJob", boolean, [handle, handle, ctypes.POINTER(boolean)]),
    ):
        function = getattr(kernel32, name)
        function.restype = restype
        function.argtypes = argtypes
    return kernel32


class _WindowsJob:
    """A job object that kills its members when it is closed or terminated."""

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()

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
            _fields_ = [
                (n, ctypes.c_uint64)
                for n in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class _Extended(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _Basic),
                ("IoInfo", _Io),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise OSError(f"CreateJobObjectW failed ({ctypes.get_last_error()})")  # type: ignore[attr-defined,unused-ignore]
        info = _Extended()
        # No BREAKAWAY_OK: a member cannot start a process outside the job.
        info.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(info), ctypes.sizeof(info)
        ):
            kernel32.CloseHandle(job)
            raise OSError("SetInformationJobObject failed")
        self._kernel32 = kernel32
        self._handle: int | None = job

    def assign(self, pid: int) -> None:
        if self._handle is None:
            raise OSError("the job is closed")
        process = self._kernel32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_SET_QUOTA, False, pid)
        if not process:
            raise OSError("OpenProcess failed")
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                raise OSError("AssignProcessToJobObject failed")
        finally:
            self._kernel32.CloseHandle(process)

    def holds(self, pid: int) -> bool:
        """Whether the process `pid` is in this job (for the tests)."""
        import ctypes
        from ctypes import wintypes

        if self._handle is None:
            return False
        process = self._kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            return False
        try:
            result = wintypes.BOOL()
            self._kernel32.IsProcessInJob(process, self._handle, ctypes.byref(result))
            return bool(result.value)
        finally:
            self._kernel32.CloseHandle(process)

    def terminate(self) -> None:
        if self._handle is not None:
            self._kernel32.TerminateJobObject(self._handle, 1)

    def close(self) -> None:
        if self._handle is not None:
            self._kernel32.CloseHandle(self._handle)
            self._handle = None


def resume_process(pid: int) -> None:
    """Let a process started with CREATE_SUSPENDED run: resume its threads
    (a new process has the one). OSError when none could be resumed."""
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()

    class _ThreadEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ThreadID", wintypes.DWORD),
            ("th32OwnerProcessID", wintypes.DWORD),
            ("tpBasePri", wintypes.LONG),
            ("tpDeltaPri", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
        ]

    snapshot = kernel32.CreateToolhelp32Snapshot(_SNAPTHREAD, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        raise OSError("CreateToolhelp32Snapshot failed")
    resumed = 0
    try:
        entry = _ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        more = kernel32.Thread32First(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32OwnerProcessID == pid:
                thread = kernel32.OpenThread(_THREAD_SUSPEND_RESUME, False, entry.th32ThreadID)
                if thread:
                    try:
                        if kernel32.ResumeThread(thread) != 0xFFFFFFFF:
                            resumed += 1
                    finally:
                        kernel32.CloseHandle(thread)
            more = kernel32.Thread32Next(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    if not resumed:
        raise OSError("the command's thread could not be resumed")


# --- POSIX: the process table -------------------------------------------------------


def _posix_table() -> dict[int, int]:
    """Each live process's parent: from /proc on Linux, from `ps` elsewhere."""
    table: dict[int, int] = {}
    if os.path.isdir("/proc/self"):
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/stat", "rb") as handle:
                    data = handle.read()
            except OSError:
                continue
            fields = data[data.rfind(b")") + 2 :].split()
            if len(fields) >= 2 and fields[0] != b"Z":
                table[int(name)] = int(fields[1])
        return table
    try:
        listing = subprocess.run(  # noqa: S603 -- a fixed argv
            ["ps", "-A", "-o", "pid=,ppid=,stat="],  # noqa: S607 -- ps from PATH, as any shell finds it
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return table
    for line in listing.splitlines():
        parts = line.split()
        if (
            len(parts) >= 3
            and parts[0].isdigit()
            and parts[1].isdigit()
            and not parts[2].startswith("Z")
        ):
            table[int(parts[0])] = int(parts[1])
    return table


def descendants(pid: int) -> list[int]:
    """The live processes below `pid`, parents before their children."""
    table = _posix_table()
    children: dict[int, list[int]] = {}
    for child, parent in table.items():
        children.setdefault(parent, []).append(child)
    out: list[int] = []
    stack = list(children.get(pid, ()))
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(children.get(current, ()))
    return out


def _uses_keeper() -> bool:
    return (
        sys.platform.startswith("linux")
        and bool(sys.executable)
        and not getattr(sys, "frozen", False)
    )


def _signal_all(pids: list[int], signum: int) -> None:
    for pid in pids:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signum)


# --- the process tree --------------------------------------------------------------


class ProcessTree:
    """A started command and the means to end it with everything below it.
    `container` (engine, name) is a sandbox container to remove as well:
    killing a container's client does not always stop the container.
    `keeper`: the process is the Linux keeper (`reaper.py`) around it."""

    def __init__(
        self,
        proc: asyncio.subprocess.Process,
        container: tuple[str, str] | None = None,
        *,
        job: _WindowsJob | None = None,
        keeper: bool = False,
    ) -> None:
        self.proc = proc
        self.container = container
        self.keeper = keeper
        self._job = job

    @property
    def pid(self) -> int:
        return self.proc.pid

    def in_job(self, pid: int) -> bool:
        """Whether the process `pid` is in this tree's job (Windows)."""
        return self._job is not None and self._job.holds(pid)

    async def kill(self, *, remove_container: bool = True) -> None:
        """End the command and everything below it, and wait for the
        command. The tree is ended even when the shell itself has exited:
        a child it started in the background is still in it."""
        if sys.platform == "win32":
            if self._job is not None:
                self._job.terminate()
            elif self.proc.returncode is None:
                await _taskkill(self.proc.pid)
        elif self.keeper:
            await self._end_keeper()
        else:
            await self._end_group()
        with contextlib.suppress(ProcessLookupError):
            if self.proc.returncode is None:
                self.proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), 5)
        self.close()
        if self.container is not None and remove_container:
            engine, name = self.container
            await _remove_container(engine, name)

    async def _end_keeper(self) -> None:
        """Ask the keeper to end everything below it; if it does not, kill
        its session's group and whatever is still found below it."""
        if sys.platform == "win32" or self.proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(self.proc.pid, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), KEEPER_WAIT_S)
        if self.proc.returncode is None:
            below = await asyncio.to_thread(descendants, self.proc.pid)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.proc.pid, signal.SIGKILL)
            _signal_all(below, signal.SIGKILL)

    async def _end_group(self) -> None:
        """Signal the session's process group and every descendant still
        attached, SIGTERM and then SIGKILL. The descendants are found while
        their parents live: a process whose parent has exited is no longer
        one."""
        if sys.platform == "win32":
            return
        below = await asyncio.to_thread(descendants, self.proc.pid)
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGTERM)
        _signal_all(below, signal.SIGTERM)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self.proc.wait(), KILL_GRACE_S)
        below += [p for p in await asyncio.to_thread(descendants, self.proc.pid) if p not in below]
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        _signal_all(below, signal.SIGKILL)

    def close(self) -> None:
        if self._job is not None:
            self._job.close()
            self._job = None


async def _taskkill(pid: int) -> None:
    with contextlib.suppress(OSError):
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/T",
            "/F",
            "/PID",
            str(pid),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()


async def _remove_container(engine: str, name: str) -> None:
    with contextlib.suppress(OSError, TimeoutError):
        remover = await asyncio.create_subprocess_exec(
            engine,
            "rm",
            "-f",
            name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(remover.wait(), 15)


async def spawn(
    argv: list[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    stdin: int | None,
    stdout: int | None,
    stderr: int | None,
    container: tuple[str, str] | None = None,
    limit: int | None = None,
) -> ProcessTree:
    """Start `argv` as a process tree that can be ended whole: in a job
    made before it runs (Windows), under the keeper (Linux), or leading a
    new session (other POSIX)."""
    kwargs: dict[str, Any] = {}
    if limit is not None:
        kwargs["limit"] = limit
    keeper = False
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED
    else:
        kwargs["start_new_session"] = True
        if _uses_keeper():
            argv = [sys.executable, "-I", str(REAPER), "--", *argv]
            keeper = True
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=cwd, env=dict(env), stdin=stdin, stdout=stdout, stderr=stderr, **kwargs
    )
    if sys.platform != "win32":
        return ProcessTree(proc, container, keeper=keeper)
    job: _WindowsJob | None = None
    try:
        job = _WindowsJob()
        job.assign(proc.pid)
    except OSError:
        # The TUI may itself be in a job that forbids another; Stop then
        # falls back to taskkill, which finds only attached children.
        if job is not None:
            job.close()
        job = None
    try:
        resume_process(proc.pid)
    except OSError:
        if job is not None:
            job.terminate()
            job.close()
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    return ProcessTree(proc, container, job=job)


async def start(argv: list[str], cwd: str, container: tuple[str, str] | None = None) -> ProcessTree:
    """A command for run_shell: no input, its output and errors in one pipe."""
    return await spawn(
        argv,
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        container=container,
    )


async def run_command(
    command: str,
    root: Path,
    *,
    timeout_s: float,
    output_max_bytes: int,
    sandbox: SandboxConfig | None = None,
    on_start: Callable[[ProcessTree], None] | None = None,
    on_stop: Callable[[ShellResult], None] | None = None,
) -> ShellResult:
    """Run `command` in `root` to its end, its timeout, or its caller's
    cancellation. Cancelled (Stop), the whole tree is ended, `on_stop` gets
    what the command printed so far, and the cancellation goes on up. The
    output keeps its head and its tail when it is longer than the cap."""
    box = sandbox or SandboxConfig()
    reason = sandbox_available(box)
    if reason is not None:
        raise SandboxUnavailable(reason)
    container: tuple[str, str] | None = None
    if box.engine in ("podman", "docker"):
        container = (box.engine, f"mw-agent-{secrets.token_hex(6)}")
    argv, cwd = build_argv(command, root, box, name=container[1] if container else None)
    tree = await start(argv, cwd, container)
    if on_start is not None:
        on_start(tree)
    stdout = tree.proc.stdout
    if stdout is None:
        await tree.kill()
        raise OSError("the command has no output pipe")
    output = OutputBuffer(output_max_bytes)

    async def drain() -> None:
        with contextlib.suppress(OSError):
            while chunk := await stdout.read(65536):
                output.feed(chunk)

    reading = asyncio.create_task(drain())

    async def finish() -> None:
        # The command ends when its output does (the usual case), or when it
        # exits while something it started still holds the pipe open.
        # (`proc.wait()` alone would wait for the pipe as well.)
        while not reading.done() and tree.proc.returncode is None:
            await asyncio.wait({reading}, timeout=EXIT_POLL_S)
        if reading.done():
            await tree.proc.wait()
        else:
            # What is still in the pipe is read for a moment.
            await asyncio.wait({reading}, timeout=DRAIN_GRACE_S)
        # What it left running ends with it. (A container that exited
        # removed itself.)
        await tree.kill(remove_container=False)

    timed_out = False
    try:
        await asyncio.wait_for(finish(), timeout_s)
    except TimeoutError:
        timed_out = True
        await tree.kill()
    except asyncio.CancelledError:
        await asyncio.shield(tree.kill())
        await _settle(reading)
        if on_stop is not None:
            on_stop(output.result(None, stopped=True))
        raise
    finally:
        tree.close()
    await _settle(reading)
    return output.result(exit_code_of(tree.proc.returncode), timed_out=timed_out)


async def _settle(reading: asyncio.Task[None]) -> None:
    """Let the reader take what the ended tree left in the pipe; a pipe
    still held open by something outside the tree is given up on."""
    done, _ = await asyncio.wait({reading}, timeout=2)
    if not done:
        reading.cancel()
        await asyncio.wait({reading})
