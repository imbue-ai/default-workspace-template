#!/usr/bin/env bash
# Installs everything datalib needs to run, the first time the datalib skill
# is used: the binaries (datalib-http for the Datalib app, datalib-dag and the
# rest for the skill), one pinned release for both so the UI and the agent's
# tools never disagree about the store format, datalib's guide for agents,
# and the Node runtime a sync shells out to. Then it adds the Datalib app's
# program to supervisord, which starts it. Nothing here runs at boot, so a
# workspace that never uses datalib downloads nothing and has no datalib
# program.
#
# Safe to run every time: each half checks what is already in place first.
#
# The release lands in a directory named after the pin, under the persistent
# home, and ~/.local/bin gets one symlink per binary, so a pin bump installs
# beside the old release and relinks. datalib-http finds its sibling binaries
# through the resolved executable path, which is why the binaries themselves
# stay together in that directory.
set -euo pipefail

# The skill (../SKILL.md) names the same version;
# system/apps/datalib/test_datalib_pin.py fails when the two disagree.
readonly PINNED_VERSION="v0.46.0"

readonly INSTALL_ROOT="${DATALIB_INSTALL_ROOT:-$HOME/.local/share/datalib}"
readonly BIN_DIR="${DATALIB_BIN_DIR:-$HOME/.local/bin}"
readonly RELEASE_DIR="$INSTALL_ROOT/$PINNED_VERSION"

# This script is .agents/skills/datalib/scripts/install_datalib.sh.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
readonly REPO_ROOT
readonly PROGRAM_SOURCE="$REPO_ROOT/system/apps/datalib/datalib.conf"
readonly PROGRAM_DIR="${DATALIB_PROGRAM_DIR:-$REPO_ROOT/system/supervisord.conf.d}"
readonly SUPERVISORCTL="${DATALIB_SUPERVISORCTL:-supervisorctl}"

_log() {
    printf '[install_datalib] %s\n' "$*"
}

_link_binaries() {
    local binary name
    mkdir -p "$BIN_DIR"
    for binary in "$RELEASE_DIR"/*; do
        [ -f "$binary" ] || continue
        name="$(basename "$binary")"
        ln -sfn "$binary" "$BIN_DIR/$name"
    done
}

if [ -x "$RELEASE_DIR/datalib-http" ]; then
    _log "datalib $PINNED_VERSION already installed at $RELEASE_DIR"
else
    # datalib's own installer (the fully-static musl build runs as-is on any
    # Linux). It downloads the release tarball for this architecture, verifies
    # the published sha256, and drops the binaries into DATALIB_INSTALL_DIR.
    _log "installing datalib $PINNED_VERSION into $RELEASE_DIR (about 80 MB)"
    mkdir -p "$RELEASE_DIR"
    curl -LsSf "https://raw.githubusercontent.com/imbue-ai/datalib/$PINNED_VERSION/scripts/install.sh" \
        | DATALIB_VERSION="$PINNED_VERSION" DATALIB_LIBC=musl DATALIB_INSTALL_DIR="$RELEASE_DIR" sh
    [ -x "$RELEASE_DIR/datalib-http" ] || {
        _log "the installer did not produce $RELEASE_DIR/datalib-http"
        exit 1
    }
fi
_link_binaries
_log "binaries linked into $BIN_DIR"

# datalib's guide for agents, and the two docs a config is written from, at
# the same tag as the binaries. Kept beside them so the skill reads a local
# copy instead of fetching github.com in every chat. They sit at the paths
# they have under the repo's docs/, so the links between them resolve. A miss
# is reported but not fatal: the skill falls back to the URL.
readonly DOCS_DIR="$RELEASE_DIR/docs"
readonly DOCS_URL="https://raw.githubusercontent.com/imbue-ai/datalib/$PINNED_VERSION/docs"
for doc in agent_user.md user/getting_your_data.md user/config_examples/all_sources.toml; do
    [ -s "$DOCS_DIR/$doc" ] && continue
    mkdir -p "$(dirname "$DOCS_DIR/$doc")"
    # Downloaded under another name first, so an interrupted fetch never
    # leaves a truncated guide that the check above would accept.
    if curl -LsSf "$DOCS_URL/$doc" -o "$DOCS_DIR/$doc.partial"; then
        mv "$DOCS_DIR/$doc.partial" "$DOCS_DIR/$doc"
    else
        rm -f "$DOCS_DIR/$doc.partial"
        _log "could not fetch docs/$doc; read it at $DOCS_URL/$doc instead"
    fi
done
_log "agent guide at $DOCS_DIR/agent_user.md"

# The tarball carries the binaries only. The Node runtime (latchkey and qmd,
# at the versions datalib was built with) is a separate asset of the same
# release, fetched sha256-checked into ~/.cache/datalib/runtime. A sync
# fetches it too if this fails, so a miss here is reported but not fatal.
_log "fetching the Node runtime (about 115 MB, once)"
if "$RELEASE_DIR/datalib-step" pull-runtime; then
    _log "runtime in place under ~/.cache/datalib/runtime"
else
    _log "runtime fetch FAILED; the first sync will try again"
fi

# The Datalib app's program. supervisord reads system/supervisord.conf.d/ only
# when told to, so `reread` picks up the new or changed file and `update`
# loads it, which starts it (it autostarts). `update` leaves a program it
# already has unchanged alone, stopped or not, so a stopped one is started
# here. Installed last: the program needs the binaries above.
readonly INSTALLED_PROGRAM="$PROGRAM_DIR/datalib.conf"
if ! cmp -s "$PROGRAM_SOURCE" "$INSTALLED_PROGRAM"; then
    # Copied under a name supervisord's *.conf glob does not match first.
    cp "$PROGRAM_SOURCE" "$INSTALLED_PROGRAM.partial"
    mv "$INSTALLED_PROGRAM.partial" "$INSTALLED_PROGRAM"
    _log "added the Datalib app's program at $INSTALLED_PROGRAM"
fi
"$SUPERVISORCTL" reread >/dev/null
"$SUPERVISORCTL" update datalib
program_state="$("$SUPERVISORCTL" status datalib | awk '{print $2}')" || true
case "$program_state" in
    RUNNING | STARTING) ;;
    *) "$SUPERVISORCTL" start datalib ;;
esac
_log "the Datalib app's program is running"
