from __future__ import annotations

from pathlib import Path

import pytest

STREAMS = Path(__file__).parent / "streams"


@pytest.fixture
def recorded():
    def load(name: str) -> str:
        return (STREAMS / name).read_text(encoding="utf-8")

    return load
