"""grep in a Python process of its own, so a pattern that never ends cannot
freeze the TUI.

CPython's `re` holds the interpreter for the whole of one match. A pattern
that backtracks exponentially on one line (`(a+)+$` against a long run of
`a`s that ends in something else) keeps every thread waiting, the screen's
and Stop's included, and the SDK's `run_grep(timeout_s=)` cannot end it:
its deadline is checked between lines. So the model's grep runs the SDK's
`run_grep` in a Python process beside the TUI's. A search over many files
ends itself at the deadline with the contract's `timeout` error, and the
process stays; one stuck in a single match has not answered a moment
later, so it is killed and the call answers with the same error. Stop
kills it too.

The process starts with the first search and is kept for the next
(starting one costs most of a second: the SDK's import); after a kill, the
next search starts another. It reads one JSON request per line and writes
one JSON answer per line. It ends when its input closes (the TUI closed it
or died), and on its own a little after a search's deadline, so a search
left running by a TUI that died still ends.

Where no process can be started (no Python to run, a frozen build), the
search runs on a thread here with the SDK's deadline, which bounds a slow
search but not one catastrophic line.
"""

from __future__ import annotations

import asyncio
import contextlib
import faulthandler
import json
import queue
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import IO, Any

from matterwave_sdk import agent as contract

# How long one search may take, in seconds.
GREP_TIMEOUT_S = 30.0
# How long past its deadline a search that has not answered is given before
# its process is killed (it is stuck inside one match).
KILL_AFTER_S = 1.0
# How long a new process gets to import the SDK and say it is ready.
START_TIMEOUT_S = 30.0
READY = b"ready"


def answer(request: Mapping[str, Any]) -> dict[str, Any]:
    """One search, as the grep tool answers it: its text, and whether that
    is an error."""
    root = request["root"]
    try:
        text = contract.run_grep(
            root,
            request["pattern"],
            request["path"],
            glob=request.get("glob"),
            ignore_case=bool(request.get("ignore_case")),
            ignore=contract.SearchIgnore.from_root(root),
            limits=request.get("limits"),
            timeout_s=request["timeout_s"],
        )
    except (contract.PathRefused, contract.ToolError) as exc:
        return {"text": str(exc), "is_error": True}
    except OSError as exc:
        return {"text": f"grep failed: {exc.strerror or exc}.", "is_error": True}
    return {"text": text, "is_error": False}


def serve(requests: IO[bytes], answers: IO[bytes]) -> None:
    """The search process: an answer for each request, until the input ends."""
    answers.write(READY + b"\n")
    answers.flush()
    for line in requests:
        request = json.loads(line)
        # Ends this process when no one is left to kill it: a C thread,
        # which needs no turn on the interpreter that a match is holding.
        faulthandler.dump_traceback_later(request["timeout_s"] + 2 * KILL_AFTER_S, exit=True)
        reply = answer(request)
        faulthandler.cancel_dump_traceback_later()
        answers.write(json.dumps(reply).encode("ascii") + b"\n")
        answers.flush()


def _read_answers(stream: IO[bytes], answers: queue.Queue[bytes]) -> None:
    """Every line the process writes, then b"" once it has ended."""
    try:
        with contextlib.suppress(OSError, ValueError):
            for line in stream:
                answers.put(line)
    finally:
        answers.put(b"")
        with contextlib.suppress(OSError):
            stream.close()


@dataclass(frozen=True)
class _Live:
    """A started search process: where its requests go and its answers come."""

    proc: subprocess.Popen[bytes]
    requests: IO[bytes]
    answers: queue.Queue[bytes]

    def end(self) -> None:
        with contextlib.suppress(OSError):
            self.proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)
        with contextlib.suppress(OSError):
            self.requests.close()


class GrepProcess:
    """The search process, started when a search first needs it and kept.
    Searches take turns; each waits for its answer no longer than its own
    deadline (and the moment after it), however long it waited its turn."""

    def __init__(self, command: Sequence[str] | None = None) -> None:
        self.command = (
            list(command) if command is not None else [sys.executable, "-P", "-m", __name__]
        )
        self._turn = threading.Lock()
        self._state = threading.Lock()
        self._live: _Live | None = None
        # The search waiting on the process now, and that process.
        self._running: tuple[threading.Event, _Live] | None = None
        self._unavailable = False

    @property
    def pid(self) -> int | None:
        """The live process's id, if there is one."""
        live = self._live
        return live.proc.pid if live is not None and live.proc.poll() is None else None

    async def search(self, request: Mapping[str, Any]) -> dict[str, Any]:
        """`answer(request)`, from the process. Cancelled (Stop), the
        process is killed if it is this search's."""
        abandoned = threading.Event()
        try:
            return await asyncio.to_thread(self._search, request, abandoned)
        except asyncio.CancelledError:
            abandoned.set()
            with self._state:
                running = self._running
            if running is not None and running[0] is abandoned:
                self._kill(running[1])
            raise

    def close(self) -> None:
        """End the process; the next search starts another."""
        with self._state:
            live = self._live
        if live is not None:
            self._kill(live)

    def _search(self, request: Mapping[str, Any], abandoned: threading.Event) -> dict[str, Any]:
        with self._turn:
            if abandoned.is_set():
                return {"text": "grep was stopped.", "is_error": True}
            live = self._started()
            if live is None:
                return answer(request)
            with self._state:
                self._running = (abandoned, live)
            try:
                return self._ask(live, request)
            finally:
                with self._state:
                    self._running = None

    def _ask(self, live: _Live, request: Mapping[str, Any]) -> dict[str, Any]:
        timeout_s = float(request["timeout_s"])
        try:
            live.requests.write(json.dumps(dict(request)).encode("ascii") + b"\n")
            live.requests.flush()
            line = live.answers.get(timeout=timeout_s + KILL_AFTER_S)
        except queue.Empty:
            self._kill(live)
            raise contract.ToolError(
                "timeout", pattern=request["pattern"], timeout_s=f"{timeout_s:g}"
            ) from None
        except OSError:
            line = b""
        if not line:
            self._kill(live)
            return {
                "text": "grep failed: the search process ended without an answer.",
                "is_error": True,
            }
        reply: dict[str, Any] = json.loads(line)
        return reply

    def _started(self) -> _Live | None:
        """The live process, started if need be; None when no process can
        be started here."""
        with self._state:
            live = self._live
        if live is not None:
            if live.proc.poll() is None:
                return live
            self._kill(live)
        if self._unavailable:
            return None
        try:
            proc = subprocess.Popen(  # noqa: S603 -- this interpreter, this module
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError:
            self._unavailable = True
            return None
        if proc.stdin is None or proc.stdout is None:  # not with PIPE; for the types
            proc.kill()
            self._unavailable = True
            return None
        live = _Live(proc, proc.stdin, queue.Queue())
        threading.Thread(
            target=_read_answers,
            args=(proc.stdout, live.answers),
            name="matterwave-grep",
            daemon=True,
        ).start()
        try:
            ready = live.answers.get(timeout=START_TIMEOUT_S)
        except queue.Empty:
            ready = b""
        if ready.strip() != READY:
            live.end()
            self._unavailable = True
            return None
        with self._state:
            self._live = live
        return live

    def _kill(self, live: _Live) -> None:
        with self._state:
            if self._live is live:
                self._live = None
        live.end()


if __name__ == "__main__":
    serve(sys.stdin.buffer, sys.stdout.buffer)
