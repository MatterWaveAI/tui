"""The TUI, headless, against the deployed dev cluster: a real sign-in,
the real `/api` routes, grt-nano answering through the real stack.

Each test is one app run on the minted key. The conversation under test is
created and deleted through the API so the org's list is never left to
chance, and so that a run leaves nothing behind.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from textual.widgets import ListView, Markdown, OptionList, Static

from matterwave_tui.api import HttpChatAPI
from matterwave_tui.app import MatterWaveApp
from matterwave_tui.screens import ModelPicker
from matterwave_tui.widgets import MessageView

from .conftest import DevCluster, MintedKey

pytestmark = pytest.mark.e2e

# A cold model on the dev cluster can take most of a minute to load before
# the first token; the wait is generous so a slow answer is not a failure.
TURN_TIMEOUT_S = 120
POLL_S = 0.5
# The model the deployment answers with: grt-nano on the cluster, whatever
# a local API serves otherwise.
MODEL = os.environ.get("E2E_MODEL", "grt-nano")


def transcript_text(app: MatterWaveApp) -> str:
    return "\n".join(md.source for view in app.query(MessageView) for md in view.query(Markdown))


def status(app: MatterWaveApp) -> str:
    return str(app.query_one("#status", Static).render())


def sidebar_ids(app: MatterWaveApp) -> list[str]:
    return [item.conversation.id for item in app.query_one("#conversations", ListView).children]


async def settle(app: MatterWaveApp, pilot) -> None:
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def wait_for(pilot, condition, *, patience: float) -> None:
    """Poll `condition` while the app's workers run; the app's own
    `wait_for_complete` would block on the stream's whole life."""
    deadline = time.monotonic() + patience
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(f"gave up after {patience}s waiting for {condition.__name__}")
        await asyncio.sleep(POLL_S)
        await pilot.pause()


@pytest_asyncio.fixture
async def app(dev_cluster: DevCluster, api_key: MintedKey) -> AsyncIterator[MatterWaveApp]:
    # The app's own client, on the gateway's CA: the TUI accepts the client
    # so a deployment with a private root can still be reached.
    http = httpx.AsyncClient(verify=dev_cluster.ca, timeout=httpx.Timeout(30, read=None))
    api = HttpChatAPI(base_url=dev_cluster.base_url, token=api_key.key, http_client=http)
    try:
        yield MatterWaveApp(api)
    finally:
        await http.aclose()


@pytest.fixture(scope="module")
def conversation(bearer: httpx.Client) -> Iterator[dict]:
    """A conversation of this run's own, deleted after the module."""
    created = bearer.post("/api/conversations", json={"title": f"tui e2e {int(time.time())}"})
    assert created.status_code in (200, 201), f"creating a conversation: {created.text}"
    conv = created.json()
    try:
        yield conv
    finally:
        bearer.delete(f"/api/conversations/{conv['id']}")


@pytest.mark.asyncio
async def test_the_sidebar_lists_the_orgs_conversations(app: MatterWaveApp, conversation: dict):
    async with app.run_test() as pilot:
        await settle(app, pilot)
        assert app.failure is None, app.failure
        assert conversation["id"] in sidebar_ids(app)
        assert app.conversation is not None


@pytest.mark.asyncio
async def test_a_short_message_gets_a_streamed_answer(app: MatterWaveApp, conversation: dict):
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await app.open_conversation(conversation["id"])
        await settle(app, pilot)
        await pilot.press(*"Say hi in one word.", "ctrl+enter")

        def answered() -> bool:
            return app.failure is not None or (
                not app.streaming and app.thread.last("assistant") is not None
            )

        await wait_for(pilot, answered, patience=TURN_TIMEOUT_S)
        await settle(app, pilot)
        assert app.failure is None, app.failure
        answer = app.thread.last("assistant")
        assert answer is not None and answer.content.strip()
        assert answer.content.strip() in transcript_text(app)
        assert "context " in status(app)


@pytest.mark.asyncio
async def test_the_model_picker_offers_the_deployments_model(app: MatterWaveApp):
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await pilot.press("escape", "m")
        await settle(app, pilot)
        assert isinstance(app.screen, ModelPicker)
        picker = app.screen.query_one("#models", OptionList)
        shown = [str(picker.get_option_at_index(i).prompt) for i in range(picker.option_count)]
        # The list renders the model's label ("GRT Nano  (8k context)"), not
        # its id, so the row is found by id and its label looked for on screen.
        rows = [row for row in app.screen.rows if row.model_id == MODEL]
        assert rows, f"/api/models does not list {MODEL}; the picker shows {shown}"
        assert rows[0].selectable, f"{MODEL} is listed but not on the plan: {rows[0].label}"
        assert any(rows[0].label in line for line in shown), shown
        await pilot.press("escape")
        await settle(app, pilot)
        assert not isinstance(app.screen, ModelPicker)


@pytest.mark.asyncio
async def test_the_context_meter_follows_an_opened_conversation(
    app: MatterWaveApp, conversation: dict
):
    async with app.run_test() as pilot:
        await settle(app, pilot)
        await app.open_conversation(conversation["id"])
        await settle(app, pilot)
        assert app.failure is None, app.failure
        # `GET /api/conversations/{id}/context` on open answers the model's
        # window for any conversation of the org's, with or without turns.
        assert app.context_window, "the context route gave no window"
        assert "context " in status(app)
