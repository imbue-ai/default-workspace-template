import gzip
import shlex
import zlib
from pathlib import Path
from typing import Final

from imbue.imbue_common.pure import pure
from loguru import logger

from terminal_app.data_types import TerminalPaths
from terminal_app.errors import UnsafeDispatchPathError

# The dispatch scripts the ttyd dispatch snippet runs by URL key: ``?arg=_&arg=<key>&arg=...``
# runs ``<commands_dir>/<key>.sh`` with the remaining arguments.
AGENT_SCRIPT_FILENAME: Final[str] = "agent.sh"
WORKDIR_SCRIPT_FILENAME: Final[str] = "workdir.sh"
SESSION_SCRIPT_FILENAME: Final[str] = "session.sh"

_EXECUTABLE_MODE: Final[int] = 0o755

# The snippet ``ttyd -W bash -c`` runs for every connection. ttyd appends the URL's ``arg``
# values to the command, so the first one lands in ``$0`` (the frontend sends ``_`` for it) and
# the second, the dispatch key, is ``$1``.
_DISPATCH_SNIPPET_TEMPLATE: Final[str] = """
KEY="${1:-}"
if [ -z "$KEY" ]; then
  exec bash
fi
SCRIPT="{commands_dir}/$KEY.sh"
if [ -f "$SCRIPT" ]; then
  shift
  exec bash "$SCRIPT" "$@"
fi
echo "Unknown ttyd key: $KEY" >&2
read -r
exit 1
"""

# CLEANUP: agent.sh serves the chat UI's terminal back face (``?arg=_&arg=agent&arg=<name>``);
# move it to the chat app when phase 10 of the workspace app model gives chat its own package.
_AGENT_SCRIPT: Final[str] = """#!/bin/bash
# Attach to a mngr agent's tmux session window 0.
#
# If a session name is provided as $1, use "$MNGR_PREFIX$1" as the target
# session (so the minds chat UI can deep-link to a specific sub-agent's
# terminal by passing the agent name). Otherwise fall back to the current
# tmux session -- useful when ttyd is invoked without args.
set -euo pipefail
if [ $# -gt 0 ] && [ -n "$1" ]; then
    TARGET_SESSION="${MNGR_PREFIX:-mngr-}$1"
else
    TARGET_SESSION=$(tmux display-message -p '#{session_name}')
fi
unset TMUX
exec tmux attach -t "$TARGET_SESSION":0
"""

_WORKDIR_SCRIPT: Final[str] = """#!/bin/bash
cd "$1" 2>/dev/null && exec bash
"""

_SESSION_SCRIPT_TEMPLATE: Final[str] = """#!/bin/bash
# Attach to (or create) a named, in-memory tmux terminal session.
#
# Args (passed by the ttyd dispatch after the "session" key is consumed):
#   $1 = session name (e.g. "terminal-1")
#   $2 = tab id       (per-tab id used to map this ttyd client's pty back to
#                      the dockview tab for live tab-title tracking; may be "")
#   $3 = working directory to anchor a newly-created session in (may be "")
#
# `tmux new-session -A` attaches when the session exists and creates it
# otherwise, so this single path covers reattach (tab reopen / reload / ttyd
# restart) and first creation, as well as recreation after a container restart
# cleared the tmux server (the tab just comes back as a fresh shell).
set -euo pipefail
SESSION_NAME="${1:-}"
TAB_ID="${2:-}"
WORKDIR="${3:-}"
unset TMUX

if [ -z "$SESSION_NAME" ]; then
    exec bash
fi

# Record this connection's pty under the tab id so the tmux
# client-session-changed / session-renamed hooks can map a live client back
# to the dockview tab that owns it (best-effort; never fatal).
if [ -n "$TAB_ID" ]; then
    CLIENTS_DIR="{clients_dir}"
    mkdir -p "$CLIENTS_DIR"
    MY_TTY="$(tty 2>/dev/null || true)"
    if [ -n "$MY_TTY" ]; then
        # This pty now authoritatively belongs to this tab id. Drop any
        # stale mapping that still points at the same pty: Linux reuses a pty
        # number after a client disconnects, so a since-closed tab's leftover
        # file could otherwise shadow this one and misroute title updates to a
        # closed tab (the resolver returns the first matching entry).
        for existing in "$CLIENTS_DIR"/*; do
            [ -f "$existing" ] || continue
            if [ "$(cat "$existing" 2>/dev/null)" = "$MY_TTY" ]; then
                rm -f "$existing"
            fi
        done
        printf '%s\\n' "$MY_TTY" > "$CLIENTS_DIR/$TAB_ID" 2>/dev/null || true
    fi
fi

WORKDIR_ARGS=()
if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ]; then
    WORKDIR_ARGS=(-c "$WORKDIR")
fi

exec tmux new-session -A -s "$SESSION_NAME" "${WORKDIR_ARGS[@]}"
"""


