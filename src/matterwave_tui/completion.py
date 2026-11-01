"""Shell completion, generated from the command line's own parser, so a
flag added there completes without a second list to keep.

    matterwave-tui completion bash        >> ~/.bashrc  (or eval "$(...)")
    matterwave-tui completion zsh         >> ~/.zshrc
    matterwave-tui completion fish        > ~/.config/fish/completions/matterwave-tui.fish
    matterwave-tui completion powershell  >> $PROFILE
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

PROG = "matterwave-tui"
SHELLS = ("bash", "zsh", "fish", "powershell")


@dataclass(frozen=True)
class Command:
    """One level of the command line: its options, and the fixed values
    an option or the positional takes, where it has them."""

    options: list[str]
    choices: dict[str, list[str]] = field(default_factory=dict)
    positional: list[str] = field(default_factory=list)


def describe(parser: argparse.ArgumentParser) -> tuple[Command, dict[str, Command]]:
    """The top level and each subcommand, read off the parser."""

    def level(p: argparse.ArgumentParser) -> Command:
        options: list[str] = []
        choices: dict[str, list[str]] = {}
        positional: list[str] = []
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                continue
            if action.option_strings:
                options.extend(action.option_strings)
                if action.choices:
                    for flag in action.option_strings:
                        choices[flag] = [str(c) for c in action.choices]
            elif action.choices:
                positional.extend(str(c) for c in action.choices)
        return Command(options, choices, positional)

    subcommands: dict[str, Command] = {}
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                subcommands[name] = level(sub)
    return level(parser), subcommands


def script(shell: str, parser: argparse.ArgumentParser) -> str:
    top, subcommands = describe(parser)
    if shell == "bash":
        return _bash(top, subcommands)
    if shell == "zsh":
        # zsh runs the bash function through its own bash completion layer.
        return "autoload -U +X bashcompinit && bashcompinit\n" + _bash(top, subcommands)
    if shell == "fish":
        return _fish(top, subcommands)
    if shell == "powershell":
        return _powershell(top, subcommands)
    raise ValueError(f"no completion for {shell}; one of {', '.join(SHELLS)}")


def _bash(top: Command, subcommands: dict[str, Command]) -> str:
    cases = []
    for name, command in subcommands.items():
        cases.append(
            f'        {name}) words_for_command="{" ".join(command.options + command.positional)}" ;;'
        )
    choice_cases = []
    for command in [top, *subcommands.values()]:
        for flag, values in command.choices.items():
            choice_cases.append(
                f'        {flag}) COMPREPLY=($(compgen -W "{" ".join(values)}" -- "$cur")); return ;;'
            )
    return f"""_matterwave_tui() {{
    local cur prev command word words_for_command
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    case "$prev" in
{chr(10).join(sorted(set(choice_cases)))}
    esac
    command=""
    for word in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
        case "$word" in
            {"|".join(subcommands)}) command="$word" ;;
        esac
    done
    words_for_command="{" ".join(top.options + list(subcommands))}"
    case "$command" in
{chr(10).join(cases)}
    esac
    COMPREPLY=($(compgen -W "$words_for_command" -- "$cur"))
}}
complete -o default -F _matterwave_tui {PROG}
"""


def _fish(top: Command, subcommands: dict[str, Command]) -> str:
    lines = [f"complete -c {PROG} -f"]
    names = " ".join(subcommands)
    lines.append(f'complete -c {PROG} -n "not __fish_seen_subcommand_from {names}" -a "{names}"')

    def flags(command: Command, condition: str) -> None:
        for flag in command.options:
            spec = f"-l {flag[2:]}" if flag.startswith("--") else f"-s {flag[1:]}"
            values = command.choices.get(flag)
            extra = f' -r -a "{" ".join(values)}"' if values else ""
            lines.append(f'complete -c {PROG} -n "{condition}" {spec}{extra}')

    flags(top, f"not __fish_seen_subcommand_from {names}")
    for name, command in subcommands.items():
        flags(command, f"__fish_seen_subcommand_from {name}")
        if command.positional:
            lines.append(
                f'complete -c {PROG} -n "__fish_seen_subcommand_from {name}" '
                f'-a "{" ".join(command.positional)}"'
            )
    return "\n".join(lines) + "\n"


def _powershell(top: Command, subcommands: dict[str, Command]) -> str:
    def quoted(words: list[str]) -> str:
        return ", ".join(f"'{w}'" for w in words)

    per_command = "\n".join(
        f"        '{name}' = @({quoted(command.options + command.positional)})"
        for name, command in subcommands.items()
    )
    choices = {
        flag: values
        for command in [top, *subcommands.values()]
        for flag, values in command.choices.items()
    }
    per_flag = "\n".join(
        f"        '{flag}' = @({quoted(values)})" for flag, values in choices.items()
    )
    return f"""Register-ArgumentCompleter -Native -CommandName '{PROG}' -ScriptBlock {{
    param($wordToComplete, $commandAst, $cursorPosition)
    $top = @({quoted(top.options + list(subcommands))})
    $perCommand = @{{
{per_command}
    }}
    $perFlag = @{{
{per_flag}
    }}
    $words = @($commandAst.CommandElements | Select-Object -Skip 1 | ForEach-Object {{ $_.ToString() }})
    if ($wordToComplete -ne '' -and $words.Count -gt 0) {{ $words = $words[0..($words.Count - 2)] }}
    $candidates = $top
    if ($words.Count -gt 0 -and $perFlag.ContainsKey($words[-1])) {{
        $candidates = $perFlag[$words[-1]]
    }} else {{
        foreach ($word in $words) {{
            if ($perCommand.ContainsKey($word)) {{ $candidates = $perCommand[$word] }}
        }}
    }}
    $candidates | Where-Object {{ $_ -like "$wordToComplete*" }} | ForEach-Object {{
        [System.Management.Automation.CompletionResult]::new($_, $_, 'ParameterValue', $_)
    }}
}}
"""
