"""The seven contract tools, run on this machine inside the workspace root.

The read tools are the SDK's own (`agent.run_read_file`, `run_list_dir`,
`run_glob`, `run_grep`): every path goes through `agent.confine` first, so a
`..`, an absolute path outside the root, a link out of it or a loop of
links, a denied file and a name Windows would read as another (a stream, a
trailing dot, a device) are refused before anything is read, and search
leaves out what `.gitignore` and `.matterwaveignore` ignore. Their text is
the contract's. grep runs in a process of its own with a deadline
(`grep.py`), so a pattern that backtracks without end cannot freeze the
TUI.

A write is split in two: `prepare_write` reads the file and works out the
new text and its diff (the SDK's `apply_edit` and `unified_diff`), and
`apply_write` writes it. Between the two, the approval shows the diff; a
rejected write never reaches `apply_write`, so the file stays
byte-identical, and `apply_write` writes nothing when the file or where its
path leads changed after the diff was made. A protected path (the agent's
settings and instructions, `.matterwaveignore`, a shell startup file, a
credentials folder) is worked out like any other, marked `protected`, and
written only once the user has approved it: `confine(..., approved=True)`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from matterwave_sdk import agent as contract

from .grep import GREP_TIMEOUT_S, GrepProcess
from .settings import SandboxConfig
from .shell import ProcessTree, SandboxUnavailable, ShellResult, run_command

# edit and write_file hold the old and new text and diff them.
WRITE_MAX_BYTES = 8 * 1024 * 1024
# The search process every LocalTools shares unless it is given its own.
SEARCH = GrepProcess()


@dataclass(frozen=True)
class ToolOutcome:
    """A tool's answer for the model, and whether it is a failure."""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class PendingWrite:
    """A write worked out and not yet made: what the approval shows.
    `old_bytes` is the file as the diff saw it (None: it did not exist).
    `protected`: the path is one the contract protects, so the write asks
    in every mode and is made only with the user's approval."""

    tool: str
    path: contract.ConfinedPath
    old_text: str | None
    new_text: str
    diff: str
    old_bytes: bytes | None = None
    protected: bool = False

    @property
    def relative(self) -> str:
        return self.path.relative

    @property
    def created(self) -> bool:
        return self.old_bytes is None


class ArgumentError(ValueError):
    pass


def parse_arguments(raw: str) -> dict[str, Any]:
    """A call's JSON arguments as an object, or ArgumentError with the
    sentence the model gets back."""
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArgumentError(f"The arguments are not valid JSON ({exc.msg}).") from exc
    if not isinstance(value, dict):
        raise ArgumentError("The arguments must be a JSON object.")
    return value


