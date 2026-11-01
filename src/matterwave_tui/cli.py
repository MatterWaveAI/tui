"""`matterwave-tui`: sign in, then open the app.

    matterwave-tui                     open (signs in by device code if needed)
    matterwave-tui -p "prompt" [-m M]  one-shot: the answer, then exit
    cat notes | matterwave-tui         a piped-in prompt is the prompt
    matterwave-tui login [--key [K]]   store a key, or sign in by device code
    matterwave-tui logout              forget the stored credential

stdout carries the answer and nothing else; sign-in prompts and every
note go to stderr, so `matterwave-tui -p "..." > answer.txt` holds only
the answer. The exit status tells a script how the turn ended (EXIT_HELP).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from collections.abc import Callable

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import APIConnectionError, APIError

from .api import DEFAULT_BASE_URL, Cut, HttpChatAPI
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
from .failures import Kind, classify

# Asked for interactively when `--key` is given with no value, so the key is
# not left in the shell history or the process list.
_PROMPT = object()

# How a one-shot ended, for a script to branch on.
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_TRUNCATED = 4
# The failures that are the platform saying no, rather than the turn going
# wrong: asking again unchanged gets the same answer.
REFUSED_KINDS: frozenset[Kind] = frozenset(
    {"moderation", "budget", "rate_limited", "plan", "context", "session", "permission"}
)
EXIT_HELP = f"""exit status:
  {EXIT_OK}  the whole answer is on stdout
  {EXIT_ERROR}  the turn failed (network, gateway, timeout, an unexpected answer)
  {EXIT_USAGE}  the command line was wrong, or there was nothing to send
  {EXIT_REFUSED}  the platform refused: moderation, budget, plan, rate limit, sign-in
  {EXIT_TRUNCATED}  the answer was cut short; what arrived is on stdout
"""


def _note(text: str) -> None:
    """Everything that is not the answer goes to stderr."""
    print(text, file=sys.stderr, flush=True)


def exit_status(kind: Kind) -> int:
    if kind == "truncated":
        return EXIT_TRUNCATED
    return EXIT_REFUSED if kind in REFUSED_KINDS else EXIT_ERROR


def _print_code(code: DeviceCode) -> None:
    url = code.verification_uri_complete or code.verification_uri
    _note(f"To sign in, open:\n\n    {url}\n\nand enter the code:  {code.user_code}\n")
    _note("Waiting for approval…")


def _key_arg(value: object) -> str | None:
    if value is _PROMPT:
        return getpass.getpass("MatterWave API key: ").strip() or None
    return value if isinstance(value, str) else None


def _login(base_url: str, key: str | None) -> str:
    if key:
        save_token(base_url, key)
        _note(f"Key stored in the OS keyring for {base_url}.")
        return key
    token = asyncio.run(device_login(base_url, _print_code)).access_token
    save_token(base_url, token)
    _note(f"Signed in. The credential is in the OS keyring for {base_url}.")
    return token


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="matterwave-tui",
        description="MatterWave in a terminal.",
        epilog=EXIT_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--base-url", help=f"API origin (or MATTERWAVE_BASE_URL; default {DEFAULT_BASE_URL})"
    )
    parser.add_argument(
        "--key",
        nargs="?",
        const=_PROMPT,
        help="an mw_ API key; with no value, prompts for it. Stored in the OS keyring.",
    )
    parser.add_argument(
        "-p",
        "--prompt",
        help="one-shot mode: send this prompt, print the answer to stdout, exit",
    )
    parser.add_argument("-m", "--model", help="the model for a one-shot prompt")
    parser.add_argument(
        "--shell",
        action="store_true",
        help="shell assistant: an answer is a proposed command, run only after you confirm",
    )
    sub = parser.add_subparsers(dest="command")
    login = sub.add_parser("login", help="store a key, or sign in by device code")
    login.add_argument("--key", nargs="?", const=_PROMPT, dest="login_key")
    sub.add_parser("logout", help="forget the stored credential for this deployment")
    return parser


def _oneshot_prompt(args: argparse.Namespace) -> str | None:
    """The one-shot prompt: `-p`, else stdin when it is a pipe rather than
    a terminal. None means interactive mode."""
    flag = args.prompt
    if flag is not None:
        return str(flag).strip()
    if sys.stdin is not None and not sys.stdin.isatty():
        return sys.stdin.read().strip()
    return None


async def run_oneshot(
    base_url: str, credential: Credential, prompt: str, model: str | None = None
) -> int:
    """One answer, no interface: a conversation is made, the prompt sent
    through the SDK's stream, and the text deltas printed as they arrive.
    The exit status says how it ended (EXIT_HELP); whatever arrived before
    a failure is already on stdout."""
    api = HttpChatAPI(base_url=base_url, token=credential.token)
    wrote = False
    try:
        conversation = await api.create_conversation(model=model)
        async for item in api.send(conversation.id, prompt, model):
            if isinstance(item, fr.DeltaFrame) and not item.reasoning:
                sys.stdout.write(item.content or "")
                sys.stdout.flush()
                wrote = wrote or bool(item.content)
            elif isinstance(item, Cut) or (
                isinstance(item, fr.DoneFrame) and item.message.truncated
            ):
                _note("(the answer was cut short; what arrived is above)")
                return EXIT_TRUNCATED
            elif isinstance(item, fr.ErrorFrame):
                _note(f"error: {item.detail}")
                return exit_status(classify(item.detail, None, item.code))
            elif isinstance(item, fr.ModerationBlockFrame):
                _note("(the answer was blocked by moderation)")
                return EXIT_REFUSED
    except APIConnectionError as exc:
        _note(f"could not reach {base_url}: {exc}")
        return EXIT_ERROR
    except APIError as exc:
        _note(f"error: {exc} (request {exc.request_id or 'unknown'})")
        return exit_status(classify(exc.message, exc.status_code, exc.code))
    finally:
        if wrote:
            sys.stdout.write("\n")
        await api.aclose()
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_url = resolve_base_url(args.base_url, DEFAULT_BASE_URL)
    except ValueError as exc:
        _note(str(exc))
        return EXIT_USAGE
    try:
        if args.command == "logout":
            removed = delete_token(base_url)
            _note("Signed out." if removed else "Nothing was stored for this deployment.")
            return EXIT_OK
        if args.command == "login":
            _login(base_url, _key_arg(args.login_key) or _key_arg(args.key))
            return EXIT_OK
        key = _key_arg(args.key)
        if key:
            save_token(base_url, key)
        credential = resolve_credential(base_url, key)
        if credential is None:
            credential = Credential(_login(base_url, None), "keyring")
    except LoginError as exc:
        _note(str(exc))
        return EXIT_ERROR
    prompt = _oneshot_prompt(args)
    if prompt is not None:
        if not prompt:
            _note("Nothing to send: give -p a prompt or pipe one in.")
            return EXIT_USAGE
        return asyncio.run(run_oneshot(base_url, credential, prompt, args.model))
    run_app(base_url, credential, shell_mode=args.shell)
    return EXIT_OK


def run_app(base_url: str, credential: Credential, *, shell_mode: bool = False) -> None:
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
    app = MatterWaveApp(api, relogin=device, shell_mode=shell_mode)
    holder["app"] = app
    app.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
