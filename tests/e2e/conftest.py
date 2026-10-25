"""The TUI against a deployed cluster, as a person would use it: one
device key, minted for the run and revoked after it.

The key comes from a device login with the browser's part played here: a
password sign-in of the standing e2e account, done the way the web app
does it (the API's login redirect to the IdP, the password route, the
session cookie), then that session approves the code and the poll hands
out the key. A device key is what the TUI itself gets when a person signs
it in, and it is minted on every plan. The sign-in happens once per
session: the IdP allows ten logins per address per quarter hour, and a
key per test would spend that on a single run.

Nothing here runs without E2E_ADMIN_EMAIL and E2E_ADMIN_PASSWORD; the
plain `pytest` run skips the whole directory. E2E_AUTH=none is the
exception, for an API started on a laptop with MW_AUTH_MODE=none: no
account and no sign-in, the dev principal approves the code.
"""

from __future__ import annotations

import os
import ssl
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
# The IdP and the app bounce a sign-in between them several times before
# the password form; more hops than this is a loop, not a sign-in.
MAX_REDIRECTS = 20


@dataclass(frozen=True)
class DevCluster:
    base_url: str
    email: str
    password: str
    # E2E_CA_FILE's authority, or True (the system store); a `verify=`
    # for every client here.
    ca: ssl.SSLContext | bool
    # E2E_AUTH=none: the API runs with MW_AUTH_MODE=none, nobody signs in.
    no_auth: bool


@dataclass(frozen=True)
class MintedKey:
    id: str
    key: str


def _ca() -> ssl.SSLContext | bool:
    """E2E_CA_FILE (absolute or relative to the repo root) for a deployment
    behind a private CA; the system's authorities when it is unset."""
    given = os.environ.get("E2E_CA_FILE")
    if not given:
        return True
    path = Path(given)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if not path.is_file():
        pytest.fail(f"CA file {path} does not exist (set E2E_CA_FILE)")
    return ssl.create_default_context(cafile=str(path))


@pytest.fixture(scope="session")
def dev_cluster() -> DevCluster:
    email = os.environ.get("E2E_ADMIN_EMAIL", "")
    password = os.environ.get("E2E_ADMIN_PASSWORD", "")
    no_auth = os.environ.get("E2E_AUTH") == "none"
    if not no_auth and (not email or not password):
        pytest.skip("E2E_ADMIN_EMAIL / E2E_ADMIN_PASSWORD are not set; no cluster to test against")
    base_url = os.environ.get("E2E_BASE_URL", "").rstrip("/")
    if not base_url:
        pytest.skip("E2E_BASE_URL is not set; no deployment to test against")
    return DevCluster(
        base_url=base_url,
        email=email,
        password=password,
        ca=_ca() if base_url.startswith("https:") else True,
        no_auth=no_auth,
    )


def _follow(client: httpx.Client, url: str, *, until: str | None = None) -> httpx.Response:
    """GET `url` and walk its redirects by hand, so the hop whose target
    carries `until` in its query can be stopped at and read. Without
    `until`, walks to the first non-redirect."""
    response = client.get(url)
    for _ in range(MAX_REDIRECTS):
        if not response.is_redirect:
            return response
        target = urljoin(str(response.url), response.headers["location"])
        if until is not None and until in target:
            return response
        response = client.get(target)
    pytest.fail(f"more than {MAX_REDIRECTS} redirects from {url}")


def _sign_in(client: httpx.Client, cluster: DevCluster) -> None:
    """The browser's password sign-in, headless: ends with the session and
    CSRF cookies on `client`."""
    bounced = _follow(client, f"{cluster.base_url}/api/auth/login", until="authRequest=")
    location = urljoin(str(bounced.url), bounced.headers.get("location", ""))
    auth_request = parse_qs(urlsplit(location).query).get("authRequest", [""])[0]
    if not auth_request:
        pytest.fail(f"the login redirect never reached the password form (last: {location})")
    answered = client.post(
        f"{cluster.base_url}/api/auth/password",
        json={"email": cluster.email, "password": cluster.password, "auth_request": auth_request},
    )
    if answered.status_code != 200:
        pytest.fail(f"password sign-in refused: {answered.status_code} {answered.text}")
    _follow(client, answered.json()["redirect"])
    if not client.cookies.get("mw_session") or not client.cookies.get("mw_csrf"):
        pytest.fail("signed in, but the session cookies did not arrive")


def _device_login(http: httpx.Client, cluster: DevCluster, csrf: dict[str, str]) -> MintedKey:
    """The device login a person does, the browser's part played by the
    signed-in `http`: start, approve, poll. The approval answers with the
    key's row (its id, for the revoke), the poll with its value."""
    base = cluster.base_url
    started = http.post(
        f"{base}/api/auth/device", json={"client_name": f"tui e2e {int(time.time())}"}
    )
    if started.status_code != 200:
        pytest.fail(f"starting a device login failed: {started.status_code} {started.text}")
    approved = http.post(
        f"{base}/api/auth/device/approve",
        json={"user_code": started.json()["user_code"]},
        headers=csrf,
    )
    if approved.status_code != 200:
        pytest.fail(f"approving the device login failed: {approved.status_code} {approved.text}")
    polled = http.post(
        f"{base}/api/auth/device/token", json={"device_code": started.json()["device_code"]}
    )
    if polled.status_code != 200:
        pytest.fail(f"polling the device login failed: {polled.status_code} {polled.text}")
    return MintedKey(id=str(approved.json()["key"]["id"]), key=str(polled.json()["access_token"]))


@pytest.fixture(scope="session")
def api_key(dev_cluster: DevCluster) -> Iterator[MintedKey]:
    """One device key for the whole run, revoked whatever happens to the
    tests."""
    with httpx.Client(verify=dev_cluster.ca, follow_redirects=False, timeout=30) as http:
        csrf: dict[str, str] = {}
        if not dev_cluster.no_auth:
            _sign_in(http, dev_cluster)
            csrf = {"x-csrf-token": http.cookies["mw_csrf"]}
        key = _device_login(http, dev_cluster, csrf)
        try:
            yield key
        finally:
            revoked = http.delete(f"{dev_cluster.base_url}/api/keys/{key.id}", headers=csrf)
            assert revoked.status_code == 204, f"revoking the key: {revoked.status_code}"


@pytest.fixture(scope="session")
def bearer(dev_cluster: DevCluster, api_key: MintedKey) -> Iterator[httpx.Client]:
    """A plain client on the minted key, for the set-up and tidy-up the
    tests do beside the TUI."""
    with httpx.Client(
        base_url=dev_cluster.base_url,
        verify=dev_cluster.ca,
        headers={"Authorization": f"Bearer {api_key.key}"},
        timeout=30,
    ) as http:
        yield http
