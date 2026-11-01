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
(`-m` picks the model; in the app, `-m` is the model for every turn until you pick another). A flag
the command does not use (`-m` with `logout`, say) is named on stderr rather than ignored silently. A piped-in stdin is the prompt, so scripts work the usual way:

```sh
cat notes.md | matterwave-tui -m grt-nano
echo "explain this trace" | matterwave-tui -p > answer.txt
```

stdout carries the answer and nothing else: a device sign-in's URL and code, and every note, go to
stderr. The exit status says how the turn ended:

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
| `n` | transcript | new conversation: Enter a regular one, `t` a temporary one (deleted 24 h after its last message), Esc none |
| `/` | transcript | search conversations |
| `F2` | anywhere | rename the open conversation |
| `p` | anywhere | pin / unpin the open conversation |
| `a` | anywhere | archive / unarchive the open conversation |
| `d` | anywhere | delete the open conversation (asks first) |
| `f` | transcript | fork the conversation from the selected message |
| `s` | anywhere | create a share link for the conversation |
| `w` | anywhere | export the conversation to a Markdown file in the working directory; an existing file is never overwritten (`name (2).md`) |
| `i` | anywhere | attach a file (its path, quoted or not, `~` allowed); it goes with this conversation's next message |
| `I` | anywhere | take a pending file off the next message (and delete the upload) |
| `o` | anywhere | cycle the sidebar list: default → pinned → archived → all |
| `l` | anywhere | the library: folders and tags for the open conversation |
| `L` | anywhere | prompt roles: apply, add or delete a named system prompt |
| `y` | transcript | copy the selected message (or the last answer) to the clipboard |
| `Y` | transcript | copy just the fenced code blocks of that message |
| `Ctrl+Q` | anywhere | quit |

Typing while an answer streams queues the prompt; it sends itself when the answer finishes, and
`Esc` stops the stream and puts it back in the composer. A leading `/` in the composer is a
command, not a message: `/role name` applies a saved prompt role, `/role --add name` makes one,
`/shell` toggles the shell assistant. An unknown command stays in the composer; start a message
with `//` to send it with a single leading slash (`//etc/hosts is empty` sends `/etc/hosts is empty`).

Letter keys apply when focus is not in the composer or the search box; `Esc` gets you there.

The status line also carries the org's balance for the period (`usage used/included (%)`, from
`/api/me/usage`, read again after every answer along with the overage and the unread count) and any files attached but not sent yet. On a deployment
that scans uploads, a file shows `(scanning…)` until the scan is clean; a message sent before then
waits for it, and a file the scan rejects is dropped with its name said. An image in a
message is drawn in the transcript where the terminal can show pictures: kitty, Ghostty and
WezTerm (the kitty protocol), iTerm2 (its inline images), and foot, mlterm, Konsole, contour and
Windows Terminal (sixel, which needs the `images` extra: `pip install matterwave-tui[images]`).
`MATTERWAVE_IMAGES=off` turns it off and `kitty`, `iterm2` or `sixel` forces one. Anywhere else,
and whenever a picture cannot be drawn, it is an `[image: filename]` placeholder line — the same
line the Markdown export writes. A document is always its `[document: filename]` line.

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
uv sync --extra dev --extra images
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict && uv run pytest -q
pre-commit install   # once per clone: gitleaks refuses a commit that adds a secret
```

CI runs the same checks, the suite on Linux and Windows, and a gitleaks scan of the whole history.

The wire is the Python SDK's (`matterwave-sdk` from your package index): `client.conversations.*`,
its typed frames and its stream resume, plus `client.attachments`, `client.shares` and
`client.usage` for the rest. The routes the SDK has no method for yet (`/api/models`, the
conversation delete, pin and archive) go through the same client. This package keeps what is
terminal-specific: the tree in memory, the failure copy, and the one re-login a 401 gets before
it stands.
