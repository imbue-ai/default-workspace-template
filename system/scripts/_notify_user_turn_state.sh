# Shared state for the finish-notification nudge: where one turn's state lives,
# and the step snapshot that decides whether the turn did any work.
#
# Sourced by agent_notify_user_turn_start.sh (UserPromptSubmit, which opens a
# turn) and agent_notify_user_stop_nudge.sh (Stop, which judges it). The third
# writer is the notify-user skill's own script, which drops the `sent` marker
# here after the app accepts a notification -- it is python and cannot source
# this file, so it recomputes the same path. The two ends are held together by
# a test that runs the real script and then the real hook.
#
# State is keyed by chat, not by work dir: every chat agent in a workspace
# shares one checkout, so a single directory of markers would be every chat's
# at once.

# The directory holding this chat's turn state.
notify_user_state_dir() {
    local repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
    local key="${MINDS_CHAT_ID:-${MNGR_AGENT_ID:-unknown}}"
    printf '%s/data/.state/notify-user/%s' "$repo_root" "${key//\//_}"
}

# This chat's step records, one line each, as `tk steps` prints them. Compared
# between the start and the end of a turn: a turn that created, started or
# closed a step did work worth telling the user about, and a turn that touched
# none of them was chitchat, a clarifying question, or a single quick read (the
# same carve-out AGENTS.md makes for step records themselves).
#
# Empty when tk is not usable, which makes the two snapshots equal and the
# nudge silent -- the fail-safe direction.
notify_user_step_snapshot() {
    local repo_root="${MNGR_AGENT_WORK_DIR:-$(pwd)}"
    local tickets_dir="${TICKETS_DIR:-${repo_root}/.tickets}"
    local tk_script="${repo_root}/system/vendor/tk/ticket"
    [[ -d "$tickets_dir" && -x "$tk_script" ]] || return 0
    export TICKETS_DIR="$tickets_dir"
    # `tk steps` exits non-zero when there are no steps at all, which would
    # abort the caller under `set -e`; same guard as the open-steps hooks.
    "$tk_script" steps 2>/dev/null || true
    "$tk_script" steps --status=closed 2>/dev/null || true
}
