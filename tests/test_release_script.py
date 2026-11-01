"""The release workflow's gate and notes: a tag publishes only the version
the package says it is, and the GitHub release's text is the CHANGELOG's
section for it."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import matterwave_tui

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.py"

spec = importlib.util.spec_from_file_location("release", SCRIPT)
assert spec is not None and spec.loader is not None
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)

CHANGELOG = """# Changelog

## [Unreleased]

### Fixed

- Something not yet released.

## [1.2.0] - 2026-11-01

### Added

- A thing — with a dash.

## [1.1.0] - 2026-10-01

### Fixed

- An older thing.

[Unreleased]: https://github.com/MatterWaveAI/tui/compare/v1.2.0...HEAD
[1.2.0]: https://github.com/MatterWaveAI/tui/compare/v1.1.0...v1.2.0
"""


def test_the_package_version_is_read_from_the_package():
    assert release.package_version() == matterwave_tui.__version__


def test_a_tag_must_name_the_package_version():
    assert release.check_tag("v1.2.0", "1.2.0") is None
    assert "expected v1.2.0" in release.check_tag("v1.2.1", "1.2.0")
    assert release.check_tag("1.2.0", "1.2.0") is not None


def test_the_notes_are_the_versions_section():
    assert release.notes(CHANGELOG, "1.2.0") == "### Added\n\n- A thing — with a dash.\n"
    # The last section stops at the link definitions, not the end of file.
    assert release.notes(CHANGELOG, "1.1.0") == "### Fixed\n\n- An older thing.\n"
    with pytest.raises(SystemExit, match="no section for 9.9.9"):
        release.notes(CHANGELOG, "9.9.9")


def test_the_real_changelog_has_a_section_for_the_package_version():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release.notes(text, matterwave_tui.__version__).strip()


def test_the_command_line_gate_exits_nonzero_on_a_wrong_tag():
    def run(*args: str) -> subprocess.CompletedProcess[bytes]:
        # This interpreter running this repository's own script.
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True)  # noqa: S603

    assert run("check-tag", f"v{matterwave_tui.__version__}").returncode == 0
    wrong = run("check-tag", "v0.0.0")
    assert wrong.returncode == 1
    assert b"does not name the package version" in wrong.stderr
