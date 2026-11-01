"""The seven contract tools, run on this machine inside the workspace root.

Every path goes through the SDK's `confine` first, so a `..`, an absolute
path outside the root, a link out of it and a denied file are refused
before anything is read or written. On Windows a name the system would read
as something else is refused too (`confine_path`). A write is split in two:
`prepare` reads the file and works out the new text and its diff (the SDK's
`apply_edit` and `unified_diff`), and `apply` writes it. Between the two,
the approval shows the diff; a rejected write never reaches `apply`, so the
file stays byte-identical, and `apply` writes nothing when the file or where
its path leads changed after the diff was made.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from matterwave_sdk import agent as contract

from .ignore import SearchFilter
from .settings import SandboxConfig
from .shell import ProcessTree, SandboxUnavailable, run_command

LIMITS = contract.LIMITS
BINARY_SNIFF_BYTES = 8192
GREP_FILE_MAX_BYTES = 4 * 1024 * 1024
GREP_LINE_MAX_CHARS = 500
# A line longer than this is shown cut: a minified bundle is one line.
READ_LINE_MAX_BYTES = 32 * 1024
# Past this, the lines after a read window are not counted to the end.
COUNT_MAX_BYTES = 256 * 1024 * 1024
# edit and write_file hold the old and new text and diff them.
WRITE_MAX_BYTES = 8 * 1024 * 1024
_GLOB_FLAGS = re.IGNORECASE if sys.platform in ("win32", "darwin") else 0

_DRIVE = re.compile(r"^[A-Za-z]:")
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$|clock\$)$", re.IGNORECASE)


def windows_name_problem(path: str) -> str | None:
    """Why Windows would not take `path` at its word, or None. A `:` after
    the drive names an NTFS stream (`key.pem::$DATA` is the file's own
    bytes, `dir::$INDEX_ALLOCATION` makes a folder); a trailing dot or space
    is dropped (`.git ` is `.git`); a device name (`CON`, `NUL.txt`) is a
    device, not a file. Each would let one name stand for another past the
    deny list and the agent's own-files check."""
    rest = path[2:] if _DRIVE.match(path) else path
    for segment in re.split(r"[\\/]", rest):
        if segment in ("", ".", ".."):
            continue
        if ":" in segment:
            return "names an NTFS stream (a `:` in a name), not a file"
        if segment[-1] in ". ":
            return "has a name ending in a dot or a space, which Windows drops, so it names another file"
        if _RESERVED.match(segment.split(".")[0].rstrip(" ")):
            return "uses a reserved device name (CON, NUL, COM1 and the like)"
    return None


class NameRefused(contract.PathRefused):
    """A path refused for its spelling rather than where it leads."""

    def __init__(self, path: str, why: str) -> None:
        super().__init__("denied", path)
        self.args = (f"Refused: {path} {why}.",)


def confine_path(root: str | os.PathLike[str], path: str, *, write: bool = False) -> contract.ConfinedPath:
    """The SDK's `confine`, with the Windows spellings refused first. A path
    the SDK cannot even relate to the root (`NUL` on another mount) is
    outside it."""
    if sys.platform == "win32":
        problem = windows_name_problem(path)
        if problem is not None:
            raise NameRefused(path, problem)
    try:
        return contract.confine(root, path, write=write)
    except ValueError as exc:
        raise contract.PathRefused("outside", path) from exc


@dataclass(frozen=True)
class ToolOutcome:
    """A tool's answer for the model, and whether it is a failure."""

    text: str
    is_error: bool = False


@dataclass(frozen=True)
class PendingWrite:
    """A write worked out and not yet made: what the approval shows.
    `old_bytes` is the file as the diff saw it (None: it did not exist)."""

    tool: str
    path: contract.ConfinedPath
    old_text: str | None
    new_text: str
    diff: str
    old_bytes: bytes | None = None

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


def _integer(args: Mapping[str, Any], key: str, low: int, high: int | None = None) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ArgumentError(f"`{key}` must be an integer.")
    if value < low or (high is not None and value > high):
        bound = f"between {low} and {high}" if high is not None else f"at least {low}"
        raise ArgumentError(f"`{key}` must be {bound}.")
    return value


def _glob_regex(pattern: str) -> re.Pattern[str]:
    try:
        return re.compile(contract.glob_to_regex(pattern.replace("\\", "/")), _GLOB_FLAGS)
    except re.error as exc:
        raise ArgumentError(f"The glob {pattern!r} is not valid ({exc}).") from exc


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_SNIFF_BYTES]


def read_text(path: Path) -> str:
    """A file's text exactly as stored (CRLF and BOM kept), so the text
    written back after an edit differs from the original only by the edit.
    ValueError for a binary file or one that is not UTF-8."""
    data = path.read_bytes()
    if _is_binary(data):
        raise ValueError("binary")
    return data.decode("utf-8")


