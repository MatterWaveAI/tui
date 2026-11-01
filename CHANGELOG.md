# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Coding-agent mode: `matterwave-tui code [path]` opens the agent on a
  folder as a screen of the app (also on the command palette). It runs
  its own loop against `/v1/chat/completions` with the SDK's agent
  contract (`matterwave_sdk.agent` 1.2.0): the seven tools (`read_file`,
  `list_dir`, `glob`, `grep`, `edit`, `write_file`, `run_shell`), path
  confinement through the SDK's `confine` (no `..`, nothing outside the
  root, no link out of it), the deny list (`.env*`, `*.pem`, `id_*`,
  `.git/`, `.matterwaveignore`) for reads and writes, `.gitignore` and
  `.matterwaveignore` for search, and the versioned agent prompt. The
  server never runs a tool.
- Approval modes `plan`, `ask` (the default), `auto-edit` and `yolo`;
  Shift+Tab cycles them and the status line always shows the mode. `yolo`
  needs `--yolo` each session and is never saved; `--yolo` without a
  sandbox warns. In `ask` mode each change opens its unified diff and is
  written only on `y`; `n` leaves the file byte for byte as it was.
- Changes to protected files (the contract's list: `.matterwave/`, the
  context files, `.matterwaveignore`, the shell startup files, `.ssh/`,
  `.aws/`, `.config/matterwave`) are asked about in every mode and
  refused headless. An approval dialog takes no answer in its first
  moments and Enter never answers it.
- Stop (`Esc`) closes the stream and kills a running command with
  everything it started, what it detached included: a job object made
  while the command is suspended on Windows, a child-subreaper keeper on
  Linux, the process group and its live descendants on macOS. Background
  children end with their command.
  Each run has a turn cap and a token cap; a 402 or 429 ends it with the
  chat's own copy. The agent says it is unavailable when the org has
  switched local agent tools off (`local_agent_tools` on `/api/me`) or the
  model cannot call tools (`supports_tools`); a missing field allows it.
- Runs are stored with `metadata.conversation_id` (tool rounds included,
  so they open in the web); `--continue`, `--resume <id>` and `/resume`
  pick one up with its earlier tool rounds in context.
- Context files: `MATTERWAVE.md` / `AGENTS.md` at the root and a personal
  `MATTERWAVE.local.md`, loaded only in a folder the user trusted (asked
  once per folder, remembered), with the account's custom instructions on
  top; `/init` drafts one. `@path` attaches a file or folder.
- File checkpoints in a shadow git repository outside the project before
  each applied change; `/restore` puts the files back and leaves the
  project's own `.git` alone.
- Headless agent: `-p` with `code` or any agent option, with
  `--output-format text|json|stream-json`, `--approval-mode`,
  `--max-turns`, `--max-tokens`, `--continue`, `--resume`, `--trust`,
  `--sandbox` and `--no-store`. It never prompts; exit codes 0 done,
  1 error, 2 usage, 3 budget / rate limit, 4 approval refused, 5 cap.
  Plain `-p` is still the chat one-shot.
