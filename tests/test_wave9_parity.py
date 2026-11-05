"""Unit tests for Wave 9 TUI parity: `MatterWaveClient` helpers and interactive slash commands."""

from __future__ import annotations

import json
from typing import Any, cast

import httpx
import pytest
from textual.widgets import TextArea

from matterwave_tui.api import ChatAPI
from matterwave_tui.app import MatterWaveApp
from matterwave_tui.client import MatterWaveClient
from matterwave_tui.commands import (
    SLASH_COMMAND_DESCRIPTIONS,
    SLASH_COMMAND_SPECS,
    SLASH_COMMANDS,
)
from matterwave_tui.models import GRT_HF_MODELS, GRT_HF_ORG, GRT_HF_ORG_URL
from matterwave_tui.screens import HelpScreen
from matterwave_tui.widgets import MessageView
from tests.fake_api import FakeAPI


async def _settle(app: MatterWaveApp, pilot: Any) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


@pytest.mark.asyncio
async def test_matterwave_client_wave9_httpx_fallback_when_sdk_mock_lacks_methods() -> None:
    """When the underlying SDK mock lacks Wave 9 methods, `MatterWaveClient` falls back to httpx."""
    recorded: list[tuple[str, str, dict[str, str], dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        params = dict(request.url.params)
        recorded.append((request.method, request.url.path, params, body))

        if request.method == "GET" and request.url.path == "/api/v1/connectors/catalog":
            return httpx.Response(
                200,
                json={
                    "connectors": [
                        {
                            "provider_id": "github",
                            "name": "GitHub",
                            "category": params.get("category", "developer"),
                            "auth_types": ["oauth2", "pat"],
                        }
                    ],
                    "total": 1,
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/oauth/proxy/cred-123":
            return httpx.Response(
                200,
                json={
                    "credential_id": "cred-123",
                    "provider_id": "github",
                    "method": body["method"] if body else "GET",
                    "target_url": body["endpoint"] if body else "",
                    "status_code": 200,
                    "auto_refreshed": False,
                    "data": {"login": "octocat"},
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/runs/run-42/decision-trail":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-42",
                    "nodes": [
                        {
                            "id": "node-1",
                            "type": "plan",
                            "label": "Select connector tool",
                            "confidence": 0.97,
                            "outcome": "completed",
                        }
                    ],
                    "edges": [],
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/runs/run-42/replay":
            return httpx.Response(
                200,
                json={
                    "replay_id": "rep-1",
                    "run_id": "run-42",
                    "mode": body["mode"] if body else "simulated",
                    "success": True,
                    "differences": [
                        {
                            "step_id": "step-1",
                            "field": "output",
                            "original": "old",
                            "replayed": "new",
                            "significance": "minor",
                        }
                    ],
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/runs/run-42/checkpoints":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-42",
                    "checkpoints": [
                        {
                            "checkpoint_id": "ckpt-9",
                            "run_id": "run-42",
                            "step_id": "step-1",
                            "step_index": 1,
                        }
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/runs/run-42/resume":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-43",
                    "source_run_id": "run-42",
                    "resumed_from_checkpoint_id": body.get("checkpoint_id") if body else "ckpt-9",
                    "resumed_from_step": "step-1",
                    "state": "running",
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/reliability/metrics":
            return httpx.Response(
                200,
                json={
                    "agent_id": params.get("agent_id"),
                    "sr": 0.99,
                    "cr": 0.97,
                    "pc": 0.95,
                    "hir": 0.02,
                    "ma": 0.96,
                    "tcl": 110.0,
                    "wct": 950.0,
                    "total_runs": 25,
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/guardrails/evaluate":
            return httpx.Response(
                200,
                json={
                    "rail_type": body.get("rail_type") if body else "input",
                    "action": "allow",
                    "allowed": True,
                    "modified_content": body.get("content") if body else "",
                    "triggered_rails": [],
                    "violations": [],
                },
            )
        if request.method == "POST" and request.url.path == "/api/v1/tools/plan":
            return httpx.Response(
                200,
                json={
                    "plan_id": "plan-99",
                    "goal": body.get("goal") if body else "",
                    "steps": [{"step_id": "s1", "tool_name": "search_web"}],
                    "confidence": 0.91,
                },
            )
        return httpx.Response(404, json={"detail": "Not found"})

    class BareSdkMock:
        """Mock SDK object that does not implement any Wave 9 methods."""

        base_url = "https://matterwave.test"
        api_key = "mw_test_token"

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = MatterWaveClient(BareSdkMock(), http_client=http_client)

        catalog = await client.list_connector_catalog(category="developer", q="git")
        assert catalog["connectors"][0]["provider_id"] == "github"

        proxy = await client.proxy_oauth_request(
            "cred-123", "/user", method="POST", body={"ping": True}
        )
        assert proxy["data"]["login"] == "octocat"

        trail = await client.get_run_decision_trail("run-42")
        assert trail["nodes"][0]["id"] == "node-1"

        replay = await client.replay_run("run-42", mode="strict")
        assert replay["mode"] == "strict"
        assert replay["differences"][0]["field"] == "output"

        ckpts = await client.list_run_checkpoints("run-42")
        assert ckpts["checkpoints"][0]["checkpoint_id"] == "ckpt-9"

        resumed = await client.resume_run("run-42", checkpoint_id="ckpt-9")
        assert resumed["run_id"] == "run-43"

        rel = await client.get_reliability_metrics(agent_id="agent-alpha")
        assert rel["sr"] == 0.99
        assert rel["agent_id"] == "agent-alpha"

        guard = await client.evaluate_guardrails("safe text", rail_type="input")
        assert guard["allowed"] is True

        plan = await client.plan_tools("Audit connectors")
        assert plan["plan_id"] == "plan-99"

    assert recorded == [
        ("GET", "/api/v1/connectors/catalog", {"category": "developer", "q": "git"}, None),
        (
            "POST",
            "/api/v1/oauth/proxy/cred-123",
            {},
            {"endpoint": "/user", "method": "POST", "body": {"ping": True}},
        ),
        ("GET", "/api/v1/runs/run-42/decision-trail", {}, None),
        ("POST", "/api/v1/runs/run-42/replay", {}, {"mode": "strict"}),
        ("GET", "/api/v1/runs/run-42/checkpoints", {}, None),
        ("POST", "/api/v1/runs/run-42/resume", {}, {"checkpoint_id": "ckpt-9"}),
        ("GET", "/api/v1/reliability/metrics", {"agent_id": "agent-alpha"}, None),
        (
            "POST",
            "/api/v1/guardrails/evaluate",
            {},
            {"content": "safe text", "rail_type": "input"},
        ),
        ("POST", "/api/v1/tools/plan", {}, {"goal": "Audit connectors"}),
    ]


@pytest.mark.asyncio
async def test_matterwave_client_wave9_delegates_to_sdk_methods_when_present() -> None:
    """When the SDK object provides Wave 9 methods, `MatterWaveClient` delegates directly."""
    calls: list[tuple[str, Any]] = []

    class FullWave9SdkMock:
        base_url = "https://matterwave.test"

        async def list_connector_catalog(
            self, category: str | None = None, q: str | None = None
        ) -> dict[str, Any]:
            calls.append(("list_connector_catalog", (category, q)))
            return {"connectors": [{"provider_id": "slack", "category": category}]}

        async def proxy_oauth_request(
            self,
            credential_id: str,
            endpoint: str,
            method: str = "GET",
            body: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            calls.append(("proxy_oauth_request", (credential_id, endpoint, method, body)))
            return {"credential_id": credential_id, "status_code": 200}

        async def get_run_decision_trail(self, run_id: str) -> dict[str, Any]:
            calls.append(("get_run_decision_trail", (run_id,)))
            return {"run_id": run_id, "nodes": [{"id": "n-sdk", "confidence": 0.99}]}

        async def replay_run(self, run_id: str, mode: str = "simulated") -> dict[str, Any]:
            calls.append(("replay_run", (run_id, mode)))
            return {"replay_id": "rep-sdk", "run_id": run_id, "mode": mode, "differences": []}

        async def list_run_checkpoints(self, run_id: str) -> dict[str, Any]:
            calls.append(("list_run_checkpoints", (run_id,)))
            return {"run_id": run_id, "checkpoints": [{"checkpoint_id": "ckpt-sdk"}]}

        async def resume_run(self, run_id: str, checkpoint_id: str | None = None) -> dict[str, Any]:
            calls.append(("resume_run", (run_id, checkpoint_id)))
            return {"run_id": "run-resumed", "resumed_from_checkpoint_id": checkpoint_id}

        async def get_reliability_metrics(self, agent_id: str | None = None) -> dict[str, Any]:
            calls.append(("get_reliability_metrics", (agent_id,)))
            return {"agent_id": agent_id, "sr": 1.0, "cr": 1.0}

        async def evaluate_guardrails(
            self, content: str, rail_type: str = "input"
        ) -> dict[str, Any]:
            calls.append(("evaluate_guardrails", (content, rail_type)))
            return {"allowed": True, "rail_type": rail_type, "modified_content": content}

        async def plan_tools(self, goal: str) -> dict[str, Any]:
            calls.append(("plan_tools", (goal,)))
            return {"plan_id": "plan-sdk", "goal": goal}

    client = MatterWaveClient(FullWave9SdkMock(), token="mw_sdk")
    try:
        assert (await client.list_connector_catalog(category="communication", q="sl"))[
            "connectors"
        ][0]["provider_id"] == "slack"
        assert (await client.proxy_oauth_request("cred-1", "/v1/me", method="GET"))[
            "credential_id"
        ] == "cred-1"
        assert (await client.get_run_decision_trail("run-1"))["nodes"][0]["id"] == "n-sdk"
        assert (await client.replay_run("run-1", mode="counterfactual"))["mode"] == "counterfactual"
        assert (await client.list_run_checkpoints("run-1"))["checkpoints"][0][
            "checkpoint_id"
        ] == "ckpt-sdk"
        assert (await client.resume_run("run-1", checkpoint_id="ckpt-sdk"))[
            "resumed_from_checkpoint_id"
        ] == "ckpt-sdk"
        assert (await client.get_reliability_metrics(agent_id="a-1"))["sr"] == 1.0
        assert (await client.evaluate_guardrails("hello", rail_type="output"))["allowed"] is True
        assert (await client.plan_tools("ship release"))["plan_id"] == "plan-sdk"
    finally:
        await client.aclose()

    assert [name for name, _ in calls] == [
        "list_connector_catalog",
        "proxy_oauth_request",
        "get_run_decision_trail",
        "replay_run",
        "list_run_checkpoints",
        "resume_run",
        "get_reliability_metrics",
        "evaluate_guardrails",
        "plan_tools",
    ]


@pytest.mark.asyncio
async def test_interactive_wave9_slash_commands() -> None:
    """Verify `/connectors`, `/trail`, `/replay`, `/checkpoints`, `/reliability`, and `/guardrails`."""
    for cmd in (
        "/connectors",
        "/trail",
        "/replay",
        "/checkpoints",
        "/reliability",
        "/guardrails",
    ):
        assert cmd in SLASH_COMMANDS
        assert cmd in SLASH_COMMAND_DESCRIPTIONS
        assert any(spec.name == cmd for spec in SLASH_COMMAND_SPECS)

    api = FakeAPI()
    app = MatterWaveApp(cast(ChatAPI, api))
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        composer = app.query_one("#composer", TextArea)

        # 1. `/connectors` lists all catalog providers, `/connectors developer` filters by category
        composer.text = "/connectors"
        app.action_send()
        await _settle(app, pilot)
        assert composer.text == ""
        assert api.connector_catalog_queries[-1] == (None, None)
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Connector catalog (2):" in last_text
        assert "github (GitHub) [developer]" in last_text
        assert "slack (Slack) [communication]" in last_text

        composer.text = "/connectors developer"
        app.action_send()
        await _settle(app, pilot)
        assert api.connector_catalog_queries[-1] == ("developer", None)
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Connector catalog (developer, 1):" in last_text
        assert "github (GitHub)" in last_text
        assert "slack (Slack)" not in last_text

        # 2. `/trail` without run_id warns with usage; `/trail <run_id>` renders nodes & confidence
        shown.clear()
        composer.text = "/trail"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /trail <run_id>" in s for s in shown)

        composer.text = "/trail run-101"
        app.action_send()
        await _settle(app, pilot)
        assert api.decision_trail_queries == ["run-101"]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Decision trail for run-101 (2 nodes):" in last_text
        assert "node-1: Select search and analysis tools" in last_text
        assert "confidence=0.96" in last_text

        # 3. `/replay` without args warns; `/replay <run_id> [mode]` replays and shows differences
        shown.clear()
        composer.text = "/replay"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /replay <run_id>" in s for s in shown)

        shown.clear()
        composer.text = "/replay run-101 unknown_mode"
        app.action_send()
        await _settle(app, pilot)
        assert any("Invalid replay mode" in s for s in shown)

        composer.text = "/replay run-101 counterfactual"
        app.action_send()
        await _settle(app, pilot)
        assert api.replay_calls == [("run-101", "counterfactual")]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Replay rep-run-101 for run-101 (mode=counterfactual" in last_text
        assert "step-2.tool_output: 'cached-v1' -> 'recomputed-v2' [minor]" in last_text

        # 4. `/checkpoints <run_id>` lists checkpoints; `/checkpoints <run_id> resume <checkpoint_id>` resumes
        shown.clear()
        composer.text = "/checkpoints"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /checkpoints <run_id>" in s for s in shown)

        composer.text = "/checkpoints run-101"
        app.action_send()
        await _settle(app, pilot)
        assert api.checkpoint_queries == ["run-101"]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Checkpoints for run-101 (2):" in last_text
        assert "ckpt-1: step=step-1 (step #1)" in last_text
        assert "ckpt-2: step=step-2 (step #2)" in last_text

        composer.text = "/checkpoints run-101 resume ckpt-1"
        app.action_send()
        await _settle(app, pilot)
        assert api.resume_run_calls == [("run-101", "ckpt-1")]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Resumed run run-101 from checkpoint ckpt-1" in last_text
        assert "run=run-101-resumed [running]" in last_text

        # 5. `/reliability [agent_id]` renders the 7 ia_modules metrics (SR, CR, PC, HIR, MA, TCL, WCT)
        composer.text = "/reliability agent-coder"
        app.action_send()
        await _settle(app, pilot)
        assert api.reliability_queries == ["agent-coder"]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Reliability metrics (agent-coder, runs=50):" in last_text
        for metric_code in ("SR", "CR", "PC", "HIR", "MA", "TCL", "WCT"):
            assert f"• {metric_code} (" in last_text

        # 6. `/guardrails <text>` evaluates content against the Guardrails engine
        shown.clear()
        composer.text = "/guardrails"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /guardrails <text>" in s for s in shown)

        composer.text = "/guardrails Summarize the latest release notes"
        app.action_send()
        await _settle(app, pilot)
        assert api.guardrail_evaluations[-1] == ("Summarize the latest release notes", "input")
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Guardrails evaluation (input): verdict=allowed | action=allow" in last_text

        composer.text = "/guardrails unsafe drop table users"
        app.action_send()
        await _settle(app, pilot)
        assert api.guardrail_evaluations[-1] == ("unsafe drop table users", "input")
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Guardrails evaluation (input): verdict=blocked | action=block" in last_text
        assert "Blocked by input safety policy." in last_text
        assert "Unsafe pattern detected" in last_text

        # 7. `help_rows()` includes all Wave 9 slash commands
        help_entries = {usage: desc for usage, desc, _ in app.help_rows()}
        assert "/connectors [category]" in help_entries
        assert "/trail <run_id>" in help_entries
        assert "/replay <run_id> [mode]" in help_entries
        assert "/checkpoints <run_id> [resume <checkpoint_id>]" in help_entries
        assert "/reliability [agent_id]" in help_entries
        assert "/guardrails <text>" in help_entries

        composer.text = "/help"
        app.action_send()
        await _settle(app, pilot)
        assert isinstance(app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_grt_9_model_suite_rerank_and_tts_client_and_slash_commands() -> None:
    """Verify the 9-model MatterWaveAI GRT HF suite, client methods, and `/rerank` & `/tts`."""
    assert GRT_HF_ORG == "MatterWaveAI"
    assert GRT_HF_ORG_URL == "https://huggingface.co/MatterWaveAI"
    assert len(GRT_HF_MODELS) == 9
    expected_repos = {
        "MatterWaveAI/grt-30m",
        "MatterWaveAI/grt-30m-chat",
        "MatterWaveAI/grt-30m-mor",
        "MatterWaveAI/grt-30m-embedding",
        "MatterWaveAI/grt-30m-reranker",
        "MatterWaveAI/grt-30m-vl",
        "MatterWaveAI/grt-30m-tts",
        "MatterWaveAI/grt-30m-omni",
        "MatterWaveAI/grt-30m-tokenizer",
    }
    assert {m["repo_id"] for m in GRT_HF_MODELS} == expected_repos

    for cmd in ("/rerank", "/tts"):
        assert cmd in SLASH_COMMANDS
        assert cmd in SLASH_COMMAND_DESCRIPTIONS
        assert any(spec.name == cmd for spec in SLASH_COMMAND_SPECS)

    recorded: list[tuple[str, str, dict[str, Any] | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        recorded.append((request.method, request.url.path, body))
        if request.method == "POST" and request.url.path == "/v1/rerank":
            return httpx.Response(
                200,
                json={
                    "model": body["model"] if body else "MatterWaveAI/grt-30m-reranker",
                    "results": [
                        {
                            "index": 1,
                            "relevance_score": 0.94,
                            "document": {"text": body["documents"][1] if body else "doc2"},
                        },
                        {
                            "index": 0,
                            "relevance_score": 0.51,
                            "document": {"text": body["documents"][0] if body else "doc1"},
                        },
                    ],
                },
            )
        if request.method == "POST" and request.url.path == "/v1/audio/speech":
            return httpx.Response(
                200,
                json={
                    "model": body["model"] if body else "MatterWaveAI/grt-30m-tts",
                    "voice": body["voice"] if body else "aurora",
                    "format": "wav",
                    "sample_rate_hz": 24000,
                    "duration_ms": 660,
                    "audio_base64": "UklGRg==",
                },
            )
        if request.method == "GET" and request.url.path == "/api/v1/models/grt-suite":
            return httpx.Response(
                200,
                json={
                    "organization": GRT_HF_ORG,
                    "hf_url": GRT_HF_ORG_URL,
                    "total": 9,
                    "models": [dict(m) for m in GRT_HF_MODELS],
                },
            )
        return httpx.Response(404, json={"detail": "Not found"})

    class BareSdk:
        base_url = "https://matterwave.test"
        api_key = "mw_test_token"

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http_client:
        client = MatterWaveClient(BareSdk(), http_client=http_client)
        reranked = await client.rerank("vector search", ["irrelevant", "vector search index"])
        assert reranked["results"][0]["index"] == 1
        speech = await client.synthesize_speech("Hello MatterWave", voice="aurora")
        assert speech["model"] == "MatterWaveAI/grt-30m-tts"
        assert speech["voice"] == "aurora"
        suite = await client.get_grt_suite()
        assert suite["total"] == 9
        assert len(suite["models"]) == 9

    assert recorded == [
        (
            "POST",
            "/v1/rerank",
            {
                "query": "vector search",
                "documents": ["irrelevant", "vector search index"],
                "model": "MatterWaveAI/grt-30m-reranker",
            },
        ),
        (
            "POST",
            "/v1/audio/speech",
            {
                "input": "Hello MatterWave",
                "text": "Hello MatterWave",
                "model": "MatterWaveAI/grt-30m-tts",
                "voice": "aurora",
            },
        ),
        ("GET", "/api/v1/models/grt-suite", None),
    ]

    api = FakeAPI()
    suite_fake = await api.get_grt_suite()
    assert suite_fake["total"] == 9
    assert api.grt_suite_queries == 1

    app = MatterWaveApp(cast(ChatAPI, api))
    shown: list[str] = []
    app.notify = lambda message, **_: shown.append(str(message))  # type: ignore[method-assign]

    async with app.run_test() as pilot:
        await _settle(app, pilot)
        composer = app.query_one("#composer", TextArea)

        shown.clear()
        composer.text = "/rerank missing pipe"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /rerank <query> | <doc1> ; <doc2>" in s for s in shown)

        composer.text = "/rerank neural reranker | Legacy keyword index ; Neural reranker scoring"
        app.action_send()
        await _settle(app, pilot)
        assert api.rerank_calls == [
            (
                "neural reranker",
                ["Legacy keyword index", "Neural reranker scoring"],
                "MatterWaveAI/grt-30m-reranker",
            )
        ]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert (
            "Rerank results for 'neural reranker' (MatterWaveAI/grt-30m-reranker, 2 docs):"
            in last_text
        )
        assert "[1] doc#1" in last_text
        assert "Neural reranker scoring" in last_text

        shown.clear()
        composer.text = "/tts"
        app.action_send()
        await _settle(app, pilot)
        assert any("Usage: /tts <text>" in s for s in shown)

        composer.text = "/tts Welcome to the MatterWave GRT suite"
        app.action_send()
        await _settle(app, pilot)
        assert api.speech_calls == [
            ("Welcome to the MatterWave GRT suite", "MatterWaveAI/grt-30m-tts", "aurora")
        ]
        views = list(app.query(MessageView))
        last_text = views[-1].message.content
        assert "Synthesized speech (MatterWaveAI/grt-30m-tts, voice=aurora" in last_text
        assert "Welcome to the MatterWave GRT suite" in last_text
