"""What the package says about itself: one version, written once; the
typing marker its classifier promises; the SDK floor the code needs and
where uv takes the SDK from."""

from __future__ import annotations

import tomllib
from importlib import metadata
from pathlib import Path

import matterwave_tui

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_version_is_written_once():
    project = PYPROJECT["project"]
    assert "version" not in project
    assert project["dynamic"] == ["version"]
    assert PYPROJECT["tool"]["hatch"]["version"]["path"] == "src/matterwave_tui/__init__.py"
    # The installed distribution was built from that one place.
    assert metadata.version("matterwave-tui") == matterwave_tui.__version__


def test_the_package_ships_its_typing_marker():
    assert "Typing :: Typed" in PYPROJECT["project"]["classifiers"]
    assert (ROOT / "src" / "matterwave_tui" / "py.typed").is_file()


def test_the_sdk_resolves_from_the_source_the_lockfile_records():
    """matterwave-sdk is not on PyPI: uv takes it from its repository at the
    commit the lockfile pins or, while agent mode needs an agent contract
    that is not on that repository yet, from the python-sdk checkout beside
    this one. The floor is the release whose per-call timeout and retry
    overrides the TUI's client passes through."""
    assert "matterwave-sdk>=0.5.0" in PYPROJECT["project"]["dependencies"]
    source = PYPROJECT["tool"]["uv"]["sources"]["matterwave-sdk"]
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    if "git" in source:
        assert source["git"] == "https://github.com/MatterWaveAI/python-sdk"
        assert len(source["rev"]) == 40
        assert f"python-sdk?rev={source['rev']}#{source['rev']}" in lock
    else:
        assert source == {"path": "../python-sdk", "editable": True}
        assert 'source = { editable = "../python-sdk" }' in lock