- Slash commands in the agent's composer: `/help`, `/model`,
  `/approval-mode [--save]`, `/compact`, `/context`, `/restore`, `/init`,
  `/resume`, `/trust`, `/mcp`, `/agents`, `/clear`, `/chat`, and custom
  commands from Markdown files (`commands/` in the config folder or a
  trusted project's `.matterwave/commands/`).
- Agent compaction: old tool output is folded first (no model call). Mid-run
  the oldest rounds are then left out of the prompt, and before the next
  prompt a stored run is summarised with the conversation's compaction; the
  summary rides in the system prompt, so each round is stored on one path
  and each stored user turn is the user's own words.
- MCP client: stdio and Streamable HTTP servers from user and (trusted)
  project settings, each with a tool allow-list and `trust`; MatterWave's
  own `/mcp` pre-wired with the user's credential. MCP writes go through
  the same approvals; a tool's read-only hint counts only from a trusted
  server.
- A task list tool (`todo_write`), the plan-mode exit (`exit_plan_mode`),
  client web fetch behind approval (`web_fetch`), and subagents from
  Markdown definitions with their own prompt and tool allow-list, nested
  under the parent's mode, approvals and caps.
- An optional shell sandbox, off by default: podman or docker (only the
  root mounted, with its `.git` and `.matterwave` read-only, read-only
  elsewhere, no network, removed on Stop), bubblewrap on Linux, seatbelt
  on macOS. Web fetch does not follow redirects.
- `FakeV1` in `tests/fake_api.py`: a scripted `/v1` under the real adapter
  and the SDK, with golden files for the headless output formats.

### Security

- Escape sequences from files, diffs, commands, MCP servers and the model
  are spelled out (`\x1b`) in the agent screen and the headless text
  output, with bidi overrides and, in approval dialogs, zero-width
  characters: nothing shown can redraw a dialog or set the clipboard.
  Pick lists no longer read titles and file names as markup, and only
  `http`/`https` links in the agent's answers open on a click (Textual
  hands any link to the system's opener, which runs a `file:` script).
- A write re-checks after the approval: if the file changed or its path
  now leads elsewhere (a folder swapped for a link), nothing is written.
  Overwriting a binary file shows the full text that replaces it.
- NTFS streams (`key.pem::$DATA`), names ending in a dot or a space
  (`.git `), device names (`CON`, `NUL.txt`) and `\\?\` or network paths
  are refused, on every platform (the SDK's `confine`).
- Every write asks, in every mode, when the workspace holds the home
  folder, and so does a write to the user's own MatterWave config or state
  folder inside the workspace. In `auto-edit`, a command's approval names
  the files changed without asking since the last approval.
- Project context files, commands and subagents are read under the tools'
  confinement (a link out of the root is not loaded); `/restore` confines
  each path again; the checkpoint store is readable by the user alone.
- Reads are bounded: `read_file` reads files up to 16 MB and shows a
  window at a time, `grep` skips files over 4 MB, pipes and devices are
  not read, and edits work on files up to 8 MB. An HTTP MCP answer over
  16 MB is refused, each MCP
  request and each web fetch has an overall deadline, a stdio server that
  sends an unreadable line fails at once, and MCP tools that clash once
  their names are made safe are both left out.

### Changed

- Agent mode follows agent contract 1.2.0, so it reads, searches, gates
  and refuses as the VS Code extension and the desktop app do. What the
  SDK now owns is no longer the TUI's own copy: the search rules and walk
  (`SearchIgnore`, `walk`; matching ignores case on Linux too), the read
  tools and their text (`run_read_file`, `run_list_dir`, `run_glob`,
  `run_grep`), run_shell's result (`format_shell`), the decision for each
  of the TUI's own tools by kind (`allowed`, `tools_for_mode`), the
  refusal sentences (`refusal`), the prompt's slots for extra tools and
  context files, the protected list and the Windows-name refusals
  (`confine`), and the availability flags (`tools_of`,
  `local_agent_tools_of`). The contract's conformance cases run through
  the TUI's tools in its tests. What users and models will notice:
  - Every MCP call asks in `ask` and `auto-edit`, a trusted server's
    included; `"trust": true` now only takes the server's read-only hint,
    so its read-only tools are offered in `plan`, where they ask.
  - Protected writes, now including the home folder's shell startup files
    and credential folders at any depth, ask in `yolo` too.
  - A rejected call is an ordinary result in the contract's words; refusals
    use the contract's sentences (`headless`, `needs_approval`,
    `unknown_tool`, `mode`).
  - `list_dir` shows protected entries (`AGENTS.md`, `.matterwaveignore`);
    `glob("*.ts")` matches at any depth and a pattern with `/` matches
    under `path`; grep lines are cut at 400 characters and a grep `glob`
    with `/` is matched under `path`; read_file's cut marker and empty-file
    text, and run_shell's status line (`Exit code N`, `Stopped after N s:
    ...`) and middle-cut marker, are the contract's; a command's output
    keeps its last line break.
  - Context files go into the prompt's fenced instructions slot, cut at
    32,768 characters; the account's instructions follow them.
  - A command Stop ends answers its call with the output so far, under
    "Stopped by the user", instead of a stand-in.
  - A command that exits while something it detached holds its output
    open ends at once, with that process, instead of running to its
    timeout (asyncio's `wait()` waits for the pipe to close as well).
  - A loop of links is refused (`loop`); a pipe, socket or device is
    never opened and answers `special_file` instead of "does not exist";
    a `[...]` range that runs backwards in a glob is the `invalid_glob`
    error, and such a line in `.gitignore` or `.matterwaveignore` (or a
    leading BOM) is skipped instead of failing every search; an absolute
    path is accepted under a root inside folders named like Windows
    devices (`con`, `x.`).
  - grep runs in a Python process of its own with a 30-second deadline.
    A long search answers with the contract's `timeout` error; one stuck
    inside a single match (a pattern that backtracks without end, which
    Python's `re` cannot interrupt) is killed, so it no longer freezes
    the TUI. Stop kills it too.
  - The context files are read in the contract's order (`CONTEXT_FILES`:
    `MATTERWAVE.md`, `AGENTS.md`, then the personal `MATTERWAVE.local.md`)
    and run_shell's cut is the SDK's own (`format_shell(omitted=)`) from
    the start and end the TUI kept.
- The SDK floor rises to `matterwave-sdk>=0.5.0`, and uv resolves the SDK
  from a sibling `python-sdk` checkout carrying agent contract 1.2 until
  the release that carries it is published.
- CI runs on Linux, Windows and macOS, and runs mypy.

## [0.4.0] - 2026-10-31

### Added

- One-shot mode (T-210): `matterwave-tui -p "prompt" [-m model]` prints
  the answer to stdout and exits — no interface, a conversation made like
  the web app's, text deltas as they arrive, exit 0 on a whole answer and
  1 on a refusal or a cut. A piped-in stdin is the prompt
  (`cat notes | matterwave-tui`). The SDK's stream is used directly; no
  Textual app boots.
- Prompt roles (T-211): a local library of named system prompts in the
  state directory (`roles.json`; `MATTERWAVE_STATE_DIR` moves it).
  `/role name` applies one to the open conversation through the existing
  instructions PUT, `/role --add name` writes one, `/role` (or the `L`
  screen, or "Prompt roles…" on the command palette) opens the library to
  apply, add and delete. Roles persist across runs.
- Shell assistant (T-212): `--shell` at launch or `/shell` in the
  composer turns it on. A finished answer is then a proposed command,
  shown in a gate that runs it only on Enter — never without that
  explicit confirmation — in a shell with a thirty-second cap, its output
  shown in the transcript. Esc leaves the command unrun.
- Clipboard copy (T-213): `y` copies the selected message, or the last
  answer, onto the clipboard (`app.clipboard`, OSC 52 where the terminal
  takes it); `Y` copies just the fenced code blocks of that message.
- Prompt queueing (T-214): sending while an answer streams queues the
  prompt and sends it when the turn completes. Esc stops the stream and
  clears the queue, with the text back in the composer; switching
  conversations keeps it as the old conversation's draft.
- The library modal (T-215): `l` opens folders and tags — `f` makes a
  folder, `t` a tag, `m` files the open conversation in the highlighted
  folder, Space toggles a tag on it — and the sidebar's captions carry
  the ` 📁 folder · #tag` suffixes as the server reports them.
- Notifications pagination and the unread filter (T-216): `u` shows only
  the unread ones, `n`/`p` page and widen the limit window (capped at the
  route's 100), and the status line's unread badge follows each answer.

- The account-wide export: "Export this account's data…" on the command
  palette queues `POST /api/me/export`, polls the job to `ready` the way
  an import is watched, and saves the bytes behind its fresh signed
  download link to `matterwave-export-<date>.json` beside the terminal.
  The wire adapters (`export_inline`, `start_export`, `export_status`,
  `export_download`) sit behind the same `ChatAPI` protocol as the rest.

### Changed

- Announcements, share management (mine, per conversation, revoke) and
  the library (folders, tags, move, set tags) go through the SDK's typed
  resources instead of hand-rolled routes; the adapters' signatures
  stand. `revoke_share` now answers `{}`: the typed resource discards
  the route's body, which nothing read. The SDK floor rises to
  `matterwave-sdk>=0.3.0` for those resources.
- The SDK client's `_send` override mirrors the supertype's
  `timeout`/`max_retries` parameters, so the 401-renewal path keeps
  passing them through (typing only; no behaviour change).

## [0.3.1] - 2026-10-30

### Added

- Per-message feedback plumbing: the reply's `message_id` rides the
  feedback dialog end to end, so the rating it saves targets the reply the
  dialog was opened on; and a report (`R`) is about the selected reply —
  `POST /api/conversations/{id}/report` carries its `message_id` — where
  one is selected, the whole conversation otherwise.
- Multi-account switching: an `Accounts` screen (command palette,
  "Switch account…") listing who is signed in via `GET /api/accounts` with
  the acting one marked, `POST /api/accounts/switch` making the chosen
  slot acting, the switch acknowledged by name, and everything the open
  conversation showed — sidebar, notifications, usage, budget — read back
  from the account that is acting now.
- History import: "Import a ChatGPT or Claude export…" uploads the file
  (`POST /api/me/imports`, capped at the API's 100 MB), polls the job to
  `done` or `failed`, and reports the counts it did — conversations,
  messages, skipped — or the failure it named.
- Dictation (`v`, and "Dictate audio…" on the command palette): the
  microphone through the optional `dictation` extra
  (`pip install matterwave-tui[dictation]`, a sixty-second clip), or a
  named audio file where the extra is missing; the clip goes to
  `POST /api/speech/transcribe` and the transcript lands at the composer's
  caret. Unsupported suffixes are refused before the upload.
- Image rendering: an image part of a message draws through the kitty
  graphics protocol when the terminal is one (env-detected; sixel forced
  with `MATTERWAVE_IMAGES=sixel`, behind Pillow), bytes fetched per
  message via `GET /api/attachments/{id}` and its short-lived URL.
  `MATTERWAVE_IMAGES=off` disables it, and every failure — no backend, no
  Pillow, a failed download — falls back to the `[image: filename]`
  placeholder.

### Changed

- The notifications and budget adapters go through the SDK's typed
  resources (`notifications.list/read/read_all`, `budget.get`) instead of
  hand-rolled HTTP; the typed rows are flattened to the app's dicts at the
  adapter edge.

[0.3.1]: https://github.com/MatterWaveAI/tui/compare/v0.3.0...v0.3.1

## [0.3.0] - 2026-10-30

### Added

- Notifications: an inbox panel (`N`) listing the caller's rows from
  `GET /api/me/notifications`, with `r` to mark the highlighted one read,
  `a` to read all, and an unread badge in the status line.
- Announcements: the active ones from `GET /api/announcements` show as a
  banner at startup; `A` dismisses the dismissible ones, and the dismissed
  ids are remembered per device. A critical announcement stays until the
  operator ends it.
- Reports: `R` files a report about the open conversation through
  `POST /api/conversations/{id}/report` — the kind (`unsafe` or
  `unhelpful`), an optional note, and a confirm; the filed id is named in
  the acknowledgement.
- Share management: a `Shares` screen (`S`) listing every link the caller
  has made (`GET /api/me/shares`) with revoke (`DELETE /api/shares/{id}`)
  behind a confirm.
- The status line shows the org's overage against its cap when
  `GET /api/budget` reports overage billing, with a warning at the cap.

[0.3.0]: https://github.com/MatterWaveAI/tui/compare/v0.2.1...v0.3.0
