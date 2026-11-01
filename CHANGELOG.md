# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
- The test suite signs in against an in-memory keyring, so a run no longer
  writes to the developer's OS keyring and passes on a runner without one.

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