def read_text_head(path: Path, max_bytes: int) -> tuple[str, bool]:
    """Up to `max_bytes` of a text file, and whether there was more; a
    character cut in half at the end is left out. ValueError for a binary
    file or one that is not UTF-8, OSError for one that is not a regular
    file (a pipe would never end)."""
    if not path.is_file():
        raise OSError(f"{path.name} is not a regular file")
    with open(path, "rb") as handle:
        data = handle.read(max_bytes + 1)
    more = len(data) > max_bytes
    data = data[:max_bytes]
    if _is_binary(data):
        raise ValueError("binary")
    try:
        return data.decode("utf-8"), more
    except UnicodeDecodeError as exc:
        if more and exc.start >= len(data) - 3:
            return data[: exc.start].decode("utf-8"), more
        raise


def _load(path: Path) -> bytes:
    """A file's bytes for a write to work from. OSError for one that is not a
    regular file or is too large to diff."""
    if not path.is_file():
        raise OSError(f"{path.name} is not a regular file")
    if path.stat().st_size > WRITE_MAX_BYTES:
        raise OSError(f"it is larger than {WRITE_MAX_BYTES // (1024 * 1024)} MB, too large to change here")
    return path.read_bytes()


def _lines(handle: IO[bytes]) -> Iterator[tuple[bytes, bool]]:
    """Each line's bytes, without its line feed, and whether it was cut at
    `READ_LINE_MAX_BYTES`. A file of one huge line is never held whole."""
    while True:
        raw = handle.readline(READ_LINE_MAX_BYTES + 1)
        if not raw:
            return
        if raw.endswith(b"\n"):
            yield raw[:-1], False
        elif len(raw) <= READ_LINE_MAX_BYTES:
            yield raw, False
        else:
            while True:
                more = handle.readline(1 << 20)
                if not more or more.endswith(b"\n"):
                    break
            yield raw[:READ_LINE_MAX_BYTES], True


