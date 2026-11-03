# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.5.0] - 2026-11-03

### Added

- SDK 0.5.0 client parity (`MatterWaveClient` and `HttpChatAPI`): async helpers
  for web search (`search_web` -> `POST /v1/search`), RAG retrieval
  (`query_rag` -> `POST /v1/rag/query`, `list_rag_collections` ->
  `GET /v1/rag/collections`), MCP Server Registry (`list_mcp_servers` ->
  `GET /v1/mcp/servers`, `call_mcp_tool` -> `POST /v1/mcp/servers/{id}/call`),
  multi-file edit sessions (`create_edit_session` -> `POST /v1/edit-sessions`),
  and Agent Command Center runs (`list_agent_runs` -> `GET /api/v1/runs`), with
  graceful HTTP fallback via `httpx`.
- Interactive composer slash commands and `/help` discovery for `/search <query>`,
  `/rag [collection_id] <query>`, `/mcp [server_id]`, and `/runs [status]`.
- Organization presets (`M`, or "Choose preset…" on the command palette
  when the organisation offers presets): picking a preset sets `preset_id`
  on the conversation (`PATCH /api/conversations/{id}`, or `POST
  /api/conversations` when chosen before the first turn), shows the
  preset's name in the status line, fixes the model to the preset's model
  (the model picker explains why it cannot be changed), and shows the
  preset's welcome message in an empty conversation. Choosing "No preset"
  clears `preset_id` and restores the conversation's own model. A
  conversation on a deleted preset (`GET /api/org/presets/{id}`) still
  names it and keeps its model without offering it to other conversations.
- A conversation a connector opened (one started from a Slack routing
  rule) is captioned with where it is answered -- `Slack #support` --
  and, while open, a notice over the composer says so, with the thread's
  link. A send, edit or regenerate shows the notice instead of posting,
  since the server refuses them (409 `connector_conversation`); the fork
  (`f`) is the way to continue here. The 409 the server answers a stale
  row's send with locks the conversation the same way. Needs
  `matterwave-sdk` with `Conversation.source` and
  `ConnectorConversationError`.
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
- Marks a screen reader can say: a symbol that stands alone carries its
  word (`📌 pinned`, `👍 good`, `⛔ critical:`), list rows say "this chat",
  "current" or "unread" in words instead of a bare dot, and `--plain`
  draws every mark as its word with no colour. `NO_COLOR` is honoured.
- Each answer streamed in the session shows the tokens it took in and
  out and what they come to at the model's list price (from the `context`
  frame and `/api/models`); `answer_usage = "hide"` in the config file
  turns the line off.
- The command line's basics: `--version`; `--list-models`; one-shot
  `--continue`, `--conversation ID` and `--temporary`; a piped stdin
  after `-p` is the context the prompt is about; `export ID` prints a
  conversation as Markdown, JSON or HTML (or writes a new file with
  `-o`); `completion` prints a bash, zsh, fish or PowerShell completion
  script; `--debug` logs each request's method, path, status and request
  id, never what was said; and the app looks at PyPI once a day for a
  newer release (`--no-update-check` or `MATTERWAVE_NO_UPDATE_CHECK=1`
  turns that off).
- Composing and getting about: `Ctrl+E` writes the message in
  `$VISUAL` / `$EDITOR`; `?` or `F1` lists every key, generated from the
  bindings; the config file's `[keys]` table moves a key (`new = "ctrl+n"`);
  a theme chosen from the palette is remembered for the next start; `i`
  takes a glob (`~/shots/*.png`) for several files, and `Ctrl+O` picks them
  from a tree of the working directory.
- A config file (`matterwave-tui config` shows where, and where each
  setting comes from): the server, the model, the theme, the image mode
  and the shell, and named profiles of them chosen with `--profile` or
  `MATTERWAVE_PROFILE`. Each profile signs in on its own keyring entry.
  The command line wins over the environment, which wins over the
  profile, which wins over the file; a setting the file does not know is
  an error. `MATTERWAVE_MODEL` sets the model.
