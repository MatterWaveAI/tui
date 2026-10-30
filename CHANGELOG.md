# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
