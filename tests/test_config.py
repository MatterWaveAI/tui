"""The config file and its profiles: where each setting comes from, what
the file may say, and that a profile signs in on its own keyring entry."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import httpx
import pytest

from matterwave_tui import cli
from matterwave_tui.app import MatterWaveApp
from matterwave_tui.config import ConfigError, keyring_entry, load_config, resolve

from .fake_api import FakeAPI
from .test_cli import mock_api

CONFIG = """
base_url = "https://file.test"
model = "grt-nano"
theme = "nord"
images = "off"
default_profile = "home"

[profiles.home]
model = "lab-x"

[profiles.work]
base_url = "https://work.test"
model = "grt-max"
shell = "pwsh"
"""


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("MATTERWAVE_CONFIG", str(path))
    return path


def test_each_setting_comes_from_the_first_place_that_has_it(config_file: Path):
    config = load_config(config_file)
    # The default profile, over the file's top level.
    settings = resolve(config, flags={}, env={})
    assert settings.profile == "home"
    assert (settings.get("model"), settings.sources["model"]) == ("lab-x", "profile home")
    assert (settings.get("base_url"), settings.sources["base_url"]) == (
        "https://file.test",
        "config file",
    )
    assert settings.sources["shell"] == "default"
    # A named profile, the environment over it, a flag over both.
    env = {"MATTERWAVE_MODEL": "other-7b", "MATTERWAVE_BASE_URL": "https://env.test"}
    settings = resolve(config, flags={"model": "lab-y"}, env=env, profile="work")
    assert (settings.get("model"), settings.sources["model"]) == ("lab-y", "flag")
    assert (settings.get("base_url"), settings.sources["base_url"]) == (
        "https://env.test",
        "environment",
    )
    assert (settings.get("shell"), settings.sources["shell"]) == ("pwsh", "profile work")
    # The profile can come from the environment too.
    assert resolve(config, flags={}, env={"MATTERWAVE_PROFILE": "work"}).profile == "work"


def test_no_file_is_every_default(tmp_path: Path):
    settings = resolve(load_config(tmp_path / "absent.toml"), flags={}, env={})
    assert settings.profile is None
    assert set(settings.sources.values()) == {"default"}


@pytest.mark.parametrize(
    ("text", "complaint"),
    [
        ("base_url = [", "is not valid TOML"),
        ('server = "https://x"', "server, which this version does not know"),
        ('[profiles.a]\nbaseurl = "x"', "[profiles.a] has baseurl"),
        ("model = 3", "model in the top level must be a string"),
    ],
)
def test_a_file_this_version_cannot_use_is_an_error(tmp_path: Path, text: str, complaint: str):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigError, match=complaint.replace("[", r"\[").replace("]", r"\]")):
        load_config(path)


def test_an_unknown_profile_names_the_ones_there_are(config_file: Path):
    with pytest.raises(ConfigError, match=r"No profile named play .*\(profiles: home, work\)"):
        resolve(load_config(config_file), flags={}, env={}, profile="play", path=config_file)


def test_each_profile_has_its_own_keyring_entry():
    assert keyring_entry("https://a.test", None) == "https://a.test"
    assert keyring_entry("https://a.test", "work") == "https://a.test [work]"


def test_a_profile_signs_in_with_its_own_key_on_its_own_server(
    config_file: Path, memory_keyring, capsys, monkeypatch
):
    memory_keyring.entries[("matterwave", "https://work.test [work]")] = "mw_work"
    memory_keyring.entries[("matterwave", "https://file.test [home]")] = "mw_home"
    seen: list[httpx.Request] = []
    monkeypatch.setattr("matterwave_tui.api.httpx.AsyncClient", mock_api(seen))
    monkeypatch.delenv("MATTERWAVE_API_KEY", raising=False)
    assert cli.main(["--profile", "work", "-p", "hi"]) == 0
    assert {r.url.host for r in seen} == {"work.test"}
    assert {r.headers["authorization"] for r in seen} == {"Bearer mw_work"}
    # The profile's model rides the turn.
    assert json.loads(seen[-1].content)["model"] == "grt-max"


def test_config_says_where_each_setting_comes_from(config_file: Path, capsys, monkeypatch):
    monkeypatch.setenv("MATTERWAVE_MODEL", "other-7b")
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert f"config file: {config_file}" in out
    assert "profile: home" in out
    assert "model: other-7b (environment)" in out
    assert "base_url: https://file.test (config file)" in out
    assert "keyring entry: https://file.test [home]" in out


def test_a_file_this_version_cannot_use_stops_the_cli(tmp_path: Path, capsys, monkeypatch):
    path = tmp_path / "config.toml"
    path.write_text('server = "x"', encoding="utf-8")
    monkeypatch.setenv("MATTERWAVE_CONFIG", str(path))
    assert cli.main(["config"]) == cli.EXIT_USAGE
    assert "does not know" in capsys.readouterr().err


def test_the_files_image_and_theme_settings_reach_the_app(config_file: Path, monkeypatch):
    handed: dict[str, object] = {}

    def run_app(base_url: str, credential: object, **kwargs: object) -> None:
        handed.update(kwargs, base_url=base_url)

    monkeypatch.setattr(cli, "run_app", run_app)
    monkeypatch.setattr(cli.sys, "stdin", argparse.Namespace(isatty=lambda: True))
    monkeypatch.delenv("MATTERWAVE_IMAGES", raising=False)
    assert cli.main(["--key", "mw_k"]) == 0
    assert handed["theme"] == "nord" and handed["model"] == "lab-x"
    environ = handed["environ"]
    assert isinstance(environ, dict) and environ["MATTERWAVE_IMAGES"] == "off"
    # Handed down, not written into the process's own environment.
    assert "MATTERWAVE_IMAGES" not in os.environ


@pytest.mark.asyncio
async def test_the_configured_theme_is_applied_and_an_unknown_one_said():
    app = MatterWaveApp(FakeAPI(), theme="nord")
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.theme == "nord"
    shown: list[str] = []
    app = MatterWaveApp(FakeAPI(), theme="no-such-theme")
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert any("No theme named no-such-theme" in s for s in shown)
