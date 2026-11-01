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

Two extras add what a terminal may not need:

| Extra | Adds |
|---|---|
| `images` | Pillow: sixel images, and JPEG/GIF/WebP on the kitty protocol (PNG needs nothing) |
| `dictation` | sounddevice: dictation from the microphone (without it, dictation transcribes a file) |

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
| `matterwave-tui --list-models` | the models this account can use, the default marked |
| `matterwave-tui export ID [-f md\|json\|html] [-o FILE]` | one conversation's active path to stdout, or to a file that is not there yet |
| `matterwave-tui completion bash\|zsh\|fish\|powershell` | the completion script, to source from the shell's profile |
| `matterwave-tui config` | the config file, and where each setting comes from |
| `matterwave-tui --debug [FILE]` | log each request's method, path, status and request id (never what was said) to FILE, or `debug.log` in the state directory |
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
answer also carries two thumbs: a click rates it, a second click on the same thumb takes it back.

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

- **Account…** — the same screen as `U`
- **Switch account…**
- **Import a ChatGPT or Claude export…**
- **Export this account's data…** — the whole account as one JSON file, saved beside the terminal
- **Dictate audio…**
- **Prompt roles…**

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
| `MATTERWAVE_STATE_DIR` | where the dismissed announcements and the prompt roles are kept, as `<dir>/matterwave-tui`; else the platform's state directory: `$XDG_STATE_HOME` or `~/.local/state` on Linux, `%LOCALAPPDATA%` on Windows, `~/Library/Application Support` on macOS |

## Development

```sh
uv sync --extra dev --extra images
uv run ruff check . && uv run ruff format --check . && uv run mypy --strict && uv run pytest -q
pre-commit install   # once per clone: gitleaks refuses a commit that adds a secret
```

CI runs the same checks, the suite on Linux and Windows, and a gitleaks scan of the whole history.

The wire is the Python SDK's: `client.conversations.*` with its typed frames and its stream
resume, and the typed resources for the rest (models, attachments, shares, library, notifications,
announcements, budget, usage, keys, feedback). The few routes the SDK has no method for (an
upload's scan status, accounts, imports, speech, the account export) go through the same client.
This package keeps what is terminal-specific: the tree in memory, the failure copy, and the one
re-login a 401 gets before it stands.
