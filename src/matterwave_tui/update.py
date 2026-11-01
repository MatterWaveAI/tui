"""A once-a-day look at PyPI for a newer matterwave-tui, at the app's
start. `--no-update-check` or `MATTERWAVE_NO_UPDATE_CHECK=1` turns it off.
The time of the last look is kept in the state directory, written before
the request so an unreachable index is asked once a day, not on every
start; any failure is silence, never a startup delay or an error.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

import httpx

PYPI_URL = "https://pypi.org/pypi/matterwave-tui/json"
DISABLE_ENV = "MATTERWAVE_NO_UPDATE_CHECK"
STATE_FILE = "update-check.json"
CHECK_EVERY_S = 24 * 60 * 60
TIMEOUT_S = 3.0


def version_key(version: str) -> tuple[int, ...]:
    """The release numbers of a version (`0.10.2` -> (0, 10, 2)); a
    suffix such as `rc1` ends them."""
    numbers: list[int] = []
    for part in version.split("."):
        match = re.match(r"\d+", part)
        if match is None:
            break
        numbers.append(int(match.group()))
        if match.end() != len(part):
            break
    return tuple(numbers)


def newer(latest: str, current: str) -> bool:
    return version_key(latest) > version_key(current)


def _claim(state: Path, now: float) -> bool:
    """Whether a look is due, writing the time of this one if it is."""
    try:
        checked = json.loads(state.read_text(encoding="utf-8")).get("checked_at")
    except (OSError, ValueError, AttributeError):
        checked = None
    if isinstance(checked, int | float) and now - checked < CHECK_EVERY_S:
        return False
    try:
        state.parent.mkdir(parents=True, exist_ok=True)
        state.write_text(json.dumps({"checked_at": now}), encoding="utf-8")
    except OSError:
        return False
    return True


async def check_for_update(
    current: str,
    state: Path,
    *,
    http: httpx.AsyncClient,
    now: float | None = None,
) -> str | None:
    """The newer version PyPI has, or None: not due yet, nothing newer, or
    the index could not be asked."""
    moment = time.time() if now is None else now
    if not await asyncio.to_thread(_claim, state, moment):
        return None
    try:
        response = await http.get(PYPI_URL, timeout=TIMEOUT_S)
        latest = response.json()["info"]["version"] if response.is_success else None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return None
    return latest if isinstance(latest, str) and newer(latest, current) else None
