"""The agent contract's conformance cases (`CONTRACT["conformance"]`), run
through the TUI's own tools: the paths through `LocalTools.confine` and the
write preparation, the tool cases through `LocalTools.read`, the ignore
verdicts through the search rules the tools use, and the run_shell cases
through the output buffer and result `run_command` builds. Every client
runs the same cases, so a TUI that drifts from the contract fails here."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from matterwave_sdk import agent as contract

from matterwave_tui.agent.shell import OutputBuffer
from matterwave_tui.agent.tools import LocalTools, PendingWrite, ToolOutcome

CASES: Mapping[str, Any] = contract.CONTRACT["conformance"]


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [_thaw(v) for v in value]
    return value


def _id(case: Mapping[str, Any]) -> str:
    return "|".join(f"{k}={case[k]!r}" for k in ("tool", "path", "args", "limits") if k in case)[:90]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("conformance") / "ws"
    root.mkdir()
    for relative, text in CASES["tree"].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(str(text).encode("utf-8"))
    return root


def test_the_contract_is_1_1_and_carries_every_kind_of_case():
    assert contract.CONTRACT_VERSION == "1.1.0"
    assert all(CASES[kind] for kind in ("paths", "tools", "ignore", "shell"))


@pytest.mark.parametrize("case", CASES["paths"], ids=_id)
def test_paths(workspace: Path, case: Mapping[str, Any]):
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
    cap = (limits or contract.LIMITS)["shell_output_max_bytes"]
    buffer = OutputBuffer(cap)
    data = str(case["output"]).encode("utf-8")
    # In small pieces, as a pipe delivers it.
    for i in range(0, len(data), 3):
        buffer.feed(data[i : i + 3])
    result = buffer.result(
        case["exit_code"], timed_out=bool(case.get("timed_out")), stopped=bool(case.get("stopped"))
    )
    assert result.render(case.get("timeout_s"), limits=limits) == case["expected"]
