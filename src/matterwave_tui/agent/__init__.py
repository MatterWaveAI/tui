"""Coding-agent mode: a local tool loop against `/v1/chat/completions`.

The tool contract (1.2.0) is the SDK's (`matterwave_sdk.agent`): the
approval modes and the decision for each tool kind, path confinement and
the protected paths, the search rules, the read tools and their output,
run_shell's result text, the refusal sentences, the edit rule, the diff
and the agent-mode system prompt with its slots for extra tools and
context files. This package runs them on the user's machine, and adds what
is the TUI's own: the approval dialogs, writes re-checked before they are
made, the process trees, MCP, subagents and the sandbox. The server relays
the tools and the calls and never executes one.

    settings.py     user and project settings, the trusted-folder store
    shell.py        run_shell: job object / keeper / group kill, timeout, output cap, sandbox
    reaper.py       the Linux keeper: runs a command as a child subreaper
    tools.py        the seven contract tools: the SDK's reads, writes split into prepare and apply
    grep.py         grep in a process of its own, killed at its deadline
    checkpoints.py  file snapshots in a shadow git repo outside the project
    context.py      MATTERWAVE.md / AGENTS.md, @path mentions, custom commands
    mcp.py          MCP servers as tool sources (stdio, HTTP, MatterWave's /mcp)
    extras.py       todo, plan exit, web fetch and subagent definitions
    loop.py         the run: caps, approvals, compaction, the outcome
    wire.py         what the run asks of the API, and resuming a stored run
    headless.py     `matterwave-tui -p` in agent mode
    screen.py       the Textual screen for `matterwave-tui code`
"""
