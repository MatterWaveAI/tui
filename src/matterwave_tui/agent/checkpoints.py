"""File checkpoints: before each applied write, the bytes of the files it
touches go into a shadow git repository outside the project, and
`/restore` puts them back.

The shadow repository is a bare one in the state directory, one per
workspace (named by a hash of its path). It is used as a content store:
`git hash-object -w --no-filters` keeps a file's exact bytes and
`git cat-file blob` gives them back, so no index, work tree, commit or
hook is involved and the project's own `.git` is never read or written.
The list of checkpoints, each naming the files it holds (or that they did
not exist yet), is a JSON file beside it.

Restoring checkpoint k undoes every write since it: the checkpoints from
the newest back to k are each put back in turn, so a file touched twice
ends at its bytes from before the first of those writes. The current bytes
are checkpointed first, so a restore can itself be restored.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from matterwave_sdk import agent as contract

from .settings import folder_key, state_dir
from .tools import confine_path

GIT_TIMEOUT_S = 30
INDEX_FILE = "checkpoints.json"


@dataclass(frozen=True)
class Checkpoint:
    """One snapshot: the files a write was about to touch, each with the
    blob of its bytes then (None: it did not exist)."""

    id: int
    created: str
    label: str
    files: dict[str, str | None]
    conversation_id: str | None = None
    turn: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created": self.created,
            "label": self.label,
            "files": self.files,
            "conversation_id": self.conversation_id,
            "turn": self.turn,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> Checkpoint:
        files = row.get("files")
        return cls(
            id=int(row.get("id") or 0),
            created=str(row.get("created") or ""),
            label=str(row.get("label") or ""),
            files={str(k): (str(v) if v else None) for k, v in files.items()}
            if isinstance(files, dict)
            else {},
            conversation_id=row.get("conversation_id") if isinstance(row.get("conversation_id"), str) else None,
            turn=row.get("turn") if isinstance(row.get("turn"), int) else None,
        )


class CheckpointError(RuntimeError):
    pass


@dataclass
class Checkpoints:
    """The shadow store for one workspace root."""

    root: Path
    store: Path = field(default=Path())

    def __post_init__(self) -> None:
        if self.store == Path():
            digest = hashlib.sha256(folder_key(self.root).encode("utf-8")).hexdigest()[:16]
            self.store = state_dir() / "checkpoints" / digest

    @staticmethod
    def available() -> bool:
        return shutil.which("git") is not None

    # --- git --------------------------------------------------------------------

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env["GIT_CONFIG_NOSYSTEM"] = "1"
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _git(self, *args: str, data: bytes | None = None, bare: bool = True) -> bytes:
        prefix = ["git", f"--git-dir={self.store / 'git'}"] if bare else ["git"]
        try:
            done = subprocess.run(  # noqa: S603 -- fixed argv, git from PATH
                [*prefix, *args],
                input=data,
                capture_output=True,
                env=self._env(),
                cwd=str(self.store),
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CheckpointError(f"git could not run: {exc}") from exc
        if done.returncode != 0:
            raise CheckpointError(done.stderr.decode("utf-8", "replace").strip() or "git failed")
        return done.stdout

    def _ensure(self) -> None:
        if (self.store / "git" / "HEAD").exists():
            return
        # The blobs are the project's file contents: readable by the user
        # alone, whatever the umask (on Windows the profile folder's own
        # access list already is).
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(self.store.parent, 0o700)
            os.chmod(self.store, 0o700)
        self._git("init", "--bare", "--quiet", str(self.store / "git"), bare=False)

    # --- the index of checkpoints -------------------------------------------------

    def _index_path(self) -> Path:
        return self.store / INDEX_FILE

    def entries(self) -> list[Checkpoint]:
        """Every checkpoint, oldest first."""
        try:
            rows = json.loads(self._index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [Checkpoint.from_dict(r) for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def _save(self, rows: list[Checkpoint]) -> None:
        path = self._index_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps([r.to_dict() for r in rows], indent=1), encoding="utf-8")
        os.replace(tmp, path)

    # --- snapshot and restore -----------------------------------------------------

    def snapshot(
        self,
        relatives: list[str],
        label: str,
        *,
        conversation_id: str | None = None,
        turn: int | None = None,
    ) -> Checkpoint:
        """Keep the current bytes of `relatives` (paths under the root, `/`
        separated) as a new checkpoint."""
        self._ensure()
        files: dict[str, str | None] = {}
        for rel in relatives:
            absolute = self.root / rel
            if absolute.is_file():
                blob = self._git("hash-object", "-w", "--no-filters", "--stdin", data=absolute.read_bytes())
                files[rel] = blob.decode("ascii").strip()
            else:
                files[rel] = None
        rows = self.entries()
        checkpoint = Checkpoint(
            id=(rows[-1].id + 1) if rows else 1,
            created=datetime.now(UTC).isoformat(timespec="seconds"),
            label=label,
            files=files,
            conversation_id=conversation_id,
            turn=turn,
        )
        self._save([*rows, checkpoint])
        return checkpoint

    def restore(self, checkpoint_id: int) -> list[str]:
        """Undo every write since checkpoint `checkpoint_id`; the paths
        put back. The bytes as they are now are checkpointed first."""
        rows = self.entries()
        chosen = [r for r in rows if r.id >= checkpoint_id]
        if not chosen or chosen[0].id != checkpoint_id:
            raise CheckpointError(f"there is no checkpoint {checkpoint_id}")
        paths = sorted({p for r in chosen for p in r.files})
        # Each path is confined again, as a write: since the snapshot a
        # folder on the way may have become a link out of the root, and the
        # index is a file anyone with the user's rights can edit.
        targets: dict[str, Path] = {}
        for rel in paths:
            try:
                targets[rel] = confine_path(self.root, rel, write=True).absolute
            except contract.PathRefused as exc:
                raise CheckpointError(f"{rel} cannot be put back: {exc}") from exc
        self.snapshot(paths, f"before /restore {checkpoint_id}")
        for row in reversed(chosen):
            for rel, blob in row.files.items():
                absolute = targets[rel]
                if blob is None:
                    if absolute.is_file():
                        absolute.unlink()
                    continue
                data = self._git("cat-file", "blob", blob)
                absolute.parent.mkdir(parents=True, exist_ok=True)
                absolute.write_bytes(data)
        return paths

    async def asnapshot(self, relatives: list[str], label: str, **kwargs: Any) -> Checkpoint:
        return await asyncio.to_thread(self.snapshot, relatives, label, **kwargs)

    async def arestore(self, checkpoint_id: int) -> list[str]:
        return await asyncio.to_thread(self.restore, checkpoint_id)
