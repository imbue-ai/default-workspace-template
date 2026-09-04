#!/usr/bin/env bash
# Install mngr and system-interface as uv tools, each with the plugins
# system/config/mngr_plugins.toml assigns it. mngr and every plugin come from
# wherever pyproject.toml's [tool.uv.sources] points imbue-mngr: the pinned
# public-repo commit, or the local tree at system/vendor/mngr. Each tool is one
# command so uv resolves its whole set from one source. Extra arguments go to both
# `uv tool install` calls, e.g. `--reinstall` to rebuild tools that already exist.
set -euo pipefail

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/../.." && pwd)}"
LIST_MNGR_PLUGINS=(python3 "$REPO_ROOT/system/scripts/list_mngr_plugins.py" --repo-root "$REPO_ROOT")
mapfile -t MNGR_BASE_ARGS < <("${LIST_MNGR_PLUGINS[@]}" --base)
mapfile -t MNGR_PLUGIN_ARGS < <("${LIST_MNGR_PLUGINS[@]}" --tool mngr)
mapfile -t SI_PLUGIN_ARGS < <("${LIST_MNGR_PLUGINS[@]}" --tool system-interface)

uv tool install "$@" "${MNGR_BASE_ARGS[@]}" "${MNGR_PLUGIN_ARGS[@]}"
uv tool install "$@" -e "$REPO_ROOT/system/apps/system_interface" "${SI_PLUGIN_ARGS[@]}"
