"""Where state lives: the platform's own directory, `MATTERWAVE_STATE_DIR`
over it, and an earlier release's files carried across once."""

from __future__ import annotations

from pathlib import Path

import pytest

from matterwave_tui import paths


@pytest.fixture
def platform_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The platform's state directory, as platformdirs would name it, kept
    in the test's scratch space."""
    where = tmp_path / "platform" / "matterwave-tui"
    monkeypatch.setattr(paths.platformdirs, "user_state_dir", lambda *a, **k: str(where))
    return where


def test_the_override_names_the_directory(tmp_path: Path):
    env = {"MATTERWAVE_STATE_DIR": str(tmp_path)}
    assert paths.state_dir(env) == tmp_path / "matterwave-tui"


def test_without_it_the_platform_decides(platform_state: Path):
    assert paths.state_dir({}) == platform_state


def test_an_earlier_releases_files_move_across_once(tmp_path: Path, platform_state: Path):
    old = tmp_path / "xdg" / "matterwave-tui"
    old.mkdir(parents=True)
    (old / "roles.json").write_text('{"terse": "Be terse."}', encoding="utf-8")
    (old / "dismissed-announcements.json").write_text('["an1"]', encoding="utf-8")
    platform_state.mkdir(parents=True)
    # A file already in the new place is the newer one, and stays.
    (platform_state / "dismissed-announcements.json").write_text('["an2"]', encoding="utf-8")
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg")}

    assert paths.migrate_legacy_state(env) == ["roles.json"]
    assert (platform_state / "roles.json").read_text(encoding="utf-8") == '{"terse": "Be terse."}'
    assert not (old / "roles.json").exists()
    assert (platform_state / "dismissed-announcements.json").read_text(encoding="utf-8") == (
        '["an2"]'
    )
    assert paths.migrate_legacy_state(env) == []


def test_nothing_moves_where_the_places_are_one(tmp_path: Path, monkeypatch):
    same = tmp_path / "state" / "matterwave-tui"
    same.mkdir(parents=True)
    (same / "roles.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(paths.platformdirs, "user_state_dir", lambda *a, **k: str(same))
    assert paths.migrate_legacy_state({"XDG_STATE_HOME": str(tmp_path / "state")}) == []
    assert (same / "roles.json").exists()


def test_nothing_moves_when_the_directory_is_named(tmp_path: Path, platform_state: Path):
    old = tmp_path / "xdg" / "matterwave-tui"
    old.mkdir(parents=True)
    (old / "roles.json").write_text("{}", encoding="utf-8")
    env = {"XDG_STATE_HOME": str(tmp_path / "xdg"), "MATTERWAVE_STATE_DIR": str(tmp_path)}
    assert paths.migrate_legacy_state(env) == []
    assert (old / "roles.json").exists()
