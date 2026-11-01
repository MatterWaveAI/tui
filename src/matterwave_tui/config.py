"""The config file and its profiles: settings that would otherwise be typed
as flags or exported in every shell.

    # ~/.config/matterwave/config.toml (%APPDATA%\\matterwave\\config.toml)
    base_url = "https://app.example.com"
    model = "grt-nano"
    theme = "nord"
    default_profile = "work"

    [profiles.work]
    base_url = "https://work.example.com"
    model = "grt-max"

    [keys]
    new = "ctrl+n"

A setting is taken from the first place that has it: the command line,
then the environment, then the chosen profile, then the file's top level,
then the built-in default. A profile is chosen by `--profile`, else
`MATTERWAVE_PROFILE`, else `default_profile`. Each profile signs in on its
own keyring entry, so two accounts on one server can sit side by side.
`MATTERWAVE_CONFIG` names another file.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import platformdirs

CONFIG_ENV = "MATTERWAVE_CONFIG"
PROFILE_ENV = "MATTERWAVE_PROFILE"
CONFIG_FILE = "config.toml"

# Each setting, and the environment variable that stands above a profile.
SETTINGS: dict[str, str | None] = {
    "base_url": "MATTERWAVE_BASE_URL",
    "model": "MATTERWAVE_MODEL",
    "theme": None,
    "images": "MATTERWAVE_IMAGES",
    "shell": "MATTERWAVE_SHELL",
}
TOP_LEVEL = frozenset({*SETTINGS, "default_profile", "profiles", "keys"})


class ConfigError(ValueError):
    """The config file cannot be used; the message says where and why."""


@dataclass(frozen=True)
class Settings:
    """The resolved settings, each with where it came from."""

    values: dict[str, str | None]
    sources: dict[str, str]
    profile: str | None = None
    path: Path | None = None
    # [keys]: a binding's name (as the help lists it) -> the key for it.
    keys: dict[str, str] = field(default_factory=dict)

    def get(self, name: str) -> str | None:
        return self.values.get(name)


def config_path(env: Mapping[str, str] = os.environ) -> Path:
    named = env.get(CONFIG_ENV)
    if named:
        return Path(named).expanduser()
    return Path(platformdirs.user_config_dir("matterwave", appauthor=False, roaming=True)) / (
        CONFIG_FILE
    )


def load_config(path: Path) -> dict[str, Any]:
    """The file's tables, or nothing when there is no file. A file that is
    not TOML, or names a setting this version does not know, is an error
    rather than a silent default: a typo should not look like the default
    server."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc
    _check(path, data, TOP_LEVEL, "the top level")
    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        raise ConfigError(f"{path}: [profiles] must be a table of tables")
    for name, profile in profiles.items():
        if not isinstance(profile, dict):
            raise ConfigError(f"{path}: [profiles.{name}] must be a table")
        _check(path, profile, frozenset(SETTINGS), f"[profiles.{name}]")
    keys = data.get("keys", {})
    if not isinstance(keys, dict) or not all(isinstance(v, str) for v in keys.values()):
        raise ConfigError(f'{path}: [keys] maps a key\'s name to a key, as new = "ctrl+n"')
    return data


def _check(path: Path, table: dict[str, Any], known: frozenset[str], where: str) -> None:
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(
            f"{path}: {where} has {', '.join(unknown)}, which this version does not know "
            f"(it knows {', '.join(sorted(known))})"
        )
    for name in set(table) & set(SETTINGS):
        if not isinstance(table[name], str):
            raise ConfigError(f"{path}: {name} in {where} must be a string")


def resolve(
    config: dict[str, Any],
    *,
    flags: Mapping[str, str | None],
    env: Mapping[str, str] = os.environ,
    profile: str | None = None,
    path: Path | None = None,
) -> Settings:
    """Each setting from the first place that has it: flags, environment,
    profile, the file's top level."""
    profiles: dict[str, dict[str, Any]] = config.get("profiles", {})
    chosen = profile or env.get(PROFILE_ENV) or config.get("default_profile")
    if chosen is not None and chosen not in profiles:
        known = ", ".join(sorted(profiles)) or "none"
        where = f" in {path}" if path else ""
        raise ConfigError(f"No profile named {chosen}{where} (profiles: {known})")
    layers: list[tuple[str, Mapping[str, Any]]] = [("flag", flags)]
    layers.append(("environment", {k: env.get(v) for k, v in SETTINGS.items() if v}))
    if chosen is not None:
        layers.append((f"profile {chosen}", profiles[chosen]))
    layers.append(("config file", config))
    values: dict[str, str | None] = {}
    sources: dict[str, str] = {}
    for name in SETTINGS:
        values[name], sources[name] = None, "default"
        for source, layer in layers:
            value = layer.get(name)
            if isinstance(value, str) and value.strip():
                values[name], sources[name] = value.strip(), source
                break
    return Settings(values, sources, chosen, path, dict(config.get("keys", {})))


def keyring_entry(base_url: str, profile: str | None) -> str:
    """The keyring entry a sign-in is kept under: the server's origin, and
    the profile's name beside it when there is one, so each profile signs
    in on its own."""
    return base_url if profile is None else f"{base_url} [{profile}]"
