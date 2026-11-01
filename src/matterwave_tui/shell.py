"""The shell assistant: which operating system and shell a proposed command
is for, the request that tells the model so, the command an answer
proposes, and the runner that executes it once the person has said yes.

The platform's turn takes no system prompt, so the shell expectation rides
the message itself, behind a marker the history shows: the model is told
the OS and the shell on every turn, and anyone reading the conversation
later sees what it was asked.
"""

from __future__ import annotations

import asyncio
import os
import platform
import re
from collections.abc import Mapping
from dataclasses import dataclass

# How long a proposed command may run before it is stopped.
SHELL_TIMEOUT_S = 30.0
# The start of every shell-mode message; a text that already carries it (an
# edit of one) is not wrapped twice.
SHELL_MARK = "[shell assistant]"
# Names the shell outright (`pwsh`, `cmd`, `/bin/zsh`), for when detection
# guesses wrong: a cmd.exe started from PowerShell inherits its environment.
SHELL_ENV = "MATTERWAVE_SHELL"


@dataclass(frozen=True)
class Shell:
    """Where a command will run: the OS and shell as the model is told them,
    and the program a command is handed to (`argv` + the command)."""

    os_name: str
    name: str
    argv: tuple[str, ...]


def detect_shell(env: Mapping[str, str], system: str | None = None) -> Shell:
    """The shell this terminal runs. `MATTERWAVE_SHELL` decides when set. On
    Windows it is PowerShell when that started the TUI -- it puts three or
    more entries on `PSModulePath` (the user's, the machine's, its own),
    where cmd.exe inherits only the machine's -- and cmd.exe otherwise;
    elsewhere it is `$SHELL`."""
    system = system or platform.system()
    forced = env.get(SHELL_ENV, "").strip()
    if forced:
        return _named_shell(forced, system)
    if system == "Windows":
        if len(env.get("PSModulePath", "").split(os.pathsep)) >= 3:
            # PowerShell 7 marks itself; Windows PowerShell does not.
            exe = "pwsh" if env.get("POWERSHELL_DISTRIBUTION_CHANNEL") else "powershell"
            return Shell("Windows", "PowerShell", (exe, "-NoProfile", "-Command"))
        return Shell("Windows", "cmd.exe", (env.get("COMSPEC") or "cmd.exe", "/c"))
    path = env.get("SHELL") or "/bin/sh"
    os_name = "macOS" if system == "Darwin" else system or "Unix"
    return Shell(os_name, os.path.basename(path) or "sh", (path, "-c"))


def _named_shell(program: str, system: str) -> Shell:
    """The shell `MATTERWAVE_SHELL` names, by its program's name."""
    os_name = "macOS" if system == "Darwin" else system or "Unix"
    base = os.path.basename(program).lower().removesuffix(".exe")
    if base == "cmd":
        return Shell(os_name, "cmd.exe", (program, "/c"))
    if base in ("powershell", "pwsh"):
        return Shell(os_name, "PowerShell", (program, "-NoProfile", "-Command"))
    return Shell(os_name, base or "sh", (program, "-c"))


def shell_request(text: str, shell: Shell) -> str:
    """The message a shell-mode turn sends: the expectation, then what the
    person asked."""
    if text.startswith(SHELL_MARK):
        return text
    return (
        f"{SHELL_MARK} Reply with one {shell.name} command for {shell.os_name} that does what "
        "follows, as a single fenced code block and nothing else. It is shown to me and runs "
        f"only if I confirm.\n\n{text}"
    )


def shell_command(text: str) -> str:
    """The command an answer proposes: one fenced block's contents when the
    answer is a single fenced block, the bare text otherwise."""
    stripped = text.strip()
    fence = re.fullmatch(r"```[^\n]*\n(.*)\n```\s*", stripped, re.DOTALL)
    return (fence.group(1) if fence else stripped).strip()


async def run_shell_command(command: str, shell: Shell) -> tuple[str, int | None]:
    """A proposed command, run for real in the shell it was asked for: its
    output (stderr folded in) and exit status, or the note that it outlived
    its half minute and was stopped (status None). cmd.exe takes the line
    whole, its own quoting being nothing an argument list can express."""
    if shell.name == "cmd.exe":
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *shell.argv, command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), SHELL_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return "(stopped: the command ran past its thirty seconds)", None
    return out.decode(errors="replace").strip(), proc.returncode
