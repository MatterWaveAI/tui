from __future__ import annotations

from pathlib import Path

import pytest

STREAMS = Path(__file__).parent / "streams"


@pytest.fixture(autouse=True)
def private_dirs(tmp_path_factory, monkeypatch):
    """Every test gets its own state and config directories, so nothing a
    test does (trusted folders, checkpoints, settings, recorded runs)
    reaches the real home directory or another test."""
    state = tmp_path_factory.mktemp("state")
    config = tmp_path_factory.mktemp("config")
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(state))
    monkeypatch.setenv("MATTERWAVE_CONFIG_DIR", str(config))
    return state, config


@pytest.fixture
def recorded():
    def load(name: str) -> str:
        return (STREAMS / name).read_text(encoding="utf-8")

    return load
