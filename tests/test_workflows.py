"""The CI and release workflows hold what the repository promises of them.
They are YAML a test cannot run, so these read them as text: the parts
that matter are single lines, and a change that drops one fails here
before it fails on GitHub."""

from __future__ import annotations

from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_ci_runs_on_every_branch_and_pull_request():
    ci = read("ci.yml")
    assert 'push:\n    branches: ["**"]\n  pull_request:' in ci
