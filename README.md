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
| `Ctrl+Q` | anywhere | quit |

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

The wire is the Python SDK's (`matterwave-sdk` from your package index): `client.conversations.*`,
its typed frames and its stream resume, plus `client.attachments`, `client.shares` and
`client.usage` for the rest. The routes the SDK has no method for yet (`/api/models`, the
conversation delete, pin and archive) go through the same client. This package keeps what is
terminal-specific: the tree in memory, the failure copy, and the one re-login a 401 gets before
it stands.
