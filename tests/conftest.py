from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import keyring
import pytest
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError

STREAMS = Path(__file__).parent / "streams"


class MemoryKeyring(KeyringBackend):
    """A keyring that lives in the test's memory. The suite signs in with
    `--key` and logs out like a person would; none of that may land in the
    keyring of the machine running it, nor fail on a runner that has no
    keyring at all."""

    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.entries: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.entries.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.entries[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if self.entries.pop((service, username), None) is None:
            raise PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    """Every test gets a fresh in-memory keyring in place of the OS one,
    which is never even loaded."""
    backend = MemoryKeyring()
    keyring.set_keyring(backend)
    yield backend


@pytest.fixture(autouse=True)
def state_home(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every test keeps its roles and dismissed announcements in a scratch
    directory, never the developer's own."""
    root = tmp_path_factory.mktemp("state")
    monkeypatch.setenv("MATTERWAVE_STATE_DIR", str(root))
    return root


@pytest.fixture
def recorded():
    def load(name: str) -> str:
        return (STREAMS / name).read_text(encoding="utf-8")

    return load
