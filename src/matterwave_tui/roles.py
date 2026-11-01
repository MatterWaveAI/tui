"""The prompt library: named system texts ("roles") kept on this device,
applied to a conversation with the same PUT the instructions editor uses.

The file sits in the state directory (see `paths`) as `roles.json`: a
JSON object of name -> system text, small enough to write whole.
"""

from __future__ import annotations

import json
from pathlib import Path

from .paths import state_dir

ROLES_FILE = "roles.json"


def roles_path() -> Path:
    """Where the library lives."""
    return state_dir() / ROLES_FILE


def load_roles(path: Path) -> dict[str, str]:
    """The saved roles. Anything unreadable reads as an empty library,
    which only loses the shortcuts, never the conversations."""
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(rows, dict):
        return {}
    return {str(name): text for name, text in rows.items() if name and isinstance(text, str)}


def save_roles(path: Path, roles: dict[str, str]) -> bool:
    """Write the library whole. False when the disk refused, so the caller
    can say so rather than let a role vanish quietly."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(roles, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True
