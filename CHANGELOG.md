# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-10-30

### Added

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