- Every option the platform takes for a turn, a share and an upload:
  `/temp 0.3` sets the open conversation's temperature for its turns
  (`/temp default` goes back to the server's); `s` opens a share dialog
  that sets how long the link lives and whether readers can open the
  attachments, copies the new link, and lists the conversation's links to
  revoke; a file larger than one upload may be, or than the storage left,
  is refused by name before its bytes are sent.
- An account screen (`U`, or "Account…" on the palette): the plan,
  billing and any moderation action in force; your API keys, to rotate
  (the new secret shown once) or revoke, with this terminal's own key
  marked and left to `logout`; the browsers signed in as you, to sign
  out; the email and notification preferences and the locale every client
  of the account reads; and your own instructions, about you and how to
  answer.
- The library manages what it lists: `r` renames a folder, `x` deletes a
  folder (its conversations return to the main list) or a tag (it comes
  off every conversation), and `Enter` narrows the sidebar to the
  highlighted folder or tag, every page of the list read so none is
  missed; `o` widens it again. Search uses the ranked `/api/search`
  (titles and messages), a page at a time behind a **More results…** row.
- Images in iTerm2 (OSC 1337 inline images), and auto-detection of the
  terminals that draw them without being told: Ghostty and WezTerm speak
  the kitty protocol, and foot, mlterm, Konsole, contour and Windows
  Terminal draw sixel. `MATTERWAVE_IMAGES=iterm2` forces the new one.

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
- `matterwave-sdk` resolves from a `python-sdk` checkout beside this one,
  carrying agent contract 1.2, instead of the pinned commit, until that
  contract is on the SDK's repository; `uv.lock` records the checkout, and
  CI checks the SDK out beside the TUI.
- CI's test matrix adds macOS, where the agent's shell tool kills process
  trees its own way.
- The README documents every key, every dialog's keys, the palette
  entries, the environment variables and the extras, and the tests check
  it against the bindings and the code; the install section says how to
  install from the repository, since neither package is on PyPI.
- One version source: `pyproject.toml` reads it from the package, and the
  package ships `py.typed`, as its "Typing :: Typed" classifier claims.
- `uv.lock` is committed, and `matterwave-sdk` (0.5.0 or later; it is not
  on PyPI) resolves from its repository at a pinned commit, so a checkout
  and CI install the same tree.
- The model list, conversation create (temporary included), delete, pin,
  unpin, archive, unarchive and a conversation's ratings go through the
  SDK's typed methods instead of hand-built requests; only the routes the
  SDK has no method for (an upload's scan status, accounts, imports,
  speech, the account export) are still built here.
- Licensed under Apache-2.0 instead of MIT: `LICENSE` is the Apache
  License 2.0, `NOTICE` names the copyright holder, and the package
  metadata declares `Apache-2.0` and ships both files.

### Fixed

- Windows: state (prompt roles, dismissed announcements) lives in
  `%LOCALAPPDATA%\matterwave-tui`, and macOS's in Application Support,
  instead of `~/.local/state` on every OS; files an earlier release left
  there move across on the first run.
- The status line's unread badge, usage and overage are read again after
  every answer, not only at start-up and after an account switch.
- Defaults that lost data or left a key live: Enter in the new-chat dialog
  now makes a regular chat (`t` makes a temporary one, Esc none); a
  Markdown or account export never overwrites an existing file, taking
  `name (2).md` instead; `logout` revokes the stored device key on the
  server before forgetting it (a key made in the web app is only
  forgotten, and the note says it stays live; `logout --local` skips the
  server); `-m` in the app is the model for every turn until another is
  picked, and a flag a command does not use is named on stderr.
- Shell assistant mode tells the model it is one: each message names the
  OS and the shell (PowerShell or cmd.exe on Windows, `$SHELL` elsewhere;
  `MATTERWAVE_SHELL` overrides), and a confirmed command runs in that
  shell rather than always in `cmd.exe` or `/bin/sh`. `--shell` works with
  `-p`: the command is offered with a `[y/N]` on the terminal, and never
  run when the prompt was piped in.
- A message sent right after attaching a file waits for the deployment's
  malware scan (`GET /api/attachments/{id}/scan`) instead of being refused
  with "One of those attachments is not available": the status line shows
  the file as `(scanning…)`, the message goes once the scan is clean, and
  a file the scan rejects leaves the message with its name said.
- Files attached but not sent stay with the conversation they were attached
  in, instead of going out with the next message of whichever conversation
  is open; `I` takes one off the message and deletes the upload; a path
  pasted with quotes round it (Windows' "Copy as path") or starting with
  `~` attaches.
- An unknown `/command` no longer wipes the composer: the text stays and
  the warning says how to send it. `//` escapes a leading slash, so
  `//etc/hosts` sends `/etc/hosts`.
- One-shot exit statuses tell the endings apart: 0 a whole answer, 1 a
  failed turn, 2 a usage error, 3 a refusal (moderation, budget, plan, rate
  limit, sign-in), 4 a cut-short answer, including a `done` marked
  truncated, which used to exit 0. `--help` lists them. The device
  sign-in's URL and code and every other note go to stderr, so
  `-p "..." > answer.txt` holds only the answer.
- Creating a tag that already exists no longer lists it twice in the
  library.
- The test suite signs in against an in-memory keyring, so a run no longer
  writes to the developer's OS keyring and passes on a runner without one.

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

## [0.4.0] - 2026-10-31

### Added

- One-shot mode: `matterwave-tui -p "prompt" [-m model]` prints
  the answer to stdout and exits — no interface, a conversation made like
  the web app's, text deltas as they arrive, exit 0 on a whole answer and
  1 on a refusal or a cut. A piped-in stdin is the prompt
  (`cat notes | matterwave-tui`). The SDK's stream is used directly; no
  Textual app boots.
- Prompt roles: a local library of named system prompts in the
  state directory (`roles.json`; `MATTERWAVE_STATE_DIR` moves it).
  `/role name` applies one to the open conversation through the existing
  instructions PUT, `/role --add name` writes one, `/role` (or the `L`
  screen, or "Prompt roles…" on the command palette) opens the library to
  apply, add and delete. Roles persist across runs.
- Shell assistant: `--shell` at launch or `/shell` in the
  composer turns it on. A finished answer is then a proposed command,
  shown in a gate that runs it only on Enter — never without that
  explicit confirmation — in a shell with a thirty-second cap, its output
  shown in the transcript. Esc leaves the command unrun.
- Clipboard copy: `y` copies the selected message, or the last
  answer, onto the clipboard (`app.clipboard`, OSC 52 where the terminal
  takes it); `Y` copies just the fenced code blocks of that message.
- Prompt queueing: sending while an answer streams queues the
  prompt and sends it when the turn completes. Esc stops the stream and
  clears the queue, with the text back in the composer; switching
  conversations keeps it as the old conversation's draft.
- The library modal: `l` opens folders and tags — `f` makes a
  folder, `t` a tag, `m` files the open conversation in the highlighted
  folder, Space toggles a tag on it — and the sidebar's captions carry
  the ` 📁 folder · #tag` suffixes as the server reports them.
- Notifications pagination and the unread filter: `u` shows only
  the unread ones, and `n`/`p` page and widen the limit window (capped at
  the route's 100). (This entry also said the unread badge followed each
  answer; it was read only at start-up. It does now: see Unreleased.)
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

### Fixed

- Pin and archive are sent with `PUT` (and cleared with `DELETE`), the
  methods the platform registers; the `POST` they used was refused.

## [0.2.1] - 2026-10-28

### Added

- Conversation management on the open conversation: rename (`F2`), delete
  (`d`, behind a confirm), pin (`p`) and archive (`a`); `o` cycles the
  sidebar between the default, pinned, archived and all conversations.
- Attachments by path (`i`), uploaded and sent with the next message;
  images and documents show as placeholder lines in the transcript.
- Markdown export of the active path (`w`), byte for byte the web
  exporter's document and filename.
- Fork from the selected message (`f`), share links (`s`), and the org's
  usage for the period in the status line.
- Temporary chats: a new chat asks first, and a temporary one is
  announced, cannot be shared or forked, and stays out of the sidebar.
- Per-conversation instructions (`t`), edited against the effective
  prompt.
- Reply feedback: thumbs on each answer that vote in place and withdraw on
  a second click, restored when the conversation opens; `b` adds reasons
  and a comment.
- An image attached for a model that cannot read images is refused by
  name, with up to three models that can.

### Changed

- A stream that drops mid-answer is resumed once by `Last-Event-ID` before
  it is reported as cut short.

## [0.2.0] - 2026-10-27

### Fixed

- The package depends on the published `matterwave-sdk` instead of a
  sibling path that only existed in the platform monorepo, so it installs.

[Unreleased]: https://github.com/MatterWaveAI/tui/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/MatterWaveAI/tui/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/MatterWaveAI/tui/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/MatterWaveAI/tui/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/MatterWaveAI/tui/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/MatterWaveAI/tui/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MatterWaveAI/tui/releases/tag/v0.2.0
