#!/usr/bin/env bash
#
# with_agent_env.sh -- run a command with the agent environment restored.
#
# cron constructs a minimal environment for its jobs (roughly just
# HOME/PATH/SHELL/LOGNAME), so none of the agent env survives into them:
# MNGR_HOST_DIR, MNGR_AGENT_ID, LATCHKEY_*, the PATH that puts uv at
# /root/.local/bin, and so on. All of it is reconstructable from files mngr
# maintains on the host dir, so this wrapper rebuilds it the same way mngr
# itself does for agent operations (build_source_env_prefix): source the host
# env file, then the services agent's own env file on top (agent overrides
# host), exactly like system/scripts/minds_start_services_agent.sh. It then puts
# /root/.local/bin (uv, mngr) on PATH, cds to the repo root, and execs the
# given command.
#
# One deliberate gap: GH_TOKEN is injected per-process into agent sessions and
# never written to these env files, so cron jobs run without it. The only
# consumer is the opt-in github-sync post-commit hook, which silently no-ops
# when the token is absent -- a commit made from a cron job is pushed on the
# next agent-context commit instead.
#
# Every cron job -- the built-in Caretaker entry and any user-added job alike
# -- should be prefixed with it:
#
#   17 3 * * *   root   /home/user/workspace/system/libs/automations/with_agent_env.sh bash my_job.sh >> /var/log/supervisor/my-job.log 2>&1
#
# One deliberate exception: the built-in update-apply recovery guard the
# bootstrap writes (/etc/cron.d/update-apply-recover). It exists to repair a
# workspace left mid-update, which is exactly where the env file and jq below
# cannot be assumed, so it carries its own PATH line and cd instead.
set -euo pipefail

if [ ! -f /home/user/.mngr/env ]; then
    echo "with_agent_env.sh: /home/user/.mngr/env not found -- this wrapper only works inside a mngr-managed workspace container" >&2
    exit 1
fi

set -a
# shellcheck source=/dev/null
. /home/user/.mngr/env
host_dir="${MNGR_HOST_DIR:-/home/user/.mngr}"
# The services agent (labelled is_primary=true) is the one whose environment
# background jobs run under; its env file carries the per-agent vars
# (MNGR_AGENT_ID, MNGR_AGENT_STATE_DIR, ...).
for data_file in "$host_dir"/agents/*/data.json; do
    [ -e "$data_file" ] || continue
    if [ "$(jq -r '.labels.is_primary // empty' "$data_file" 2>/dev/null)" = "true" ]; then
        agent_env="$(dirname "$data_file")/env"
        # shellcheck source=/dev/null
        [ -f "$agent_env" ] && . "$agent_env"
        break
    fi
done
set +a

# cron hands its jobs a bare PATH (typically /usr/bin:/bin), so anything installed
# under /usr/local/bin -- latchkey included -- is invisible to a scheduled job
# unless the wrapper names it. The same list bootstrap gives the recovery cron entry.
export PATH="/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
cd /home/user/workspace
exec "$@"
