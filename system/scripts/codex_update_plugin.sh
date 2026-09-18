#!/usr/bin/env bash
set -euo pipefail

# Run after mngr writes the per-agent config, before the first Codex process.
# Never install into an inherited CODEX_HOME: that may be the account's shared
# authentication home, or the parent agent's home. mngr only sets CODEX_HOME on
# the launched Codex process, not on extra provisioning commands.
# Like the Claude installer, outages warn without preventing agent creation;
# --strict is available for verification. No cache is deleted.
STRICT=0
if [[ $# -eq 1 && "$1" == "--strict" ]]; then
    STRICT=1
elif [[ $# -ne 0 ]]; then
    echo "usage: $0 [--strict]" >&2
    exit 2
fi

if [[ -z "${MNGR_AGENT_STATE_DIR:-}" ]]; then
    echo "error: MNGR_AGENT_STATE_DIR is required; refusing a shared-home install" >&2
    exit 1
fi
export CODEX_HOME="$MNGR_AGENT_STATE_DIR/plugin/codex/home"
if [[ ! -f "$CODEX_HOME/config.toml" ]]; then
    echo "error: Codex must be provisioned before installing its plugins" >&2
    exit 1
fi

if ! command -v codex &>/dev/null; then
    echo "warning: codex is not on PATH; code-guardian could not be installed" >&2
    exit "$STRICT"
fi

export GIT_TERMINAL_PROMPT=0
export GIT_SSH_COMMAND='ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=5'
# The marketplace source and plugin enablement live in mngr config_overrides,
# so a config rewrite cannot strand the cached Git marketplace. Upgrade also
# fetches the initial snapshot for a newly provisioned home.
if ! codex plugin marketplace upgrade imbue-code-guardian; then
    echo "warning: could not refresh code-guardian; attempting the cached marketplace" >&2
fi

if ! codex plugin add imbue-code-guardian@imbue-code-guardian; then
    echo "warning: code-guardian installation failed; this Codex agent may lack review skills and Stop checks" >&2
    exit "$STRICT"
fi
