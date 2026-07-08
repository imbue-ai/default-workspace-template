#!/usr/bin/env bash
#
# run_daily_job.sh -- run a daily job at its due hour, or the first minute the
# container is up after a missed day.
#
# Invoked every minute by a /etc/cron.d line (through with_agent_env.sh). This
# replaces anacron for daily jobs: anacron's date stamps are day-granular, so a
# single anacron entry cannot both hold a job until 3 AM (a 24/7-evaluated
# entry becomes "due" right after midnight) and still catch up a missed day at
# any hour. This script's stamp is also a date, but because the script sees the
# clock it can apply the rule anacron cannot:
#
#   at most one run per calendar day, and a run happens when today is not yet
#   covered AND (it is at or past DUE_HOUR, or a whole earlier day was missed
#   -- then run immediately, whatever the hour).
#
# So: up at 3 AM -> runs at 3 AM. Asleep at 3 AM, woken at 09:49 -> runs at
# 09:49. Woken at 00:30 the night after a successful run -> silent (nothing
# was missed). Woken at 00:30 after a fully missed day -> runs at 00:30.
#
# The stamp is written before the job starts (anacron-style), so a failing job
# is retried the next day rather than every minute; failures are visible in the
# job's log. A missing stamp (first boot before the bootstrap seeded it) is
# treated conservatively: run only at/after DUE_HOUR, never in the small hours.
# The flock is held for the job's whole duration, so overlapping ticks skip.
#
# Usage: run_daily_job.sh <job-id> <due-hour> <command...>
set -euo pipefail

JOB_ID="$1"
DUE_HOUR="$2"
shift 2

# Stamps live on the container rootfs (not under runtime/, so they are never
# backed up -- a recreated container starts fresh and the bootstrap re-seeds).
# The env override exists for tests.
STAMP_DIR="${FCT_DAILY_STAMP_DIR:-/var/lib/fct/daily-stamps}"
STAMP="$STAMP_DIR/$JOB_ID"
mkdir -p "$STAMP_DIR"

# One invocation at a time per job; held until the job exits.
exec 9>"$STAMP.lock"
flock -n 9 || exit 0

today=$(date +%F)
last=$(cat "$STAMP" 2>/dev/null || echo "")
[ "$last" = "$today" ] && exit 0

yesterday=$(date -d yesterday +%F)
hour=$((10#$(date +%H)))
if [ "$hour" -ge "$DUE_HOUR" ] || { [ -n "$last" ] && [[ "$last" < "$yesterday" ]]; }; then
    printf '%s\n' "$today" > "$STAMP"
    exec "$@"
fi
exit 0
