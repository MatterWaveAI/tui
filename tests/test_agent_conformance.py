"""The agent contract's conformance cases (`CONTRACT["conformance"]`), run
through the TUI's own tools: the paths through `LocalTools.confine` and the
write preparation, the tool cases through `LocalTools.read` (grep through
its search process), the ignore verdicts through the search rules the tools
use, and the run_shell cases through the output buffer and result
`run_command` builds. Every client runs the same cases, so a TUI that
drifts from the contract fails here.

The tree's `links` and `fifos` are made where this platform can make them;
a case that needs one this platform could not make is skipped, saying
which. `alias_roots` pins what the SDK's `confine` makes of a root inside
folders with Windows-alias names (see test_agent_review for the TUI's tools
on such a root)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent.shell import OutputBuffer, ShellResult
from matterwave_tui.agent.tools import LocalTools, PendingWrite, ToolOutcome

CASES: Mapping[str, Any] = contract.CONTRACT["conformance"]
# Every kind of entry this runner knows; a kind added to the contract fails
# the version-and-kinds test below until this runner runs it.
KINDS = {"about", "tree", "links", "fifos", "paths", "tools", "ignore", "shell", "alias_roots"}
# What _make_specials could make on this platform.
MADE: set[str] = set()


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _id(case: Mapping[str, Any]) -> str:
    return "|".join(f"{k}={case[k]!r}" for k in ("tool", "path", "args", "limits") if k in case)[:90]


def _make_specials(root: Path) -> set[str]:
    """The tree's named pipes and links, where this platform can make them:
    the kinds it made."""
    made: set[str] = set()
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is not None:
        for relative in CASES["fifos"]:
            mkfifo(root / relative)
        made.add("fifos")
    try:
        for relative, target in CASES["links"].items():
            os.symlink(target, root / relative)
        made.add("links")
    except OSError:
        pass  # Windows without Developer Mode or admin rights cannot make links
    return made


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("conformance") / "ws"
    root.mkdir()
    for relative, text in CASES["tree"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(text).encode("utf-8"))
    MADE.update(_make_specials(root))
    return root


def _needs(case: Mapping[str, Any]) -> None:
    missing = set(case.get("needs", ())) - MADE
    if missing:
        pytest.skip(f"this platform could not make the tree's {' and '.join(sorted(missing))}")


def test_the_contract_is_1_2_and_the_runner_knows_every_kind_of_case():
    assert contract.CONTRACT_VERSION == "1.2.0"
    assert set(CASES) == KINDS
    assert all(CASES[kind] for kind in KINDS)


@pytest.mark.parametrize("case", CASES["paths"], ids=_id)
def test_paths(workspace: Path, case: Mapping[str, Any]):
    _needs(case)
    tools = LocalTools(workspace)
    write = bool(case.get("write", False))
    if "relative" in case:
        assert tools.confine(case["path"], write=write).relative == case["relative"]
    else:
        with pytest.raises(contract.PathRefused) as refused:
            tools.confine(case["path"], write=write)
        assert refused.value.reason == case["refused"]
        assert str(refused.value) == case["message"]
    if not write:
        return
    # The write tools: a protected path is worked out to be asked about
    # (confined as approved only after the user's yes); any other refusal is
    # the contract's sentence; nothing is written either way.
    prepared = tools.prepare_write("write_file", {"path": case["path"], "content": "x"})
    if case.get("refused") == "protected":
        assert isinstance(prepared, PendingWrite) and prepared.protected
    elif "refused" in case:
        assert isinstance(prepared, ToolOutcome) and prepared.is_error
        assert prepared.text == case["message"]
    else:
        assert isinstance(prepared, PendingWrite) and not prepared.protected


@pytest.mark.parametrize("case", CASES["tools"], ids=_id)
def test_tools(workspace: Path, case: Mapping[str, Any]):
    _needs(case)
    limits = case.get("limits")
    tools = LocalTools(workspace, limits=dict(limits) if limits is not None else None)
    outcome = asyncio.run(tools.read(case["tool"], _thaw(case["args"])))
    if "expected" in case:
        assert not outcome.is_error, outcome.text
        assert outcome.text == case["expected"]
    elif "message_prefix" in case:
        assert outcome.is_error and outcome.text.startswith(case["message_prefix"])
    else:
        assert outcome.is_error
        assert outcome.text == case["message"]


@pytest.mark.parametrize("case", CASES["ignore"], ids=_id)
def test_ignore(workspace: Path, case: Mapping[str, Any]):
    rules = LocalTools(workspace).search_ignore()
    verdict = rules.ignored(
        case["path"], is_dir=bool(case.get("is_dir", False)), ancestors=bool(case.get("ancestors", True))
    )
    assert verdict is case["ignored"]


@pytest.mark.parametrize("case", CASES["shell"], ids=lambda c: repr(c["output"])[:30])
def test_shell(case: Mapping[str, Any]):
    limits = dict(case["limits"]) if "limits" in case else None
    flags = {"timed_out": bool(case.get("timed_out")), "stopped": bool(case.get("stopped"))}
    if "omitted" in case:
        # The start and the end a buffer kept, and the bytes it dropped
        # from between them.
        result = ShellResult(case["exit_code"], case["output"], omitted_bytes=case["omitted"], **flags)
    else:
        buffer = OutputBuffer(contract.limit("shell_output_max_bytes", limits))
        data = str(case["output"]).encode("utf-8")
        # In small pieces, as a pipe delivers it.
        for i in range(0, len(data), 3):
            buffer.feed(data[i : i + 3])
        result = buffer.result(case["exit_code"], **flags)
    assert result.render(case.get("timeout_s"), limits=limits) == case["expected"]


@pytest.mark.parametrize("case", CASES["alias_roots"], ids=lambda c: f"{c['path']!r} in {c['root']!r}")
def test_alias_roots(case: Mapping[str, Any]):
    assert contract.windows_alias(case["path"], case["root"]) == case["refused"]
