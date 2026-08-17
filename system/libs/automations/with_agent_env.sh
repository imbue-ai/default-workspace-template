#!/usr/bin/env bash
#
# CLEANUP: compatibility shim -- the wrapper moved to
# system/scripts/with_agent_env.sh. Cron entries live in data/.state/cron.d/,
# which is gitignored and survives update-self, so workspaces scheduled before
# the move still invoke this path; without this they would fail silently into
# their log files. Delete once no workspace has a data/.state/cron.d/ entry
# naming this path (the manage-scheduled-tasks skill writes the new one).
set -euo pipefail

# Resolved from this file's own location, so the shim execs the wrapper from the
# same checkout it was invoked out of rather than a hardcoded absolute path.
here="$(cd -- "$(dirname -- "$0")" && pwd)"
exec "$here/../../scripts/with_agent_env.sh" "$@"
