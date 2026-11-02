"""A command's keeper on Linux: runs it as a child subreaper, so nothing it
starts can outlive Stop.

`shell.py` starts this file as a script (`python -I reaper.py -- argv...`)
in a new session, and it starts the command. With PR_SET_CHILD_SUBREAPER a
process the command orphans (a `setsid` or `nohup ... &` child whose
parent has exited, a double-forked daemon) is re-parented here instead of
to init, so it stays a descendant and can be found. The keeper ends every
descendant, whatever its session or process group:

- when the command itself exits (what it left running ends with it), and
- on SIGTERM, SIGINT or SIGHUP (Stop, the timeout, closing the session),
- and when the TUI dies (PR_SET_PDEATHSIG), the way a Windows job object
  with KILL_ON_JOB_CLOSE ends its members.

Each is sent SIGTERM, then SIGKILL after a short grace, again and again
until none is left; a killed parent's children come here and are killed
in the next round. The keeper then exits as the command did: its exit
code, or the signal that ended it.

Only the standard library is used, and the script runs isolated (`-I`), so
nothing in the user's environment or this folder is imported.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import signal
import sys
import time

if not sys.platform.startswith("linux"):
    raise ImportError("the reaper runs on Linux only")

import resource  # noqa: E402 -- POSIX only, after the platform check

PR_SET_PDEATHSIG = 1
PR_SET_CHILD_SUBREAPER = 36
GRACE_S = 0.3
GIVE_UP_S = 10.0
STOP = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
# The parent's death arrives as its own signal, since a thread of the parent
# ending sends it too; that one is told apart by the parent still being there.
PARENT_DIED = signal.SIGUSR1
WATCHED = {signal.SIGCHLD, PARENT_DIED, *STOP}
# Reset in the command: Python ignores SIGPIPE and SIGXFSZ, and a shell
# pipeline (`yes | head`) relies on the default.
DEFAULTS = {*WATCHED, signal.SIGPIPE, signal.SIGXFSZ}


def _processes() -> dict[int, tuple[int, str]]:
    """Every process's parent and state, from /proc."""
    found: dict[int, tuple[int, str]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return found
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        # The command name is in parentheses and may hold anything, spaces
        # and parentheses included: the fields after it follow the last ")".
        fields = data[data.rfind(b")") + 2 :].split()
        if len(fields) < 2:
            continue
        found[int(name)] = (int(fields[1]), fields[0].decode("ascii", "replace"))
    return found


def descendants(pid: int) -> list[int]:
    """The live processes below `pid`, parents before their children."""
    table = _processes()
    children: dict[int, list[int]] = {}
    for child, (parent, _) in table.items():
        children.setdefault(parent, []).append(child)
    out: list[int] = []
    stack = list(children.get(pid, ()))
    while stack:
        current = stack.pop()
        if table.get(current, (0, "Z"))[1] != "Z":
            out.append(current)
        stack.extend(children.get(current, ()))
    return out


class Keeper:
    def __init__(self, command: int) -> None:
        self.command = command
        self.status: int | None = None

    def reap(self) -> bool:
        """Collect every child that has ended; False once none is left."""
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return False
            except InterruptedError:
                continue
            if pid == 0:
                return True
            if pid == self.command:
                self.status = status

    def end_all(self) -> None:
        """End every descendant: SIGTERM, a grace, then SIGKILL until none
        is left."""
        me = os.getpid()
        for pid in descendants(me):
            _signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + GRACE_S
        while time.monotonic() < deadline:
            if not self.reap() or not descendants(me):
                break
            time.sleep(0.02)
        give_up = time.monotonic() + GIVE_UP_S
        while time.monotonic() < give_up:
            alive = descendants(me)
            if not alive:
                break
            for pid in alive:
                _signal(pid, signal.SIGKILL)
            time.sleep(0.01)
            self.reap()
        while self.reap():
            time.sleep(0.01)


def _signal(pid: int, signum: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signum)


def _exit_as(status: int | None) -> None:
    """Exit the way the command did."""
    if status is not None and os.WIFSIGNALED(status):
        signum = os.WTERMSIG(status)
        with contextlib.suppress(OSError, ValueError):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(signum, signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signum})
        os.kill(os.getpid(), signum)
        os._exit(128 + signum)
    if status is not None and os.WIFEXITED(status):
        os._exit(os.WEXITSTATUS(status))
    os._exit(1)


def main(argv: list[str]) -> None:
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        sys.stderr.write("reaper: no command\n")
        os._exit(2)
    signal.pthread_sigmask(signal.SIG_BLOCK, WATCHED)
    parent = os.getppid()
    libc = ctypes.CDLL(None, use_errno=True)
    # Without the subreaper (a kernel before 3.4, a sandbox that forbids
    # prctl) orphans go to init as before; their parents are still ended.
    libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    libc.prctl(PR_SET_PDEATHSIG, int(PARENT_DIED), 0, 0, 0)
    if os.getppid() != parent:
        os._exit(1)  # the TUI was gone before the command could start
    try:
        command = os.posix_spawnp(argv[0], argv, os.environ, setsigmask=(), setsigdef=DEFAULTS)
    except OSError as exc:
        sys.stderr.write(f"{argv[0]}: {exc.strerror or exc}\n")
        os._exit(127)
    keeper = Keeper(command)
    while keeper.status is None:
        info = signal.sigwaitinfo(WATCHED)
        if info.si_signo == signal.SIGCHLD:
            keeper.reap()
        elif info.si_signo == PARENT_DIED and os.getppid() == parent:
            continue  # a thread of the TUI ended, not the TUI
        else:
            break
    keeper.end_all()
    _exit_as(keeper.status)


if __name__ == "__main__":
    main(sys.argv[1:])
