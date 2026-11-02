# matterwave-tui

MatterWave chat in a terminal: the same conversations, branches and models as the web app, over
the same `/api` routes and the same event stream.

## Install

`matterwave-tui` and the `matterwave-sdk` it is built on are not on PyPI. Install it from its
repository with [uv](https://docs.astral.sh/uv/), which also fetches the SDK from the commit the
lockfile pins:

```sh
uv tool install "git+https://github.com/MatterWaveAI/tui"            # the matterwave-tui command
uv tool install "matterwave-tui[images] @ git+https://github.com/MatterWaveAI/tui"   # with an extra
```

From a checkout of this repo: `uv sync && uv run matterwave-tui`.

Until agent contract 1.2 is on the SDK's repository, the SDK comes from a `python-sdk` checkout
beside this one instead of the pinned commit (see [Development](#development)), so install from a
checkout with that SDK beside it.

Two extras add what a terminal may not need:

| Extra | Adds |
|---|---|
| `images` | Pillow: sixel images, and JPEG/GIF/WebP on the kitty protocol (PNG needs nothing) |
| `dictation` | sounddevice: dictation from the microphone (without it, dictation transcribes a file) |

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
the SDK's agent contract defines them (`matterwave_sdk.agent`, contract 1.2.0, shared with the
VS Code extension and the desktop app). The read tools are the SDK's own, so they read, search
and word their results exactly as the other clients do; the TUI's tests run the contract's
conformance cases through them. Every path is resolved and must stay under the root after links
are followed: `..`, an absolute path outside the root, a link out of it and a loop of links are
refused before anything happens. `.env*`, `*.pem`, `id_*`, `.git/` and anything listed in
`.matterwaveignore` are refused for reading as well as writing, in any case (`.ENV`, `ID_RSA`). On
every platform, a name Windows would read as another one is refused too: a `:` stream
(`key.pem::$DATA`, `C:x`), a name ending in a dot or a space (`.git `), a device name (`CON`,
`NUL.txt`, `COM1`) and a `\\?\`, `\\.\` or network path. Search (`glob`, `grep`, `list_dir`) also
skips what the `.gitignore` and `.matterwaveignore` files from the root down ignore, ignoring case;
`list_dir` still shows the protected files below. A `glob` pattern without a `/` matches file names
at any depth (`*.ts`); one with a `/` matches the path under the search's `path`. `read_file` reads
files up to 16 MB, a window of 2,000 lines or 256 KB at a time, and a line longer than that is
shown cut; `grep` skips files over 4 MB and cuts a matching line at 400 characters, and runs in a
Python process of its own that is stopped after 30 seconds, so a pattern that backtracks without end
cannot freeze the TUI; a pipe, socket or device is not opened; `edit` / `write_file` work on files
up to 8 MB. Confinement is not a sandbox: `run_shell` can reach anything you can, which is why
commands ask.

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

**Context.** `MATTERWAVE.md` in the config folder (for every project), then `MATTERWAVE.md` and
`AGENTS.md` at the root, then your own `MATTERWAVE.local.md` (keep it in `.gitignore`) are read as
instructions in that order, so the more personal file wins, with your account's custom
instructions on top. A repository can carry instructions, so the root's files and `.matterwave/`
settings load only in a folder you trust: the first time a folder has any, you are asked, and the
answer is remembered (`/trust` changes it). `/init` has the agent draft a `MATTERWAVE.md`. `@path`
in a prompt attaches that file or folder, under the same confinement. The project's context
files, commands and subagents are read under it as well, so a `MATTERWAVE.md` that is a link to a
file outside the root is not loaded.

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

`-p` alone is the chat one-shot above (and `--continue` with it the chat's most recent
conversation). With `code`, or with any agent option, it is one agent run:

```sh
matterwave-tui -p "add a test for parse_args, then run the tests" --approval-mode auto-edit
matterwave-tui code ./repo -p "why does CI fail?" --output-format json
matterwave-tui code -p "and the other module?" --continue   # the last run in this folder
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
`MATTERWAVE_API_KEY` instead. `matterwave-tui logout` revokes the stored device key on the server
and then forgets it; a key you made in the web app and stored with `login --key` is only forgotten,
and stays live until you revoke it there. If the server cannot be reached the key stays stored, so
nothing is left live by accident; `logout --local` forgets it anyway.

On a 401 the client tries once more with a fresh credential (one another terminal stored, or a
device sign-in shown in the app), then reports the session as expired. A key passed with `--key`
or the environment is never replaced behind your back.

## One-shot and scripting

Without the interface: `matterwave-tui -p "two plus two?"` prints the answer to stdout and exits
(`-m` picks the model; in the app, `-m` is the model for every turn until you pick another). A
piped-in stdin is the prompt, so scripts work the usual way:

```sh
cat notes.md | matterwave-tui -m grt-nano
echo "explain this trace" | matterwave-tui > answer.txt
git diff | matterwave-tui -p "review this"        # the piped text follows the prompt
matterwave-tui --continue -p "and in Rust?"       # the most recent conversation
matterwave-tui --conversation c_123 -p "go on"     # that one
matterwave-tui --temporary -p "a quick one"        # out of the history, deleted after 24 h
```

Each one-shot otherwise makes a new saved conversation, as the web app does.

The rest of the command line:

| Command | Does |
|---|---|
| `matterwave-tui --version` | the version, and the SDK's |
| `matterwave-tui code [path]` | the coding agent in a folder (see [Coding agent](#coding-agent-matterwave-tui-code)) |
| `matterwave-tui --list-models` | the models this account can use, the default marked |
| `matterwave-tui export ID [-f md\|json\|html] [-o FILE]` | one conversation's active path to stdout, or to a file that is not there yet |
| `matterwave-tui completion bash\|zsh\|fish\|powershell` | the completion script, to source from the shell's profile |
| `matterwave-tui config` | the config file, and where each setting comes from |
| `matterwave-tui --debug [FILE]` | log each request's method, path, status and request id (never what was said) to FILE, or `debug.log` in the state directory |
| `matterwave-tui --plain` | every mark as its word instead of a symbol (`pinned`, not 📌), and no colour |
| `matterwave-tui --no-update-check` | skip the once-a-day look at PyPI for a newer release (or `MATTERWAVE_NO_UPDATE_CHECK=1`) |

stdout carries the answer and nothing else: a device sign-in's URL and code, and every note, go to
stderr. A flag the command does not use (`-m` with `logout`, say) is named there rather than
ignored silently. The exit status says how the turn ended:

| Status | Meaning |
|---|---|
| 0 | the whole answer is on stdout |
| 1 | the turn failed (network, gateway, timeout, an unexpected answer) |
| 2 | the command line was wrong, or there was nothing to send |
| 3 | the platform refused: moderation, budget, plan, rate limit, sign-in |
| 4 | the answer was cut short; what arrived is on stdout |

`--shell` starts the shell assistant (`/shell` toggles it in the app). Each message tells the
model which OS and shell the command is for — the message starts with `[shell assistant]` and
says so, in the history too — and the answer is a proposed command, shown in a confirm that runs
it only when you press Enter. Nothing is ever executed without that. With `-p` the confirm is a
`[y/N]` on the terminal; a prompt piped in leaves no terminal to ask, so nothing runs. The shell is
PowerShell or cmd.exe on Windows (whichever started the TUI) and `$SHELL` elsewhere;
`MATTERWAVE_SHELL=pwsh` (or `cmd`, `/bin/zsh`, …) names it outright.

## Keys

| Key | Where | Does |
|---|---|---|
| `Ctrl+Enter` | anywhere | send (also `Ctrl+J` / `Ctrl+S`, for terminals that do not report Ctrl+Enter) |
| `Esc` | composer | to the transcript; cancels an edit; stops an answer that is streaming |
| `Ctrl+E` | anywhere | write the message in `$VISUAL` / `$EDITOR` (Notepad on Windows, vi elsewhere); what it saves comes back to the composer |
| `?` / `F1` | transcript | every key, generated from the bindings, with the name `[keys]` changes it by |
| `↑` `↓` / `k` `j` | transcript | select a message |
| `[` `]` | transcript | previous / next sibling branch of the selected message |
| `e` | transcript | edit the selected (or last) user message in the composer |
| `r` | transcript | regenerate the selected (or last) answer |
| `x` | transcript | show / hide the messages a summary replaced |
| `c` | transcript | compact the conversation now |
| `m` | transcript | model picker |
| `M` | transcript | preset picker (when the organisation offers presets) |
| `n` | transcript | new conversation: Enter a regular one, `t` a temporary one (deleted 24 h after its last message), Esc none |
| `/` | transcript | search conversations, titles and messages, best match first; the last row, **More results…**, reads the next page |
| `t` | anywhere | this conversation's own instructions, edited against the effective prompt |
| `b` | transcript | reasons and a comment for the selected (or last) answer's rating |
| `F2` | anywhere | rename the open conversation |
| `p` | anywhere | pin / unpin the open conversation |
| `a` | anywhere | archive / unarchive the open conversation |
| `d` | anywhere | delete the open conversation (asks first) |
| `f` | transcript | fork the conversation from the selected message |
| `s` | anywhere | share the conversation: how long the link lives, whether readers can open the attachments, the link copied; and its links, to revoke |
| `S` | anywhere | every share link you made, to revoke |
| `w` | anywhere | export the conversation to a Markdown file in the working directory; an existing file is never overwritten (`name (2).md`) |
| `i` | anywhere | attach a file by its path (quoted or not, `~` allowed) or every file a glob matches (`~/shots/*.png`); it goes with this conversation's next message |
| `Ctrl+O` | anywhere | pick files to attach from a tree of the working directory |
| `I` | anywhere | take a pending file off the next message (and delete the upload) |
| `v` | anywhere | dictate into the composer: the microphone with the `dictation` extra, an audio file without |
| `o` | anywhere | cycle the sidebar list: default → pinned → archived → all; with a folder or tag shown, back to the whole list |
| `l` | anywhere | the library: folders and tags for the open conversation, and the sidebar narrowed to one |
| `L` | anywhere | prompt roles: apply, add or delete a named system prompt |
| `R` | anywhere | report the conversation, or the selected answer, as unsafe or unhelpful |
| `N` | anywhere | the notifications inbox |
| `A` | anywhere | dismiss the announcements banner (a critical one stays) |
| `U` | anywhere | your account: API keys, signed-in browsers, preferences, your own instructions, the plan |
| `y` | transcript | copy the selected message (or the last answer) to the clipboard |
| `Y` | transcript | copy just the fenced code blocks of that message |
| `Ctrl+Q` | anywhere | quit |

Letter keys apply when focus is not in the composer or the search box; `Esc` gets you there. Each
answer also carries two thumbs (`👍 good`, `👎 bad`): a click rates it, a second click on the same
thumb takes it back. Every symbol the screen draws alone carries its word beside it (`📌 pinned`),
so a screen reader says what it means; `--plain` draws the words alone.

Inside the dialogs:

| Dialog | Keys |
|---|---|
| Notifications (`N`) | `r` read the highlighted one, `a` read all, `u` unread only, `n` / `p` next and previous page |
| Library (`l`) | `f` new folder, `t` new tag, `m` file the chat in the highlighted folder (again: take it out), `Space` toggle the highlighted tag, `r` rename the highlighted folder, `x` or `Delete` delete the highlighted folder or tag (asks first), `Enter` show only that folder or tag in the sidebar (again: all) |
| Shares (`S`) | `x` revoke the highlighted link |
| Prompt roles (`L`) | `Enter` apply, `a` add, `d` delete |
| Share (`s`) | `Enter` change the expiry or the attachments, or create the link; `x` or `Delete` revoke the highlighted link (asks first) |
| Pick files (`Ctrl+O`) | `Enter` attach the file (with any marked), `Space` mark or unmark the highlighted file |
| Help (`?`) | `?` or `F1` closes it too |
| Pending files (`I`) | `Enter`, `x` or `Delete` take the highlighted file off |
| Account (`U`) | `Enter` toggle a preference or edit the locale or an instruction, `x` or `Delete` revoke the highlighted key or browser (asks first), `o` rotate the highlighted key and show its new secret once; this terminal's own key is left to `logout` |
| Instructions (`t`), feedback (`b`) | `Ctrl+T` save |
| Every dialog | `Esc` closes it without a change |

The command palette (`Ctrl+P`) adds what has no key of its own, beside Textual's own (the theme
among them):

- **Choose model…** — the same picker as `m`
- **Choose preset…** — the same picker as `M`, when the organisation offers presets
- **Account…** — the same screen as `U`
- **Switch account…**
- **Import a ChatGPT or Claude export…**
- **Export this account's data…** — the whole account as one JSON file, saved beside the terminal
- **Dictate audio…**
- **Prompt roles…**
- **Coding agent in this folder…** — the agent screen on the working directory, as `matterwave-tui code`

Typing while an answer streams queues the prompt; it sends itself when the answer finishes, and
`Esc` stops the stream and puts it back in the composer. A leading `/` in the composer is a
command, not a message: `/role name` applies a saved prompt role, `/role --add name` makes one,
`/shell` toggles the shell assistant, `/temp 0.3` sets this conversation's temperature (0 to 2;
`/temp default` goes back to the server's, `/temp` says which is in force). An unknown command stays in the composer; start a message
with `//` to send it with a single leading slash (`//etc/hosts is empty` sends `/etc/hosts is empty`).

## What the screen shows

The status line shows the model, the context meter (`used/window` from the last turn plus an
estimate of the draft), the org's usage for the period and any overage against its cap, the unread
notifications, the files attached but not sent yet, online / offline, the wait left on a rate
limit, and the reference of the last error. Usage, overage and the unread count are read again
after every answer. A failure shows the same copy as the web app and always the request reference
to quote; a key's per-minute limit (429) and a model the plan does not include (402) are states of
their own, since neither is the budget.

Each answer streamed in this session carries a line under it with the tokens it took in and out
and what they come to at the model's list price (`answer_usage = "hide"` in the config file turns
it off; an answer read back from history has no such line, as the server keeps none per message).

On a deployment that scans uploads, a file shows `(scanning…)` until the scan is clean; a message
sent before then waits for it, and a file the scan rejects is dropped with its name said.

An image in a message is drawn in the transcript where the terminal can show pictures: kitty,
Ghostty and WezTerm (the kitty protocol), iTerm2 (its inline images), and foot, mlterm, Konsole,
contour and Windows Terminal (sixel, which needs the `images` extra). Anywhere else, and whenever a
picture cannot be drawn, it is an `[image: filename]` placeholder line — the same line the Markdown
export writes. A document is always its `[document: filename]` line.

A draft is kept per conversation while the app runs. The model picker groups models as the web
does — GRT, partner, research preview — and shows a model your plan does not include with the plan
that has it, without letting you pick it. A model you pick is kept on the conversation
(`PATCH /api/conversations/{id}`), so the web app opens it on the same model. Announcements show
as a banner at start-up; the ones you dismiss stay dismissed on this device.

Stopping an answer with `Esc` closes the stream; the server keeps what it had sent as a truncated
turn, which is read back. A stream that drops is resumed once by `Last-Event-ID` (the SDK's own
resume) before it is reported as cut off.

## Configuration

`matterwave-tui config` prints where the config file is and where each setting comes from. The
file is `~/.config/matterwave/config.toml` on Linux (`$XDG_CONFIG_HOME`), `%APPDATA%\matterwave\config.toml`
on Windows and `~/Library/Application Support/matterwave/config.toml` on macOS:

```toml
base_url = "https://app.example.com"   # the deployment
model = "grt-nano"                     # the model for one-shot prompts and the app's turns
theme = "nord"                         # a Textual theme, by name
images = "auto"                        # as MATTERWAVE_IMAGES
shell = "pwsh"                         # as MATTERWAVE_SHELL
answer_usage = "hide"                  # no tokens-and-cost line under each answer
default_profile = "work"

[profiles.work]                        # matterwave-tui --profile work
base_url = "https://work.example.com"
model = "grt-max"

[keys]                                 # a key's name, as the help (?) lists it = the key
new = "ctrl+n"
```

A setting comes from the first place that has it: the command line, then the environment, then the
chosen profile, then the file's top level, then the default. A profile is chosen by `--profile`,
else `MATTERWAVE_PROFILE`, else `default_profile`, and signs in on its own keyring entry (the
server's origin and the profile's name), so two accounts on one server can sit side by side. A
setting the file does not know is an error rather than a silent default. A theme chosen from the
palette is remembered for the next start, unless the file names one.

## Environment

| Variable | Does |
|---|---|
| `MATTERWAVE_BASE_URL` | the deployment, when `--base-url` is not given |
| `MATTERWAVE_API_KEY` | an `mw_` key for this shell, used instead of the keyring and never stored |
| `MATTERWAVE_IMAGES` | `off` never draws images; `kitty`, `iterm2` or `sixel` forces a protocol; unset or `auto` detects |
| `MATTERWAVE_SHELL` | the shell the shell assistant writes and runs commands for (`pwsh`, `cmd`, `/bin/zsh`, …) |
| `MATTERWAVE_MODEL` | the model, when `-m` is not given |
| `MATTERWAVE_CONFIG` | another config file than the platform's own (see [Configuration](#configuration)) |
| `MATTERWAVE_PROFILE` | the profile, when `--profile` is not given |
| `MATTERWAVE_NO_UPDATE_CHECK` | `1` skips the once-a-day look for a newer release |
| `NO_COLOR` | any value: the app draws without colour (`--plain` sets it) |
| `MATTERWAVE_CONFIG_DIR` | agent mode's config folder, as `<dir>/matterwave` (its `settings.json`, `commands/` and `agents/`); else `$XDG_CONFIG_HOME`, else `~/.config` |
| `MATTERWAVE_STATE_DIR` | where the dismissed announcements, the prompt roles and agent mode's trusted folders, runs and checkpoints are kept, as `<dir>/matterwave-tui`; else the platform's state directory: `$XDG_STATE_HOME` or `~/.local/state` on Linux, `%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS |

## Development

```sh
uv sync --extra dev --extra images
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict && uv run pytest -q
pre-commit install   # once per clone: gitleaks refuses a commit that adds a secret
```

`tests/fake_api.py` holds `FakeAPI` (the chat, in memory) and `FakeV1`, a scripted `/v1` under
the real adapter for the agent's tests. `UPDATE_GOLDEN=1` rewrites the headless golden files in
`tests/golden/`.

Agent mode needs the SDK's `matterwave_sdk.agent` (the tool contract, confinement, the edit rule,
the diff and the agent prompt) at contract 1.2. Until that is on the SDK's repository, `uv sync`
resolves the SDK from the sibling checkout (`../python-sdk`) through `[tool.uv.sources]`, and
`uv.lock` records it; with pip, install that checkout first (`pip install -e ../python-sdk`).

CI runs the same checks, the suite on Linux, Windows and macOS (the agent's shell tool kills
process trees differently on each), and a gitleaks scan of the whole history.

The wire is the Python SDK's: `client.conversations.*` with its typed frames and its stream
resume, and the typed resources for the rest (models, attachments, shares, library, notifications,
announcements, budget, usage, keys, feedback). The few routes the SDK has no method for (an
upload's scan status, accounts, imports, speech, the account export) go through the same client.
This package keeps what is terminal-specific: the tree in memory, the failure copy, and the one
re-login a 401 gets before it stands.

## Licence

Apache License 2.0: see [LICENSE](LICENSE) and [NOTICE](NOTICE).
