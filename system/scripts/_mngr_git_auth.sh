#!/usr/bin/env bash
# Authenticate git (and so uv, which shells out to it) against the private mngr repo
# when a credential has been delivered to this build.
#
# The workspace's mngr pin (pyproject.toml, [tool.uv.sources]) names either the public
# mirror or the private mngr-internal repo. A build from the private repo needs a
# token; whoever runs the build delivers one to MNGR_INTERNAL_GIT_TOKEN_FILE (a
# BuildKit secret mount on docker, an uploaded file on lima/modal). This file turns
# that token into a git URL rewrite carried purely by environment variables, so no
# config file is ever written and nothing survives the build. An absent or empty
# file means a public pin, and this is a no-op.
#
# Usage (source it, then):
#   mngr_git_auth_export    # export GIT_CONFIG_* when a token is present

set -euo pipefail

MNGR_INTERNAL_GIT_TOKEN_FILE="${MNGR_INTERNAL_GIT_TOKEN_FILE:-/run/secrets/mngr_internal_git_token}"
MNGR_INTERNAL_REPO_URL="https://github.com/imbue-ai/mngr-internal"

mngr_git_auth_export() {
    if [ ! -s "$MNGR_INTERNAL_GIT_TOKEN_FILE" ]; then
        return 0
    fi
    local token
    token="$(tr -d '[:space:]' < "$MNGR_INTERNAL_GIT_TOKEN_FILE")"
    if [ -z "$token" ]; then
        return 0
    fi
    local index="${GIT_CONFIG_COUNT:-0}"
    export "GIT_CONFIG_KEY_${index}=url.https://x-access-token:${token}@github.com/imbue-ai/mngr-internal.insteadOf"
    export "GIT_CONFIG_VALUE_${index}=${MNGR_INTERNAL_REPO_URL}"
    export GIT_CONFIG_COUNT=$((index + 1))
    export GIT_TERMINAL_PROMPT=0
}
