"""The CI and release workflows hold what the repository promises of them.
They are YAML a test cannot run, so these read them as text: the parts
that matter are single lines, and a change that drops one fails here
before it fails on GitHub."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


# A third-party action is named by the commit it runs, with its tag beside
# it for the reader: a tag can be moved to other code, a commit cannot.
PINNED = re.compile(r"uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+(\.\d+)*$")


@pytest.mark.parametrize("name", ["ci.yml", "publish-pypi.yml"])
def test_every_action_is_pinned_to_a_commit(name):
    uses = [line.strip() for line in read(name).splitlines() if line.strip().startswith("- uses:")]
    assert uses
    for line in uses:
        assert PINNED.search(line.removeprefix("- ")), line


@pytest.mark.parametrize("name", ["ci.yml", "publish-pypi.yml"])
def test_the_token_is_read_only_unless_a_job_says_otherwise(name):
    text = read(name)
    assert "\npermissions:\n  contents: read\n" in text
    # The only widenings: the publish job's OIDC token and the release
    # job's right to create the release.
    widened = re.findall(r"(?m)^ {6}([\w-]+): write$", text)
    assert widened == (["id-token", "contents"] if name == "publish-pypi.yml" else [])


def test_ci_scans_the_whole_history_for_secrets():
    ci = read("ci.yml")
    assert "fetch-depth: 0" in ci
    assert "sha256sum -c -" in ci
    assert "./gitleaks git --redact" in ci


def test_ci_runs_on_every_branch_and_pull_request():
    ci = read("ci.yml")
    assert 'push:\n    branches: ["**"]\n  pull_request:' in ci


def test_a_tag_publishes_only_its_own_version_with_notes_and_an_sbom():
    publish = read("publish-pypi.yml")
    assert 'python scripts/release.py check-tag "$TAG"' in publish
    assert "--format cyclonedx1.5" in publish
    assert "python scripts/release.py notes > notes.md" in publish
    assert "gh release create" in publish and "release/sbom.cdx.json" in publish
