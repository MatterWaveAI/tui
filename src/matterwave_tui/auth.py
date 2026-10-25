"""Sign-in: an API key, or the person by device code, kept in the OS keyring.

The bearer is never written to a dotfile. A key in a file next to the
terminal's history is a key in every backup of that home directory; the
keyring is the one store each OS already protects per user. With no usable
keyring backend, sign-in fails loudly rather than falling back to a file.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from matterwave_sdk import _base as sdk_base

from .api import USER_AGENT

KEYRING_SERVICE = "matterwave"
API_KEY_ENV = sdk_base.API_KEY_ENV
BASE_URL_ENV = sdk_base.BASE_URL_ENV
DEVICE_CLIENT_NAME = "matterwave-tui"

# RFC 8628 3.5: on `slow_down` the client adds five seconds to its interval.
SLOW_DOWN_STEP_S = 5.0
DEFAULT_INTERVAL_S = 5.0


class LoginError(Exception):
    """Sign-in did not produce a bearer; the message says why, for a person."""


class KeyringUnavailable(LoginError):
    pass


# --- the keyring ---------------------------------------------------------------


def _keyring() -> Any:
    try:
        import keyring
    except ImportError as exc:  # pragma: no cover - a hard dependency
        raise KeyringUnavailable("The keyring package is not installed.") from exc
    return keyring


def load_token(base_url: str) -> str | None:
    """The stored bearer for this deployment, keyed by its origin so a
    staging key is never sent to production."""
    try:
        value = _keyring().get_password(KEYRING_SERVICE, base_url)
    except Exception as exc:
        raise KeyringUnavailable(f"The OS keyring could not be read: {exc}") from exc
    return str(value) if value else None


def save_token(base_url: str, token: str) -> None:
    try:
        _keyring().set_password(KEYRING_SERVICE, base_url, token)
    except Exception as exc:
        raise KeyringUnavailable(
            f"The OS keyring could not store the credential ({exc}). Nothing was written "
            f"to disk; set {API_KEY_ENV} for this shell instead."
        ) from exc


def delete_token(base_url: str) -> bool:
    try:
        _keyring().delete_password(KEYRING_SERVICE, base_url)
    except Exception:
        return False
    return True


def resolve_base_url(flag: str | None, default: str) -> str:
    value = flag or os.environ.get(BASE_URL_ENV, "").strip() or default
    return sdk_base.resolve_base_url(value)


# --- device code ---------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_in: float
    interval: float


@dataclass(frozen=True)
class DeviceToken:
    access_token: str
    token_type: str
    scopes: list[str]


def _post(
    http: httpx.AsyncClient, base_url: str, path: str, body: dict[str, Any]
) -> Awaitable[httpx.Response]:
    # Unauthenticated, so built from the SDK's headers with no key.
    request = sdk_base.Request("POST", path, body, False, False)
    headers = sdk_base.headers_for(request, None, USER_AGENT)
    return http.post(base_url + path, json=body, headers=headers)


def _error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    code = payload.get("error")
    if isinstance(code, dict):
        code = code.get("code") or code.get("message")
    if code is None and isinstance(payload.get("detail"), dict):
        code = payload["detail"].get("error")
    if code is None and isinstance(payload.get("detail"), str):
        code = payload["detail"]
    return str(code) if code is not None else None


async def start_device_login(
    http: httpx.AsyncClient, base_url: str, client_name: str = DEVICE_CLIENT_NAME
) -> DeviceCode:
    response = await _post(http, base_url, "/api/auth/device", {"client_name": client_name})
    if not response.is_success:
        raise LoginError(
            f"This deployment did not start a device sign-in (HTTP {response.status_code}, "
            f"request {sdk_base.request_id_of(response) or 'unknown'}). Use --key instead."
        )
    data = response.json()
    try:
        return DeviceCode(
            device_code=str(data["device_code"]),
            user_code=str(data["user_code"]),
            verification_uri=str(data["verification_uri"]),
            verification_uri_complete=data.get("verification_uri_complete"),
            expires_in=float(data.get("expires_in", 600)),
            interval=float(data.get("interval", DEFAULT_INTERVAL_S)),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise LoginError("The device sign-in answer was missing fields.") from exc


async def poll_device_token(
    http: httpx.AsyncClient,
    base_url: str,
    code: DeviceCode,
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DeviceToken:
    """Poll until the person approves, declines, or the code expires."""
    interval = max(code.interval, 1.0)
    deadline = clock() + code.expires_in
    while True:
        await sleep(interval)
        if clock() > deadline:
            raise LoginError("The sign-in code expired before it was approved. Run login again.")
        response = await _post(
            http, base_url, "/api/auth/device/token", {"device_code": code.device_code}
        )
        if response.is_success:
            data = response.json()
            token = data.get("access_token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise LoginError("The sign-in finished without a credential.")
            scopes = data.get("scopes") or []
            return DeviceToken(
                access_token=token,
                token_type=str(data.get("token_type", "bearer")),
                scopes=[str(s) for s in scopes] if isinstance(scopes, list) else [],
            )
        error = _error_code(response)
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += SLOW_DOWN_STEP_S
            continue
        if error == "expired_token":
            raise LoginError("The sign-in code expired before it was approved. Run login again.")
        if error == "access_denied":
            raise LoginError("The sign-in was declined in the browser.")
        raise LoginError(
            f"Device sign-in failed (HTTP {response.status_code}, "
            f"request {sdk_base.request_id_of(response) or 'unknown'})."
        )


async def device_login(
    base_url: str,
    on_code: Callable[[DeviceCode], None],
    *,
    http: httpx.AsyncClient | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> DeviceToken:
    owns = http is None
    client = http or httpx.AsyncClient(timeout=sdk_base.DEFAULT_TIMEOUT_S)
    try:
        code = await start_device_login(client, base_url)
        on_code(code)
        return await poll_device_token(client, base_url, code, sleep=sleep, clock=clock)
    finally:
        if owns:
            await client.aclose()


# --- where the bearer comes from ----------------------------------------------


@dataclass(frozen=True)
class Credential:
    token: str
    # "flag", "env" or "keyring". A key the person passed explicitly is not
    # replaced behind their back on a 401; a stored one can be re-logged.
    source: str


def resolve_credential(base_url: str, key_flag: str | None) -> Credential | None:
    if key_flag:
        return Credential(key_flag, "flag")
    env = os.environ.get(API_KEY_ENV, "").strip()
    if env:
        return Credential(env, "env")
    stored = load_token(base_url)
    if stored:
        return Credential(stored, "keyring")
    return None