def _string(args: Mapping[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None:
        if required:
            raise ArgumentError(f"`{key}` is required.")
        return None
    if not isinstance(value, str):
        raise ArgumentError(f"`{key}` must be a string.")
    return value


def _required(args: Mapping[str, Any], key: str) -> str:
    return _string(args, key) or ""


def _line(args: Mapping[str, Any], key: str) -> int | None:
    """A line number as given; its range is the SDK's to judge (`bad_line`)."""
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ArgumentError(f"`{key}` must be an integer.")
    return value


def _load(path: Path) -> bytes:
    """A file's bytes for a write to work from. OSError for one that is not a
    regular file or is too large to diff."""
    if not path.is_file():
        raise OSError(f"{path.name} is not a regular file")
    if path.stat().st_size > WRITE_MAX_BYTES:
        raise OSError(f"it is larger than {WRITE_MAX_BYTES // (1024 * 1024)} MB, too large to change here")
    return path.read_bytes()


def _text(data: bytes) -> str | None:
    """A file's text exactly as stored (CRLF and BOM kept), so the text
    written back after an edit differs from the original only by the edit;
    None for a binary file or one that is not UTF-8."""
    if contract.is_binary(data):
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def write_text(path: Path, text: str) -> None:
    """Replace the file whole, through a temporary file in the same folder,
    so a failure leaves the old content rather than half of the new."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mw-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class LocalTools:
    """The contract's tools over one workspace root. `limits` overrides
    the contract's limits (the conformance cases do); `grep` is the search
    process grep runs in, and `grep_timeout_s` its deadline."""

    def __init__(
        self,
        root: Path,
        *,
        sandbox: SandboxConfig | None = None,
        on_shell_start: Callable[[ProcessTree], None] | None = None,
        limits: Mapping[str, int] | None = None,
        grep: GrepProcess | None = None,
        grep_timeout_s: float = GREP_TIMEOUT_S,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.sandbox = sandbox or SandboxConfig()
        self.on_shell_start = on_shell_start
        self.limits = limits
        self.grep = grep or SEARCH
        self.grep_timeout_s = grep_timeout_s
        # What a command printed before Stop ended it, for the call's result.
        self._stopped: ToolOutcome | None = None

    # --- paths ------------------------------------------------------------------

    def confine(
        self, path: str, *, write: bool = False, approved: bool = False
    ) -> contract.ConfinedPath:
        return contract.confine(self.root, path, write=write, approved=approved)

    def search_ignore(self) -> contract.SearchIgnore:
        """What search leaves out, as of now: the deny list, and the
        .gitignore and .matterwaveignore files from the root down."""
        return contract.SearchIgnore.from_root(self.root)

    # --- reads ------------------------------------------------------------------

    async def read(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        try:
            if name == "grep":
                reply = await self.grep.search(self._grep_request(args))
                return ToolOutcome(str(reply["text"]), is_error=bool(reply["is_error"]))
            return await asyncio.to_thread(self._read, name, args)
        except (ArgumentError, contract.PathRefused, contract.ToolError) as exc:
            return ToolOutcome(str(exc), is_error=True)
        except OSError as exc:
            return ToolOutcome(f"{name} failed: {exc.strerror or exc}.", is_error=True)

    def _grep_request(self, args: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "pattern": _required(args, "pattern"),
            "path": _string(args, "path", required=False) or ".",
            "glob": _string(args, "glob", required=False) or None,
            "ignore_case": args.get("ignore_case") is True,
            "limits": dict(self.limits) if self.limits is not None else None,
            "timeout_s": self.grep_timeout_s,
        }

    def _read(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        limits = self.limits
        if name == "read_file":
            text = contract.run_read_file(
                self.root, _required(args, "path"), _line(args, "start_line"),
                _line(args, "end_line"), limits=limits,
            )
            return ToolOutcome(text)
        path = _string(args, "path", required=False) or "."
        if name == "list_dir":
            return ToolOutcome(
                contract.run_list_dir(self.root, path, ignore=self.search_ignore(), limits=limits)
            )
        if name == "glob":
            return ToolOutcome(
                contract.run_glob(
                    self.root, _required(args, "pattern"), path, ignore=self.search_ignore(),
                    limits=limits,
                )
            )
        raise ArgumentError(f"{name} is not a read tool.")

    # --- writes -----------------------------------------------------------------

    def prepare_write(self, name: str, args: Mapping[str, Any]) -> PendingWrite | ToolOutcome:
        """The write a call asks for, worked out but not made; or the
        refusal or error to answer with instead."""
        try:
            if name == "edit":
                return self._prepare_edit(args)
            return self._prepare_write_file(args)
        except (ArgumentError, contract.PathRefused, contract.EditError) as exc:
            return ToolOutcome(str(exc), is_error=True)
        except OSError as exc:
            return ToolOutcome(f"{name} failed: {exc.strerror or exc}.", is_error=True)

    def _target(self, path: str) -> tuple[contract.ConfinedPath, bool]:
        """Where a write to `path` leads, and whether it is protected. The
        contract's `confine` decides both: a protected path is refused
        until approved, so it is confined again as approved, to be asked
        about."""
        try:
            return self.confine(path, write=True), False
        except contract.PathRefused as exc:
            if exc.reason != "protected":
                raise
        return self.confine(path, write=True, approved=True), True

    def _prepare_edit(self, args: Mapping[str, Any]) -> PendingWrite | ToolOutcome:
        path = _required(args, "path")
        old_string = _required(args, "old_string")
        new_string = _required(args, "new_string")
        target, protected = self._target(path)
        if not target.absolute.exists():
            raise contract.EditError("missing", path)
        if target.absolute.is_dir():
            return ToolOutcome(f"{path} is a directory.", is_error=True)
        data = _load(target.absolute)
        old = _text(data)
        if old is None:
            return ToolOutcome(f"{path} is not UTF-8 text, so it cannot be edited.", is_error=True)
        new = contract.apply_edit(old, old_string, new_string, path=path)
        diff = contract.unified_diff(old, new, target.relative)
        return PendingWrite("edit", target, old, new, diff, data, protected)

    def _prepare_write_file(self, args: Mapping[str, Any]) -> PendingWrite | ToolOutcome:
        path = _required(args, "path")
        content = _required(args, "content")
        target, protected = self._target(path)
        if target.absolute.is_dir():
            return ToolOutcome(f"{path} is a directory.", is_error=True)
        if len(content.encode("utf-8")) > WRITE_MAX_BYTES:
            return ToolOutcome(f"The content for {path} is too large to write here.", is_error=True)
        if not target.absolute.exists():
            diff = contract.unified_diff("", content, target.relative)
            diff = diff.replace(f"--- a/{target.relative}", "--- /dev/null", 1)
            return PendingWrite("write_file", target, None, content, diff, None, protected)
        data = _load(target.absolute)
        old = _text(data)
        if old is None:
            # The old bytes cannot be shown as text, but what replaces them
            # is: the approval shows every line that will be written.
            diff = (
                f"(the current {target.relative} is binary or not UTF-8 text; "
                "it is replaced whole by the text below)\n"
                + contract.unified_diff("", content, target.relative)
            )
            return PendingWrite("write_file", target, None, content, diff, data, protected)
        if old == content:
            return ToolOutcome(f"{path} already has exactly this content; nothing to write.")
        diff = contract.unified_diff(old, content, target.relative)
        return PendingWrite("write_file", target, old, content, diff, data, protected)

    def stale(self, pending: PendingWrite) -> ToolOutcome | None:
        """Why the write must not be made after all, or None. The diff was
        approved for the file as it was and the place its path led then; a
        change to either since (another program, a command, a link swapped
        in) means the approval no longer describes the write. A protected
        path is confined as approved only when it was protected, and so
        asked about, all along."""
        try:
            again = self.confine(pending.relative, write=True, approved=pending.protected)
        except contract.PathRefused as exc:
            return ToolOutcome(f"{pending.relative} was not written: {exc}", is_error=True)
        if os.path.normcase(str(again.absolute)) != os.path.normcase(str(pending.path.absolute)):
            return ToolOutcome(
                f"{pending.relative} was not written: where its path leads changed after the "
                "change was worked out (a link was added or moved). Read it again and redo the change.",
                is_error=True,
            )
        try:
            current = again.absolute.read_bytes() if os.path.lexists(again.absolute) else None
        except OSError as exc:
            return ToolOutcome(
                f"{pending.relative} could not be checked: {exc.strerror or exc}.", is_error=True
            )
        if current != pending.old_bytes:
            return ToolOutcome(
                f"{pending.relative} was not written: it changed after the diff was made, so the "
                "diff is not what would be written. Read it again and redo the change.",
                is_error=True,
            )
        return None

    def apply_write(self, pending: PendingWrite) -> ToolOutcome:
        refused = self.stale(pending)
        if refused is not None:
            return refused
        try:
            write_text(pending.path.absolute, pending.new_text)
        except OSError as exc:
            return ToolOutcome(
                f"{pending.relative} could not be written: {exc.strerror or exc}.", is_error=True
            )
        if pending.created:
            return ToolOutcome(f"Created {pending.relative}.")
        verb = "Edited" if pending.tool == "edit" else "Wrote"
        return ToolOutcome(f"{verb} {pending.relative}.")

    # --- the shell --------------------------------------------------------------

    def shell_args(self, args: Mapping[str, Any]) -> tuple[str, int]:
        command = _string(args, "command") or ""
        if not command.strip():
            raise ArgumentError("`command` is empty.")
        timeout = args.get("timeout_s")
        if timeout is None:
            return command, contract.limit("shell_timeout_s_default", self.limits)
        most = contract.limit("shell_timeout_s_max", self.limits)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= most:
            raise contract.ToolError("bad_timeout", max=most)
        return command, timeout

    def take_stopped(self) -> ToolOutcome | None:
        """The result of the command Stop ended, once."""
        stopped, self._stopped = self._stopped, None
        return stopped

    async def run_shell(self, command: str, timeout_s: int) -> ToolOutcome:
        def stopped(result: ShellResult) -> None:
            self._stopped = ToolOutcome(result.render(timeout_s, limits=self.limits), is_error=True)

        self._stopped = None
        try:
            result = await run_command(
                command,
                self.root,
                timeout_s=timeout_s,
                output_max_bytes=contract.limit("shell_output_max_bytes", self.limits),
                sandbox=self.sandbox,
                on_start=self.on_shell_start,
                on_stop=stopped,
            )
        except SandboxUnavailable as exc:
            return ToolOutcome(
                f"Refused: the sandbox is on but cannot run here ({exc}); the command was not run.",
                is_error=True,
            )
        except OSError as exc:
            return ToolOutcome(f"The command could not be started: {exc}.", is_error=True)
        return ToolOutcome(
            result.render(timeout_s, limits=self.limits),
            is_error=result.timed_out or result.exit_code != 0,
        )
