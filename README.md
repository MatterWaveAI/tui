# matterwave-tui

MatterWave chat in a terminal: the same conversations, branches and models as the web app, over
the same `/api` routes and the same event stream.

## Install

```sh
uvx matterwave-tui                 # run without installing
pipx install matterwave-tui        # or install it once
```

Both install from the [MatterWaveAI](https://pypi.org/org/MatterWaveAI/) org on PyPI.

From a checkout of this repo: `uv sync && uv run matterwave-tui`.

## One-shot and scripting

Without the interface: `matterwave-tui -p "two plus two?"` prints the answer to stdout and exits
(`-m` picks the model). A piped-in stdin is the prompt, so scripts work the usual way:

```sh
cat notes.md | matterwave-tui -m grt-nano
echo "explain this trace" | matterwave-tui -p > answer.txt
```

Exit 0 on a whole answer, 1 when the turn was refused or cut short (whatever arrived is already on
stdout). `--shell` starts the shell assistant: each answer is treated as a proposed command, shown
in a confirm that runs it only when you press Enter — nothing is ever executed without that.

## Coding agent: `matterwave-tui code`

```sh
matterwave-tui code              # the agent in the current folder
matterwave-tui code ~/src/app    # or in another one; that folder is the workspace root
```

The agent reads and searches the folder, edits files and runs commands, with your approval. It
runs its own loop against `/v1/chat/completions`: the server passes the tools to the model,
returns the calls it makes, and never runs one. Every tool runs here, as you. The run is stored
as a conversation (`metadata.conversation_id`), so it opens in the web app with its tool calls and
results; `--no-store` keeps it out.

**Tools.** `read_file`, `list_dir`, `glob`, `grep`, `edit`, `write_file` and `run_shell`, as
the SDK's agent contract defines them (`matterwave_sdk.agent`, contract 1.1.0, shared with the
VS Code extension and the desktop app). The read tools are the SDK's own, so they read, search
and word their results exactly as the other clients do; the TUI's tests run the contract's
conformance cases through them. Every path is resolved and must stay under the root after links
are followed: `..`, an absolute path outside the root and a link out of it are refused before
anything happens. `.env*`, `*.pem`, `id_*`, `.git/` and anything listed in `.matterwaveignore`
are refused for reading as well as writing, in any case (`.ENV`, `ID_RSA`). On every platform, a
name Windows would read as another one is refused too: a `:` stream (`key.pem::$DATA`, `C:x`), a
name ending in a dot or a space (`.git `), a device name (`CON`, `NUL.txt`, `COM1`) and a `\\?\`,
`\\.\` or network path. Search (`glob`, `grep`, `list_dir`) also skips what the `.gitignore` and
`.matterwaveignore` files from the root down ignore, ignoring case; `list_dir` still shows the
protected files below. A `glob` pattern without a `/` matches file names at any depth (`*.ts`);
one with a `/` matches the path under the search's `path`. `read_file` reads files up to 16 MB,
a window of 2,000 lines or 256 KB at a time, and a line longer than that is shown cut; `grep`
skips files over 4 MB and cuts a matching line at 400 characters; `edit` / `write_file` work on
files up to 8 MB. Confinement is not a sandbox: `run_shell` can reach anything you can, which is
why commands ask.

**Approval modes.** The status line always shows the mode. `Shift+Tab` cycles it, and
`/approval-mode <mode>` sets it (`--save` keeps it as your default).

| Mode | Reads | File changes | Commands, web fetch | MCP tools |
|---|---|---|---|---|
| `plan` | yes | not offered | not offered | read-only ones approved, others not offered |
| `ask` (default) | yes | each shown as a diff and approved | each approved | each approved |
| `auto-edit` | yes | applied | each approved | each approved |
| `yolo` | yes | applied | run | run |

These are the contract's decisions (`agent.allowed`): the TUI's own tools are gated by kind, so
`web_fetch` is a network tool, an MCP server's tool an MCP one, and `todo_write`, `agent` and
`exit_plan_mode` steer the run and are allowed in every mode. Plan mode never runs silently what
`ask` mode would ask about. `yolo` exists only when the session starts with `--yolo`, and it is
never saved as a default (a `yolo` default in the settings is ignored). When no sandbox is on,
`--yolo` warns.

A change to a protected file is asked about in every mode, `yolo` included, since it outlasts
the session: `.matterwaveignore`, `.matterwave/`, `MATTERWAVE.md`, `MATTERWAVE.local.md` and
`AGENTS.md`, the shell startup files (`.bashrc`, `.bash_profile`, `.profile`, `.zshrc`,
`.zprofile`), `.ssh/` and `.aws/`, at any depth and in any case, and `.config/matterwave` (the
contract's list); and your own MatterWave config and state folders wherever they are set. So is
every change when the workspace is your home folder or a folder above it, where the startup
folder or another profile would run things later without a command being approved. A headless
run refuses such a change, whatever `--approval-mode` says.

Each change in `ask` mode opens its unified diff: `y` applies it and `n` rejects it, leaving the
file byte for byte as it was; the model is told you said no, as an ordinary result, and to change
course. Overwriting a binary file shows every line of the text replacing it.
If the file changes, or its path comes to lead somewhere else (a folder swapped for a link), while
the diff is up, nothing is written and the model is told to read it again. A command shows in
full, never shortened; in `auto-edit` its approval also names the files changed without asking
since your last approval, which the command may run (a `conftest.py`, a build script). A dialog
takes no answer in its first half second, so keys typed for something else cannot answer it.
Control characters in a diff, a command, a file, a command's output or the model's answer are
spelled out (`\x1b`), as are bidi overrides and, in a dialog, zero-width characters: nothing the
agent shows can move the cursor, redraw a dialog or write to the clipboard. Only `http` and
`https` links in its answers open on a click; a `file:` link to a script would run it. `Esc` stops a run:
the stream closes and a running command is killed with everything it started, what it detached
included (`cmd /c start /b`, `Start-Process`, `setsid`, `nohup ... &`), and the model gets the
output so far under "Stopped by the user". On Windows the command starts suspended and runs only
once it is in a job object that kills its members when closed, so nothing it starts can get out
first; if the TUI itself dies, its processes go too. On Linux it runs under a small keeper that
is a child subreaper: a process it orphans comes back to the keeper instead of to init, and the
keeper ends every descendant, whatever its session or process group, on Stop, at the command's
end and when the TUI dies. macOS has no subreaper: the command's process group and every process
still below it are ended, but a daemon whose parent has already exited belongs to launchd by then
and is not found. When a command exits, what it left running ends with it at once, so a
background process holding its output does not keep the call open until the timeout. A program
it hands to another service (a scheduled task, WMI, a system service, `launchctl`) is not its
child and outlives it on every platform; only the sandbox contains that.

**Limits.** Each run (one prompt) has a cap on model calls (`--max-turns`, default 40) and on
tokens (`--max-tokens`, default 400,000). A subagent's calls count against its parent's. A 402
or 429 from `/v1` ends the run with the same copy the chat shows.

**Unavailable.** The agent stays off, with the reason, when your organisation has switched local
agent tools off (`/api/me` `local_agent_tools`) or the model cannot call tools (`supports_tools`
on `/api/models`; `/model` picks another). A server that sends neither field allows it.

**Context.** `MATTERWAVE.md` and `AGENTS.md` at the root, and your own `MATTERWAVE.local.md`
(keep it in `.gitignore`), are read as instructions, with `MATTERWAVE.md` in the config folder
for every project and your account's custom instructions on top. A repository can carry
instructions, so the root's files and `.matterwave/` settings load only in a folder you trust:
the first time a folder has any, you are asked, and the answer is remembered (`/trust` changes
it). `/init` has the agent draft a `MATTERWAVE.md`. `@path` in a prompt attaches that file or
folder, under the same confinement. The project's context files, commands and subagents are read
under it as well, so a `MATTERWAVE.md` that is a link to a file outside the root is not loaded.

**Checkpoints.** Before each applied change, the touched files' bytes go into a shadow git
repository in the state folder, outside the project, one per workspace and readable by you alone.
`/restore` lists the checkpoints and puts the files back as they were before the one you pick,
each path confined again as a write; the project's own `.git` is never touched.

**Commands** (in the composer):

| Command | Does |
|---|---|
| `/help` | the commands and keys |
| `/model [id]` | pick the agent's model |
| `/approval-mode [mode] [--save]` | show or set the mode |
| `/compact` | fold old tool output, then summarise the run (stored runs) |
| `/context` | tokens in use, context files, tools offered, the task list |
| `/restore [id]` | list checkpoints, or restore one |
| `/init` | draft a `MATTERWAVE.md` |
| `/resume [id]` | list this folder's stored runs, or continue one |
| `/trust`, `/mcp`, `/agents`, `/clear`, `/chat` | trust the folder, list MCP tools, list subagents, start over, back to the chat |

A Markdown file in `commands/` of the config folder, or `.matterwave/commands/` of a trusted
project, is a custom command: `git/review.md` is `/git:review`, and `$ARGUMENTS` in it is replaced
by what follows the name.

When the history fills the model's context window, old tool output is folded first, without a
model call. In the middle of a run the oldest rounds are then left out of the prompt (the stored
conversation keeps them); before the next prompt, a stored run is summarised by the
conversation's compaction, and the summary rides in the system prompt so the stored turns stay
yours. A summary written mid-round would put the round's results on a branch of the conversation.

### Headless: `-p` in agent mode

`-p` alone is the chat one-shot above. With `code`, or with any agent option, it is one agent run:

```sh
matterwave-tui -p "add a test for parse_args, then run the tests" --approval-mode auto-edit
matterwave-tui code ./repo -p "why does CI fail?" --output-format json
matterwave-tui -p "and the other module?" --continue        # the last run in this folder
matterwave-tui -p "carry on" --resume <conversation-id>     # a stored run, tool rounds included
```

`--output-format text` (the answer on stdout, tool lines on stderr), `json` (one document: the
result, usage, `is_error`, the status and its message) or `stream-json` (one JSON event per line,
then the same result). A headless run never prompts: a call that the mode would ask about is
refused and the run ends. The exit code says how it ended:

| Code | Meaning |
|---|---|
| 0 | done |
| 1 | an error, the agent unavailable, or stopped |
| 2 | a usage error (a bad option, `yolo` without `--yolo`, a run that is not there) |
| 3 | budget, rate limit or plan (402 / 429) |
| 4 | a call needed approval the mode does not give |
| 5 | the turn or token cap |

In CI, run it in a container, keep `--max-turns` and `--max-tokens` low, and use `yolo` (with
`--yolo`) only there. `--trust` trusts the folder for one run without remembering it.

### Settings

`settings.json` in the config folder (`MATTERWAVE_CONFIG_DIR`, else `XDG_CONFIG_HOME`, else
`~/.config`, then `matterwave/`), and `.matterwave/settings.json` in a trusted project, on top:

```json
{
  "agent": {"default_mode": "ask", "model": "glm-4.6", "max_turns": 40, "max_tokens": 400000},
  "mcpServers": {
    "docs": {"command": "npx", "args": ["-y", "some-mcp-server"], "tools": ["search"]},
    "tracker": {"url": "https://mcp.example.com/mcp", "headers": {"Authorization": "Bearer ${TRACKER_TOKEN}"},
                "trust": false}
  },
  "matterwaveMcp": {"enabled": true},
  "sandbox": {"engine": "podman", "image": "docker.io/library/python:3.12-slim-bookworm", "network": false}
}
```

- **MCP servers**: stdio (`command`, `args`, `env`) or Streamable HTTP (`url`, `headers`;
  `${VAR}` is read from the environment). `tools` is the allow-list; a tool outside it is never
  offered to the model. Every MCP call asks in `ask` and `auto-edit` mode and runs in `yolo`. A
  tool marks itself read-only (`readOnlyHint`), but only a trusted server's word is taken
  (`"trust": true`, and MatterWave's own): such a tool is offered in `plan`, where it asks too,
  and every other tool is not offered there.
  MatterWave's own `/mcp` is pre-wired with your credential (`matterwaveMcp`). Tools are offered
  as `mcp__<server>__<tool>`, so none can stand in for a built-in; two that come out under one
  name are both left out, with a notice. An answer over 16 MB is refused, and `timeout_s`
  (default 60) bounds each request as a whole, so a server that trickles cannot hold a run.
- **Subagents**: Markdown files in `agents/` of the config folder or `.matterwave/agents/` of a
  trusted project, with `name`, `description`, `tools` (the allow-list) and `max_turns` in the
  front matter and the prompt as the body. The model hands one a task through the `agent` tool;
  it runs under the same mode, approvals and caps.
- **Sandbox**: off by default. `podman` or `docker` run each command in a container that sees
  only the root (mounted at `/workspace`), with a read-only filesystem elsewhere and no network
  unless `"network": true`. `bwrap` (Linux) and `seatbelt` (macOS) keep writes to the root and
  cut the network the same way, but the rest of your files stay readable to the command (a key
  it copies into the root is then one `read_file` from the model), so only a container keeps
  your home folder out of reach. The root's `.git` and `.matterwave` stay read-only inside all
  of them. Stop removes the container. `--sandbox` picks one for a session.
- **Web fetch** (`web_fetch`) and the **task list** (`todo_write`) are on unless
  `"agent": {"web_fetch": false}` / `{"todo": false}`. A fetch is asked for in `ask` and
  `auto-edit` mode, never sends your MatterWave credential and does not follow a redirect (the
  model is told where it leads and asks again). Its approval names the host the request really
  goes to when the URL spells it otherwise (an international look-alike, a `user@` part), and a
  fetch stops after 60 s however slowly the server sends.

**What leaves your machine.** Every file the agent reads, every command's output and every page
it fetches is sent to MatterWave and to the model behind it, and is metered. The deny list is a
floor, not a guarantee: keep secrets out of the workspace, or list them in `.matterwaveignore`.
`MATTERWAVE_API_KEY` is taken out of a command's environment, but a command runs as you and can
read what you can, the system keyring (where the TUI keeps your credential) included. The sandbox
is what keeps a command away from them.

## Sign in

Point it at a deployment with `--base-url` or `MATTERWAVE_BASE_URL` (default
`http://localhost:8000`). Then either:

