"""`matterwave-tui`: sign in, then open the app.

    matterwave-tui                     open (signs in by device code if needed)
    matterwave-tui -p "prompt" [-m M]  one-shot: the answer, then exit
    matterwave-tui --shell -p "..."    one-shot shell assistant: a command, run on y
    cat notes | matterwave-tui         a piped-in prompt is the prompt
    matterwave-tui login [--key [K]]   store a key, or sign in by device code
    matterwave-tui logout [--local]    revoke the stored device key, then forget it
    matterwave-tui config              the config file, and where each setting comes from
    matterwave-tui --profile work      the settings and the sign-in of one profile

stdout carries the answer and nothing else; sign-in prompts and every
note go to stderr, so `matterwave-tui -p "..." > answer.txt` holds only
the answer. The exit status tells a script how the turn ended (EXIT_HELP).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from collections.abc import Callable, Mapping

from matterwave_sdk import frames as fr
from matterwave_sdk.errors import APIConnectionError, APIError, AuthenticationError

from .api import DEFAULT_BASE_URL, Cut, HttpChatAPI, key_prefix
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
from .config import Settings, config_path, keyring_entry, load_config, resolve
from .failures import Kind, classify
from .shell import Shell, detect_shell, run_shell_command, shell_command, shell_request

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


async def revoke_stored(base_url: str, token: str) -> str:
    """Withdraw the stored credential on the server before it is forgotten
    here. A device key -- the one a sign-in minted -- is revoked; a key made
    in the web app and stored with `login --key` is the person's to keep,
    so it is only named. What happened, in a sentence; an error means
    nothing was withdrawn, and the caller keeps the local copy."""
    api = HttpChatAPI(base_url=base_url, token=token)
    try:
        try:
            keys = await api.api_keys()
        except AuthenticationError:
            return "The stored key no longer worked on the server."
        prefix = key_prefix(token)
        row = next(
            (
                key
                for key in keys
                if prefix and prefix in (key.get("prefix"), key.get("previous_prefix"))
            ),
            None,
        )
        if row is None:
            return "The server lists no key for this credential."
        if row.get("revoked_at"):
            return "The device key was already revoked."
        if row.get("kind") != "device":
            return (
                f"The key “{row.get('name') or prefix}” was made in the web app; it is "
                "forgotten here but stays live. Revoke it there if it should stop working."
            )
        await api.revoke_key(str(row.get("id")))
        return "The device key was revoked on the server."
    finally:
        await api.aclose()


def _key_arg(value: object) -> str | None:
    if value is _PROMPT:
        return getpass.getpass("MatterWave API key: ").strip() or None
    return value if isinstance(value, str) else None


def _login(base_url: str, key: str | None, entry: str | None = None) -> str:
    where = entry or base_url
    if key:
        save_token(base_url, key, entry)
        _note(f"Key stored in the OS keyring for {where}.")
        return key
    token = asyncio.run(device_login(base_url, _print_code)).access_token
    save_token(base_url, token, entry)
    _note(f"Signed in. The credential is in the OS keyring for {where}.")
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
        "--profile",
        help="a [profiles.NAME] of the config file: its settings, and its own sign-in "
        "(or MATTERWAVE_PROFILE)",
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
    parser.add_argument(
        "-m",
        "--model",
        help="the model for a one-shot prompt; in the app, for every turn until you pick another",
    )
    parser.add_argument(
        "--shell",
        action="store_true",
        help="shell assistant: an answer is a proposed command, run only after you confirm",
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("config", help="show the config file and where each setting comes from")
    login = sub.add_parser("login", help="store a key, or sign in by device code")
    login.add_argument("--key", nargs="?", const=_PROMPT, dest="login_key")
    logout = sub.add_parser(
        "logout",
        help="revoke the stored device key on the server, then forget it here",
    )
    logout.add_argument(
        "--local",
        action="store_true",
        help="only forget it here, leaving the key live on the server",
    )
    return parser


def _logout(base_url: str, *, local: bool, entry: str | None = None) -> int:
    stored = load_token(base_url, entry)
    if stored is None:
        _note("Nothing was stored for this deployment.")
        return EXIT_OK
    if not local:
        try:
            _note(asyncio.run(revoke_stored(base_url, stored)))
        except (APIConnectionError, APIError) as exc:
            _note(
                f"Could not revoke the key on the server ({exc}); it is still stored here. "
                "Run logout again, or `logout --local` to forget it and leave it live."
            )
            return EXIT_ERROR
    delete_token(base_url, entry)
    _note("Signed out.")
    return EXIT_OK


def _show_config(settings: Settings, base_url: str, entry: str) -> int:
    """`config`: the file, the profile, and each setting with its source,
    to stdout -- this command's output is the answer."""
    path = settings.path
    state = "" if path is None or path.exists() else " (not there; every setting is a default)"
    print(f"config file: {path}{state}")
    print(f"profile: {settings.profile or 'none'}")
    for name, value in settings.values.items():
        shown = base_url if name == "base_url" else value
        print(f"{name}: {shown or '-'} ({settings.sources[name]})")
    print(f"keyring entry: {entry}")
    return EXIT_OK


def load_settings(args: argparse.Namespace) -> Settings:
    """The command line over the environment over the profile over the
    config file."""
    path = config_path()
    return resolve(
        load_config(path),
        flags={"base_url": args.base_url, "model": args.model},
        profile=args.profile,
        path=path,
    )


