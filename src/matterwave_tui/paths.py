"""Where the TUI keeps its files on this machine, by each OS's convention.

State -- the dismissed announcements, the prompt roles -- goes in the
platform's user state directory: `~/.local/state/matterwave-tui` (or
`$XDG_STATE_HOME`) on Linux, `%LOCALAPPDATA%\\matterwave-tui` on Windows,
`~/Library/Application Support/matterwave-tui` on macOS.
`MATTERWAVE_STATE_DIR` moves it (the tests point it at a scratch
directory), as `<that>/matterwave-tui`.

Releases before this one put state in `~/.local/state/matterwave-tui` on
every OS; `migrate_legacy_state` carries those files across once.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

import platformdirs

APP_NAME = "matterwave-tui"
STATE_ENV = "MATTERWAVE_STATE_DIR"


def state_dir(env: Mapping[str, str] = os.environ) -> Path:
    root = env.get(STATE_ENV)
    if root:
        return Path(root) / APP_NAME
    return Path(platformdirs.user_state_dir(APP_NAME, appauthor=False))


def legacy_state_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where earlier releases kept state, whatever the OS."""
    xdg = env.get("XDG_STATE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return root / APP_NAME


def migrate_legacy_state(env: Mapping[str, str] = os.environ) -> list[str]:
    """Move the files an earlier release left in the old place to the new
    one, never over a file already there; the names moved. Nothing to do
    where the two places are one (Linux), or where `MATTERWAVE_STATE_DIR`
    names the place outright. A file that cannot be moved stays where it
    was, which only means it is not read."""
    if env.get(STATE_ENV):
        return []
    old, new = legacy_state_dir(env), state_dir(env)
    if not old.is_dir() or old.resolve() == new.resolve():
        return []
    moved: list[str] = []
    for item in sorted(old.iterdir()):
        target = new / item.name
        if not item.is_file() or target.exists():
            continue
        try:
            new.mkdir(parents=True, exist_ok=True)
            shutil.move(str(item), str(target))
        except OSError:
            continue
        moved.append(item.name)
    return moved
