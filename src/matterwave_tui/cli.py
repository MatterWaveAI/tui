"""`matterwave-tui`: sign in, then open the app.

    matterwave-tui                     open (signs in by device code if needed)
    matterwave-tui login [--key [K]]   store a key, or sign in by device code
    matterwave-tui logout              forget the stored credential
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable

from .api import DEFAULT_BASE_URL, HttpChatAPI
from .auth import (
    Credential,
    DeviceCode,
    LoginError,
    delete_token,
    device_login,
    load_token,
    resolve_base_url,
    resolve_credential,
    save_token,
)

# Asked for interactively when `--key` is given with no value, so the key is
# not left in the shell history or the process list.
_PROMPT = object()


def _print_code(code: DeviceCode) -> None:
    url = code.verification_uri_complete or code.verification_uri
    print(f"To sign in, open:\n\n    {url}\n\nand enter the code:  {code.user_code}\n")
    print("Waiting for approval…", flush=True)


def _key_arg(value: object) -> str | None:
    if value is _PROMPT:
        return getpass.getpass("MatterWave API key: ").strip() or None
    return value if isinstance(value, str) else None


def _login(base_url: str, key: str | None) -> str:
    if key:
        save_token(base_url, key)
        print(f"Key stored in the OS keyring for {base_url}.")
        return key
    token = asyncio.run(device_login(base_url, _print_code)).access_token
    save_token(base_url, token)
    print(f"Signed in. The credential is in the OS keyring for {base_url}.")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="matterwave-tui", description="MatterWave in a terminal.")
    parser.add_argument(
        "--base-url", help=f"API origin (or MATTERWAVE_BASE_URL; default {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--key",
        nargs="?",
        const=_PROMPT,
        help="an mw_ API key; with no value, prompts for it. Stored in the OS keyring.",
    )
    sub = parser.add_subparsers(dest="command")
    login = sub.add_parser("login", help="store a key, or sign in by device code")
    login.add_argument("--key", nargs="?", const=_PROMPT, dest="login_key")
    sub.add_parser("logout", help="forget the stored credential for this deployment")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_url = resolve_base_url(args.base_url, DEFAULT_BASE_URL)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        if args.command == "logout":
            removed = delete_token(base_url)
            print("Signed out." if removed else "Nothing was stored for this deployment.")
            return 0
        if args.command == "login":
            _login(base_url, _key_arg(args.login_key) or _key_arg(args.key))
            return 0
        key = _key_arg(args.key)
        if key:
            save_token(base_url, key)
        credential = resolve_credential(base_url, key)
        if credential is None:
            credential = Credential(_login(base_url, None), "keyring")
    except LoginError as exc:
        print(exc, file=sys.stderr)
        return 1
    run_app(base_url, credential)
    return 0


def run_app(base_url: str, credential: Credential) -> None:
    from .app import MatterWaveApp

    holder: dict[str, MatterWaveApp] = {}
    current = {"token": credential.token}

    async def refresh() -> str | None:
        """One fresh bearer on a 401: a credential another terminal stored
        since this one started, else a device sign-in in the app. A key the
        person passed explicitly is theirs to replace, so it is not."""
        try:
            stored = load_token(base_url)
        except LoginError:
            stored = None
        if stored and stored != current["token"]:
            current["token"] = stored
            return stored
        if credential.source in ("flag", "env"):
            return None
        token = await holder["app"].relogin()
        if token:
            save_token(base_url, token)
            current["token"] = token
        return token

    async def device(on_code: Callable[[DeviceCode], None]) -> str:
        return (await device_login(base_url, on_code)).access_token

    api = HttpChatAPI(base_url=base_url, token=credential.token, refresh=refresh)
    app = MatterWaveApp(api, relogin=device)
    holder["app"] = app
    app.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
