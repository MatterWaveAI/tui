from __future__ import annotations

import json

import httpx
import pytest

from matterwave_tui.auth import LoginError, device_login

BASE = "https://matterwave.test"


def token_endpoint(answers):
    """A fake API: the device start, then the token answers in order."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/api/auth/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dev-123",
                    "user_code": "WXYZ-1234",
                    "verification_uri": f"{BASE}/device",
                    "verification_uri_complete": f"{BASE}/device?code=WXYZ-1234",
                    "expires_in": 600,
                    "interval": 5,
                },
            )
        assert request.url.path == "/api/auth/device/token"
        assert json.loads(request.content) == {"device_code": "dev-123"}
        status, body = answers.pop(0)
        return httpx.Response(status, json=body)

    return handler, calls


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_pending_then_slow_down_then_token():
    handler, calls = token_endpoint(
        [
            (400, {"error": "authorization_pending"}),
            (400, {"error": "slow_down"}),
            (
                200,
                {"token_type": "bearer", "access_token": "mw_test_key", "scopes": ["chat:read", "chat:write"]},
            ),
        ]
    )
    clock = FakeClock()
    shown = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        token = await device_login(BASE, shown.append, http=http, sleep=clock.sleep, clock=clock)
    assert token.access_token == "mw_test_key"
    assert token.scopes == ["chat:read", "chat:write"]
    assert shown[0].user_code == "WXYZ-1234"
    # Interval 5, then 5 again, then 5 + the RFC's slow_down step.
    assert clock.sleeps == [5, 5, 10]
    assert [c.url.path for c in calls].count("/api/auth/device/token") == 3
    assert "authorization" not in {k.lower() for k in calls[0].headers}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "words"), [("access_denied", "declined"), ("expired_token", "expired")]
)
async def test_terminal_errors_end_the_login(error, words):
    handler, _ = token_endpoint([(400, {"error": error})])
    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LoginError, match=words):
            await device_login(BASE, lambda _c: None, http=http, sleep=clock.sleep, clock=clock)


@pytest.mark.asyncio
async def test_the_code_expires_on_the_clients_side_too():
    handler, _ = token_endpoint([(400, {"error": "authorization_pending"})] * 200)
    clock = FakeClock()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LoginError, match="expired"):
            await device_login(BASE, lambda _c: None, http=http, sleep=clock.sleep, clock=clock)
    assert clock.now > 600