def _count_rest(handle: IO[bytes]) -> int | None:
    """How many lines are left from here, or None past `COUNT_MAX_BYTES`."""
    count = 0
    read = 0
    last = b"\n"
    while chunk := handle.read(1 << 20):
        read += len(chunk)
        if read > COUNT_MAX_BYTES:
            return None
        count += chunk.count(b"\n")
        last = chunk[-1:]
    return count + (0 if last == b"\n" else 1)


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
    """The contract's tools over one workspace root."""

    def __init__(
        self,
        root: Path,
        *,
        sandbox: SandboxConfig | None = None,
        on_shell_start: Callable[[ProcessTree], None] | None = None,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        self.sandbox = sandbox or SandboxConfig()
        self.on_shell_start = on_shell_start

    # --- paths ------------------------------------------------------------------

    def confine(self, path: str, *, write: bool = False) -> contract.ConfinedPath:
        return confine_path(self.root, path, write=write)

    def _filter(self) -> SearchFilter:
        return SearchFilter(self.root)

    # --- reads ------------------------------------------------------------------

    async def read(self, name: str, args: Mapping[str, Any]) -> ToolOutcome:
        handler = {
            "read_file": self._read_file,
            "list_dir": self._list_dir,
            "glob": self._glob,
            "grep": self._grep,
        }[name]
        try:
            return await asyncio.to_thread(handler, args)
        except (ArgumentError, contract.PathRefused) as exc:
            return ToolOutcome(str(exc), is_error=True)
        except (OSError, ValueError) as exc:
            reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            return ToolOutcome(f"{name} failed: {reason}.", is_error=True)

    def _read_file(self, args: Mapping[str, Any]) -> ToolOutcome:
        path = _string(args, "path") or ""
        start = _integer(args, "start_line", 1) or 1
        end = _integer(args, "end_line", 1)
        target = self.confine(path)
        if target.absolute.is_dir():
            return ToolOutcome(f"{path} is a directory; use list_dir.", is_error=True)
        if not target.absolute.exists():
            return ToolOutcome(f"{path} does not exist.", is_error=True)
        if not target.absolute.is_file():
            # A pipe or a device would never end, or never start.
            return ToolOutcome(f"{path} is not a regular file and was not read.", is_error=True)
        try:
            with open(target.absolute, "rb") as handle:
                if _is_binary(handle.read(BINARY_SNIFF_BYTES)):
                    return ToolOutcome(f"{path} is a binary file and was not read.", is_error=True)
                handle.seek(0)
                return self._numbered(handle, path, start, end)
        except UnicodeDecodeError:
            return ToolOutcome(f"{path} is not UTF-8 text and was not read.", is_error=True)
        except OSError as exc:
            return ToolOutcome(f"{path} could not be read: {exc.strerror or exc}.", is_error=True)

    @staticmethod
    def _numbered(handle: IO[bytes], path: str, start: int, end: int | None) -> ToolOutcome:
        """Lines `start` to `end` with their numbers, read a line at a time
        so a large file is never held whole."""
        out: list[str] = []
        size = 0
        total = 0
        cut_at: int | None = None
        stopped = False
        for raw, cut in _lines(handle):
            total += 1
            if total < start:
                continue
            if end is not None and total > end:
                stopped = True
                break
            text = raw.decode("utf-8", errors="ignore" if cut else "strict").rstrip("\r")
            if cut:
                text += f" … (line cut at {READ_LINE_MAX_BYTES} bytes)"
            row = f"{total}\t{text}\n"
            size += len(row.encode("utf-8"))
            if len(out) >= LIMITS["read_max_lines"] or size > LIMITS["read_max_bytes"]:
                cut_at = total
                stopped = True
                break
            out.append(row)
        counted: int | None = total
        if stopped:
            rest = _count_rest(handle)
            counted = None if rest is None else total + rest
        if total == 0:
            return ToolOutcome(f"{path} is empty.")
        if start > total:
            return ToolOutcome(
                f"start_line {start} is past the end of {path} ({total} lines).", is_error=True
            )
        if end is not None and end < start:
            return ToolOutcome("end_line is before start_line.", is_error=True)
        if cut_at is not None:
            of = f"{counted}" if counted is not None else f"more than {total}"
            out.append(
                f"(cut off at the read limit after line {cut_at - 1} of {of}; "
                f"pass start_line={cut_at} for the rest)\n"
            )
        return ToolOutcome("".join(out).rstrip("\n"))

    def _list_dir(self, args: Mapping[str, Any]) -> ToolOutcome:
        path = _string(args, "path", required=False) or "."
        target = self.confine(path)
        if not target.absolute.is_dir():
            return ToolOutcome(f"{path} is not a directory.", is_error=True)
        base = "" if target.relative == "." else target.relative
        search = self._filter()
        dirs: list[str] = []
        files: list[str] = []
        try:
            entries = sorted(os.scandir(target.absolute), key=lambda e: e.name)
        except OSError as exc:
            return ToolOutcome(f"{path} could not be listed: {exc.strerror or exc}.", is_error=True)
        for entry in entries:
            rel = f"{base}/{entry.name}" if base else entry.name
            try:
                is_dir = entry.is_dir()
            except OSError:
                continue
            if search.hidden(rel, is_dir):
                continue
            (dirs if is_dir else files).append(entry.name + ("/" if is_dir else ""))
        names = dirs + files
        cap = LIMITS["list_max_entries"]
        if not names:
            return ToolOutcome(f"{path} is empty (or holds only ignored entries).")
        text = "\n".join(names[:cap])
        if len(names) > cap:
            text += f"\n({len(names) - cap} more entries not shown)"
        return ToolOutcome(text)

    def _search_base(self, args: Mapping[str, Any]) -> tuple[str, contract.ConfinedPath]:
        path = _string(args, "path", required=False) or "."
        target = self.confine(path)
        return ("" if target.relative == "." else target.relative), target

    def _glob(self, args: Mapping[str, Any]) -> ToolOutcome:
        pattern = (_string(args, "pattern") or "").strip().lstrip("/")
        if not pattern:
            raise ArgumentError("`pattern` is empty.")
        if ".." in re.split(r"[\\/]", pattern):
            raise contract.PathRefused("parent", pattern)
        base, target = self._search_base(args)
        if not target.absolute.is_dir():
            return ToolOutcome(f"{args.get('path')} is not a directory.", is_error=True)
        regex = _glob_regex(pattern)
        cap = LIMITS["search_max_results"]
        found: list[str] = []
        more = False
        for rel, is_dir in self._filter().walk(base):
            if is_dir:
                continue
            local = rel[len(base) + 1 :] if base else rel
            if regex.fullmatch(local):
                if len(found) >= cap:
                    more = True
                    break
                found.append(rel)
        if not found:
            return ToolOutcome(f"No files match {pattern}.")
        text = "\n".join(found)
        if more:
            text += f"\n(stopped at {cap} results; narrow the pattern)"
        return ToolOutcome(text)

    def _grep(self, args: Mapping[str, Any]) -> ToolOutcome:
        pattern = _string(args, "pattern") or ""
        file_glob = _string(args, "glob", required=False)
        ignore_case = args.get("ignore_case") is True
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise ArgumentError(f"`pattern` is not a valid regular expression ({exc}).") from exc
        glob_re: re.Pattern[str] | None = None
        if file_glob:
            glob_re = _glob_regex(file_glob)
        base, target = self._search_base(args)
        if target.absolute.is_file():
            candidates: list[str] = [target.relative]
        else:
            candidates = [rel for rel, is_dir in self._filter().walk(base) if not is_dir]
        cap = LIMITS["search_max_results"]
        hits: list[str] = []
        for rel in candidates:
            if glob_re is not None:
                subject = rel if "/" in (file_glob or "") else rel.rsplit("/", 1)[-1]
                if not glob_re.fullmatch(subject):
                    continue
            absolute = self.root / rel
            try:
                if not absolute.is_file() or absolute.stat().st_size > GREP_FILE_MAX_BYTES:
                    continue
                data = absolute.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            text = data.decode("utf-8", errors="replace")
            for number, line in enumerate(text.split("\n"), start=1):
                if regex.search(line):
                    shown = line.rstrip("\r")
                    if len(shown) > GREP_LINE_MAX_CHARS:
                        shown = shown[:GREP_LINE_MAX_CHARS] + "…"
                    hits.append(f"{rel}:{number}: {shown}")
                    if len(hits) >= cap:
                        hits.append(f"(stopped at {cap} matches; narrow the search)")
                        return ToolOutcome("\n".join(hits))
        if not hits:
            return ToolOutcome(f"No matches for {pattern}.")
        return ToolOutcome("\n".join(hits))

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

    def _prepare_edit(self, args: Mapping[str, Any]) -> PendingWrite | ToolOutcome:
        path = _required(args, "path")
        old_string = _required(args, "old_string")
        new_string = _required(args, "new_string")
        target = self.confine(path, write=True)
        if not target.absolute.exists():
            raise contract.EditError("missing", path)
        if target.absolute.is_dir():
            return ToolOutcome(f"{path} is a directory.", is_error=True)
        data = _load(target.absolute)
        try:
            if _is_binary(data):
                raise ValueError("binary")
            old = data.decode("utf-8")
        except ValueError:
            return ToolOutcome(f"{path} is not UTF-8 text, so it cannot be edited.", is_error=True)
        new = contract.apply_edit(old, old_string, new_string, path=path)
        diff = contract.unified_diff(old, new, target.relative)
        return PendingWrite("edit", target, old, new, diff, data)

    def _prepare_write_file(self, args: Mapping[str, Any]) -> PendingWrite | ToolOutcome:
        path = _required(args, "path")
        content = _required(args, "content")
        target = self.confine(path, write=True)
        if target.absolute.is_dir():
            return ToolOutcome(f"{path} is a directory.", is_error=True)
        if len(content.encode("utf-8")) > WRITE_MAX_BYTES:
            return ToolOutcome(f"The content for {path} is too large to write here.", is_error=True)
        if not target.absolute.exists():
            diff = contract.unified_diff("", content, target.relative)
            diff = diff.replace(f"--- a/{target.relative}", "--- /dev/null", 1)
            return PendingWrite("write_file", target, None, content, diff, None)
        data = _load(target.absolute)
        try:
            if _is_binary(data):
                raise ValueError("binary")
            old = data.decode("utf-8")
        except ValueError:
            # The old bytes cannot be shown as text, but what replaces them
            # is: the approval shows every line that will be written.
            diff = (
                f"(the current {target.relative} is binary or not UTF-8 text; "
                "it is replaced whole by the text below)\n"
                + contract.unified_diff("", content, target.relative)
            )
            return PendingWrite("write_file", target, None, content, diff, data)
        if old == content:
            return ToolOutcome(f"{path} already has exactly this content; nothing to write.")
        diff = contract.unified_diff(old, content, target.relative)
        return PendingWrite("write_file", target, old, content, diff, data)

    def stale(self, pending: PendingWrite) -> ToolOutcome | None:
        """Why the write must not be made after all, or None. The diff was
        approved for the file as it was and the place its path led then; a
        change to either since (another program, a command, a link swapped
        in) means the approval no longer describes the write."""
        try:
            again = self.confine(pending.relative, write=True)
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

    @staticmethod
    def shell_args(args: Mapping[str, Any]) -> tuple[str, int]:
        command = _string(args, "command") or ""
        if not command.strip():
            raise ArgumentError("`command` is empty.")
        timeout = _integer(args, "timeout_s", 1, LIMITS["shell_timeout_s_max"])
        return command, timeout or LIMITS["shell_timeout_s_default"]

    async def run_shell(self, command: str, timeout_s: int) -> ToolOutcome:
        try:
            result = await run_command(
                command,
                self.root,
                timeout_s=timeout_s,
                output_max_bytes=LIMITS["shell_output_max_bytes"],
                sandbox=self.sandbox,
                on_start=self.on_shell_start,
            )
        except SandboxUnavailable as exc:
            return ToolOutcome(
                f"Refused: the sandbox is on but cannot run here ({exc}); the command was not run.",
                is_error=True,
            )
        except OSError as exc:
            return ToolOutcome(f"The command could not be started: {exc}.", is_error=True)
        return ToolOutcome(
            result.render(timeout_s), is_error=result.timed_out or result.exit_code != 0
        )
