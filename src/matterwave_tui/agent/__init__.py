"""Coding-agent mode: a local tool loop against `/v1/chat/completions`.

The tool contract, the approval modes, path confinement, the edit rule, the
diff and the agent-mode system prompt are the SDK's (`matterwave_sdk.agent`);
this package runs them on the user's machine. The server relays the tools
and the calls and never executes one.

    settings.py     user and project settings, the trusted-folder store
    ignore.py       .gitignore / .matterwaveignore / deny-list walking for search
    shell.py        run_shell: process-tree kill, timeout, output cap, sandbox
    tools.py        the seven contract tools, split into prepare and apply
    checkpoints.py  file snapshots in a shadow git repo outside the project
    context.py      MATTERWAVE.md / AGENTS.md, @path mentions, custom commands
    mcp.py          MCP servers as tool sources (stdio, HTTP, MatterWave's /mcp)
    extras.py       todo, plan exit, web fetch and subagent definitions
    loop.py         the run: caps, approvals, compaction, the outcome
    wire.py         what the run asks of the API, and resuming a stored run
    headless.py     `matterwave-tui -p` in agent mode
    screen.py       the Textual screen for `matterwave-tui code`
"""