def _ignored_flags(args: argparse.Namespace) -> list[str]:
    """The flags this command line gives that its command does not use."""
    if args.command not in ("login", "logout", "config"):
        return []
    ignored = [
        flag
        for flag, given in (
            ("-p", args.prompt is not None),
            ("-m", args.model is not None),
            ("--shell", args.shell),
        )
        if given
    ]
    if args.command in ("logout", "config") and args.key is not None:
        ignored.append("--key")
    return ignored


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
    base_url: str,
    credential: Credential,
    prompt: str,
    model: str | None = None,
    *,
    shell: Shell | None = None,
) -> int:
    """One answer, no interface: a conversation is made, the prompt sent
    through the SDK's stream, and the text deltas printed as they arrive.
    The exit status says how it ended (EXIT_HELP); whatever arrived before
    a failure is already on stdout. With `shell`, the model is told the OS
    and shell, and the command it answers is run after a yes on the
    terminal."""
    api = HttpChatAPI(base_url=base_url, token=credential.token)
    wrote = False
    answer: list[str] = []
    if shell is not None:
        prompt = shell_request(prompt, shell)
    try:
        conversation = await api.create_conversation(model=model)
        async for item in api.send(conversation.id, prompt, model):
            if isinstance(item, fr.DeltaFrame) and not item.reasoning:
                sys.stdout.write(item.content or "")
                sys.stdout.flush()
                answer.append(item.content or "")
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
    if shell is not None:
        await _confirm_and_run(shell_command("".join(answer)), shell)
    return EXIT_OK


async def _confirm_and_run(command: str, shell: Shell) -> None:
    """The one-shot's gate: the command runs only on a typed yes at a
    terminal. A prompt piped in leaves no terminal to ask, so nothing runs.
    The command's own output follows the answer on stdout; its exit status
    is a note, the one-shot's status being the turn's."""
    if not command:
        _note("The answer proposed no command.")
        return
    if sys.stdin is None or not sys.stdin.isatty():
        _note("Not run: there is no terminal to confirm it on.")
        return
    sys.stderr.write(f"Run this in {shell.name}? [y/N] ")
    sys.stderr.flush()
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        _note("Not run.")
        return
    output, status = await run_shell_command(command, shell)
    if output:
        print(output)
    _note(f"(exit {status})" if status is not None else "(stopped after thirty seconds)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for flag in _ignored_flags(args):
        _note(f"{flag} does nothing with {args.command}; it was ignored.")
    try:
        settings = load_settings(args)
        base_url = resolve_base_url(settings.get("base_url"), DEFAULT_BASE_URL)
    except ValueError as exc:
        _note(str(exc))
        return EXIT_USAGE
    # The image and shell settings are read from an environment where they
    # are used; a profile's or the file's fill in what the real one leaves
    # unsaid, in a copy handed down rather than in the process's own.
    environ = dict(os.environ)
    for name in ("images", "shell"):
        variable = f"MATTERWAVE_{name.upper()}"
        value = settings.get(name)
        if value and not environ.get(variable):
            environ[variable] = value
    entry = keyring_entry(base_url, settings.profile)
    model = settings.get("model")
    try:
        if args.command == "config":
            return _show_config(settings, base_url, entry)
        if args.command == "logout":
            return _logout(base_url, local=args.local, entry=entry)
        if args.command == "login":
            _login(base_url, _key_arg(args.login_key) or _key_arg(args.key), entry)
            return EXIT_OK
        key = _key_arg(args.key)
        if key:
            save_token(base_url, key, entry)
        credential = resolve_credential(base_url, key, entry)
        if credential is None:
            credential = Credential(_login(base_url, None, entry), "keyring")
    except LoginError as exc:
        _note(str(exc))
        return EXIT_ERROR
    prompt = _oneshot_prompt(args)
    if prompt is not None:
        if not prompt:
            _note("Nothing to send: give -p a prompt or pipe one in.")
            return EXIT_USAGE
        shell = detect_shell(environ) if args.shell else None
        return asyncio.run(run_oneshot(base_url, credential, prompt, model, shell=shell))
    run_app(
        base_url,
        credential,
        shell_mode=args.shell,
        model=model,
        entry=entry,
        theme=settings.get("theme"),
        environ=environ,
        keymap=settings.keys,
    )
    return EXIT_OK


def run_app(
    base_url: str,
    credential: Credential,
    *,
    shell_mode: bool = False,
    model: str | None = None,
    entry: str | None = None,
    theme: str | None = None,
    environ: Mapping[str, str] | None = None,
    keymap: Mapping[str, str] | None = None,
) -> None:
    from .app import MatterWaveApp

    holder: dict[str, MatterWaveApp] = {}
    current = {"token": credential.token}

    async def refresh() -> str | None:
        """One fresh bearer on a 401: a credential another terminal stored
        since this one started, else a device sign-in in the app. A key the
        person passed explicitly is theirs to replace, so it is not."""
        try:
            stored = load_token(base_url, entry)
        except LoginError:
            stored = None
        if stored and stored != current["token"]:
            current["token"] = stored
            return stored
        if credential.source in ("flag", "env"):
            return None
        token = await holder["app"].relogin()
        if token:
            save_token(base_url, token, entry)
            current["token"] = token
        return token

    async def device(on_code: Callable[[DeviceCode], None]) -> str:
        return (await device_login(base_url, on_code)).access_token

    api = HttpChatAPI(base_url=base_url, token=credential.token, refresh=refresh)
    app = MatterWaveApp(
        api,
        relogin=device,
        shell_mode=shell_mode,
        model=model,
        theme=theme,
        environ=environ,
        keymap=keymap,
    )
    holder["app"] = app
    app.run()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