@pure
def _shell_verbatim_path(path: Path) -> str:
    """The path as the scripts embed it, inside double quotes with no escaping; a path that would need quoting is refused."""
    text = str(path)
    if shlex.quote(text) != text:
        raise UnsafeDispatchPathError(
            f"cannot embed {text!r} in a dispatch script: it needs shell quoting"
        )
    return text


@pure
def render_dispatch_snippet(commands_dir: Path) -> str:
    """The bash ``ttyd -W bash -c`` runs per connection, dispatching on the URL key to ``<commands_dir>/<key>.sh``."""
    return _DISPATCH_SNIPPET_TEMPLATE.replace(
        "{commands_dir}", _shell_verbatim_path(commands_dir)
    )


@pure
def render_agent_script() -> str:
    return _AGENT_SCRIPT


@pure
def render_workdir_script() -> str:
    return _WORKDIR_SCRIPT


@pure
def render_session_script(clients_dir: Path) -> str:
    """The named-session dispatch script, recording each tab's pty under ``clients_dir``."""
    return _SESSION_SCRIPT_TEMPLATE.replace(
        "{clients_dir}", _shell_verbatim_path(clients_dir)
    )


def _write_executable(path: Path, contents: str) -> None:
    path.write_text(contents)
    path.chmod(_EXECUTABLE_MODE)


def install_dispatch_scripts(paths: TerminalPaths) -> None:
    """Write the three dispatch scripts into the commands directory.

    ``agent.sh`` and ``session.sh`` are rewritten on every start so an existing workspace picks
    up logic changes; ``workdir.sh`` is written only when absent, so a copy a workspace
    customised survives a restart.
    """
    paths.commands_dir.mkdir(parents=True, exist_ok=True)
    _write_executable(paths.commands_dir / AGENT_SCRIPT_FILENAME, render_agent_script())
    workdir_script = paths.commands_dir / WORKDIR_SCRIPT_FILENAME
    if not workdir_script.exists():
        _write_executable(workdir_script, render_workdir_script())
    _write_executable(
        paths.commands_dir / SESSION_SCRIPT_FILENAME,
        render_session_script(paths.clients_dir),
    )


def install_ttyd_web_client(compressed_client: Path, destination: Path) -> bool:
    """Decompress the vendored OSC 52-capable ttyd web client to ``destination``, reporting whether it is there to serve.

    The stock ttyd client drops the OSC 52 escapes tmux emits on copy, so the patched client
    vendored with the mngr_ttyd plugin is served instead; when the asset is missing or will
    not decompress, ttyd falls back to its stock client so the terminal still starts.
    """
    if not compressed_client.is_file():
        logger.warning(
            "Skipped installing the ttyd web client: {} is missing; using the stock client",
            compressed_client,
        )
        return False
    # gzip.decompress raises EOFError for a truncated stream and zlib.error for corrupt data; a
    # file that is not gzip at all is a BadGzipFile, which is an OSError.
    try:
        destination.write_bytes(gzip.decompress(compressed_client.read_bytes()))
    except (OSError, EOFError, zlib.error) as e:
        logger.warning(
            "Failed to decompress the ttyd web client at {}: {}; using the stock client",
            compressed_client,
            e,
        )
        destination.unlink(missing_ok=True)
        return False
    return True


@pure
def build_ttyd_argv(
    ttyd_executable: str, port: int, index_path: Path | None, commands_dir: Path
) -> list[str]:
    """Today's ttyd command line: URL-arg dispatch on, the leave alert off, the patched client when installed, writable."""
    argv = [ttyd_executable, "-p", str(port), "-a", "-t", "disableLeaveAlert=true"]
    if index_path is not None:
        argv.extend(["-I", str(index_path)])
    argv.extend(["-W", "bash", "-c", render_dispatch_snippet(commands_dir)])
    return argv
