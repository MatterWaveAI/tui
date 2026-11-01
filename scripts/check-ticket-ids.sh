#!/usr/bin/env sh
# Fail when a tracked file names a private ticket (T-123, B-123): those ids
# belong to the internal tracker, and this repository is public. Commit
# messages are not files and are not checked.
set -eu
if git grep -nIE '\b[TB]-[0-9]{3,4}\b' -- . ':!scripts/check-ticket-ids.sh'; then
  echo "Tracked files name private ticket ids; describe the change in plain words instead." >&2
  exit 1
fi
