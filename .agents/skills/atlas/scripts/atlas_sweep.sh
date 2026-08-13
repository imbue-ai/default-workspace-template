#!/usr/bin/env bash
# Atlas idle backstop sweep -- for cron via run_job.sh --every 15m.
#
# The checkpoint clock only fires while an agent is taking tool calls. This sweep
# keeps things current when the workspace is idle:
#
#   - ALWAYS (free, no model): refresh §0 for every live topic and log a
#     checkpoint event, so a returning user always sees a current status line.
#   - OPT-IN (spends tokens): if ATLAS_SWEEP_DETECT=1, run topic detection
#     (proposes new topics); the detector's own heuristic gate bounds cost.
#     Opt-in live-tier refreshes still happen via the checkpoint engine's spawn.
#
# Non-blocking, best-effort; always exits 0.
# strict mode; the guard uses if/fi (not `&& exit`) so `-e` never trips on it.
set -euo pipefail

root="${MNGR_AGENT_WORK_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || pwd)}"
scripts="${root}/.agents/skills/atlas/scripts"
if [ ! -d "${root}/atlas/topics" ]; then
    exit 0
fi

# Free tier: refresh §0 for every live topic (branch-independent).
timeout 60 python3 "${scripts}/atlas_checkpoint.py" --reason manual --all \
    --repo-root "${root}" >/dev/null 2>&1 || true

# Opt-in detection (spends tokens): off unless explicitly enabled.
if [ "${ATLAS_SWEEP_DETECT:-0}" = "1" ]; then
    timeout 120 python3 "${scripts}/atlas_detect.py" --repo-root "${root}" >/dev/null 2>&1 || true
fi

exit 0