- **Device code** — run `matterwave-tui login` (or just `matterwave-tui`). It prints a URL and a
  short code; approve it in any browser and the terminal is signed in as you.
- **An API key** — `matterwave-tui login --key` prompts for an `mw_` key, or set
  `MATTERWAVE_API_KEY` for one shell. A key acts as its owner, so conversations show up in the
  web app too.

The credential is stored in the OS keyring (service `matterwave`, one entry per base URL), never in
a dotfile. If no keyring backend is available, sign-in fails rather than writing a file; use
`MATTERWAVE_API_KEY` instead. `matterwave-tui logout` forgets it.

On a 401 the client tries once more with a fresh credential (one another terminal stored, or a
device sign-in shown in the app), then reports the session as expired. A key passed with `--key`
or the environment is never replaced behind your back.

## Keys

| Key | Where | Does |
|---|---|---|
| `Ctrl+Enter` | anywhere | send (also `Ctrl+J` / `Ctrl+S`, for terminals that do not report Ctrl+Enter) |
| `Esc` | composer | to the transcript; cancels an edit; stops an answer that is streaming |
| `↑` `↓` / `k` `j` | transcript | select a message |
| `[` `]` | transcript | previous / next sibling branch of the selected message |
| `e` | transcript | edit the selected (or last) user message in the composer |
| `r` | transcript | regenerate the selected (or last) answer |
| `x` | transcript | show / hide the messages a summary replaced |
| `c` | transcript | compact the conversation now |
| `m` | transcript | model picker |
| `n` | transcript | new conversation |
| `/` | transcript | search conversations |
| `F2` | anywhere | rename the open conversation |
| `p` | anywhere | pin / unpin the open conversation |
| `a` | anywhere | archive / unarchive the open conversation |
| `d` | anywhere | delete the open conversation (asks first) |
| `f` | transcript | fork the conversation from the selected message |
| `s` | anywhere | create a share link for the conversation |
| `w` | anywhere | export the conversation to a Markdown file (the working directory) |
| `i` | anywhere | attach a file (its path); it is uploaded and sent with the next message |
| `o` | anywhere | cycle the sidebar list: default → pinned → archived → all |
| `l` | anywhere | the library: folders and tags for the open conversation |
| `L` | anywhere | prompt roles: apply, add or delete a named system prompt |
| `y` | transcript | copy the selected message (or the last answer) to the clipboard |
| `Y` | transcript | copy just the fenced code blocks of that message |
| `Ctrl+Q` | anywhere | quit |

