# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/MatterWaveAI/tui/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/MatterWaveAI/tui/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/MatterWaveAI/tui/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/MatterWaveAI/tui/compare/v0.2.1...v0.3.0
[0.2.1]: https://github.com/MatterWaveAI/tui/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/MatterWaveAI/tui/releases/tag/v0.2.0
