"""The README's tables are checked against the code they describe: every
key the app binds, every key a dialog binds, every palette entry, every
environment variable the package reads and every extra it offers. A key
added without a line in the README fails here."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest
from textual.binding import Binding

from matterwave_tui import screens
from matterwave_tui.app import MatterWaveApp, Transcript, TuiCommands

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text(encoding="utf-8")

# How the README writes the keys Textual names.
NAMED = {
    "slash": "/",
    "question_mark": "?",
    "left_square_bracket": "[",
    "right_square_bracket": "]",
    "escape": "Esc",
    "enter": "Enter",
    "space": "Space",
    "delete": "Delete",
    "up": "↑",
    "down": "↓",
}


def shown(key: str) -> str:
    if key in NAMED:
        return NAMED[key]
    if key.startswith("ctrl+"):
        return "Ctrl+" + shown(key.removeprefix("ctrl+")).capitalize()
    if re.fullmatch(r"f\d+", key):
        return key.upper()
    return key


def keys_of(bindings: list[Binding | tuple[str, ...]]) -> list[str]:
    out: list[str] = []
    for binding in bindings:
        key = binding.key if isinstance(binding, Binding) else binding[0]
        out.extend(shown(part.strip()) for part in key.split(","))
    return out


def section(title: str) -> str:
    start = README.index(f"## {title}\n")
    following = README.find("\n## ", start + 1)
    return README[start : following if following != -1 else len(README)]


def table_rows(text: str) -> list[list[str]]:
    """The cells of every table row in `text`, headers and rules left out."""
    rows: list[list[str]] = []
    for line in text.splitlines():
        if not line.startswith("|") or line.startswith("|-"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if cells[0] in ("Key", "Dialog", "Variable", "Extra", "Status"):
            continue
        rows.append(cells)
    return rows


def test_every_app_key_is_in_the_key_table():
    keys = section("Keys")
    for key in keys_of(MatterWaveApp.BINDINGS) + keys_of(Transcript.BINDINGS):
        assert f"`{key}`" in keys, f"{key} is bound but not in the README's key table"


def test_every_key_in_the_table_is_bound():
    bound = set(keys_of(MatterWaveApp.BINDINGS) + keys_of(Transcript.BINDINGS))
    keys = section("Keys")
    main_table = keys[: keys.index("Inside the dialogs")]
    for row in table_rows(main_table):
        for key in re.findall(r"`([^`]+)`", row[0]):
            assert key in bound, f"the README lists {key}, which nothing binds"


@pytest.mark.parametrize(
    ("dialog", "screen"),
    [
        ("Notifications (`N`)", screens.Notifications),
        ("Library (`l`)", screens.Library),
        ("Shares (`S`)", screens.Shares),
        ("Prompt roles (`L`)", screens.Roles),
        ("Pending files (`I`)", screens.PendingAttachments),
        ("Account (`U`)", screens.Account),
        ("Share (`s`)", screens.ShareDialog),
        ("Pick files (`Ctrl+O`)", screens.FilePicker),
        ("Help (`?`)", screens.HelpScreen),
    ],
)
def test_every_dialog_key_is_in_its_row(dialog, screen):
    rows = {row[0]: row[1] for row in table_rows(section("Keys")) if len(row) == 2}
    for key in keys_of(screen.BINDINGS):
        if key != "Esc":
            assert f"`{key}`" in rows[dialog], f"{dialog} binds {key}"


def test_every_palette_entry_is_named():
    for name, _, _ in TuiCommands.ENTRIES:
        assert f"**{name}**" in README


def test_every_environment_variable_is_documented():
    source = "\n".join(p.read_text(encoding="utf-8") for p in (ROOT / "src").rglob("*.py"))
    read_here = set(re.findall(r'"(MATTERWAVE_[A-Z_]+)"', source)) | {
        "MATTERWAVE_BASE_URL",
        "MATTERWAVE_API_KEY",
    }
    documented = {row[0].strip("`") for row in table_rows(section("Environment"))}
    assert read_here <= documented


def test_every_extra_is_documented():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "optional-dependencies"
    ]
    documented = {row[0].strip("`") for row in table_rows(section("Install"))}
    assert set(extras) - {"dev"} == documented