Typing while an answer streams queues the prompt; it sends itself when the answer finishes, and
`Esc` stops the stream and puts it back in the composer. A leading `/` in the composer is a
command, not a message: `/role name` applies a saved prompt role, `/role --add name` makes one,
`/shell` toggles the shell assistant.

Letter keys apply when focus is not in the composer or the search box; `Esc` gets you there.

The status line also carries the org's balance for the period (`usage used/included (%)`, read
once at start-up from `/api/me/usage`) and any files attached but not sent yet. An attached
image or document renders in the transcript as an `[image: filename]` placeholder line — the
same line the Markdown export writes — and its id goes out with the next turn.

A draft is kept per conversation while the app runs. The model picker groups models as the web
does — GRT, partner, research preview — and shows a model your plan does not include with the plan
that has it, without letting you pick it. A model you pick is kept on the conversation
(`PATCH /api/conversations/{id}`), so the web app opens it on the same model.

The status line shows the model, the context meter (`used/window` from the last turn plus an
estimate of the draft), online / offline, the wait left on a rate limit, and the reference of the
last error. A failure shows the same copy as the web app and always the request reference to
quote; a key's per-minute limit (429) and a model the plan does not include (402) are states of
their own, since neither is the budget.

Stopping an answer with `Esc` closes the stream; the server keeps what it had sent as a truncated
turn, which is read back. A stream that drops is resumed once by `Last-Event-ID` (the SDK's own
resume) before it is reported as cut off.

## Development

```sh
uv sync --extra dev
uv run ruff check . && uv run mypy --strict src && uv run pytest -q
```

`tests/fake_api.py` holds `FakeAPI` (the chat, in memory) and `FakeV1`, a scripted `/v1` under
the real adapter for the agent's tests. `UPDATE_GOLDEN=1` rewrites the headless golden files in
`tests/golden/`.

Agent mode needs the SDK's `matterwave_sdk.agent` (the tool contract, confinement, the edit rule,
the diff and the agent prompt), which is not on PyPI yet. Until it is, `uv sync` resolves the SDK
from the sibling checkout (`../python-sdk`, branch `develop`) through `[tool.uv.sources]`; with
pip, install that checkout first (`pip install -e ../python-sdk`).

The wire is the Python SDK's (`matterwave-sdk` from your package index): `client.conversations.*`,
its typed frames and its stream resume, plus `client.attachments`, `client.shares` and
`client.usage` for the rest. The routes the SDK has no method for yet (`/api/models`, the
conversation delete, pin and archive) go through the same client. This package keeps what is
terminal-specific: the tree in memory, the failure copy, and the one re-login a 401 gets before
it stands.
