import gzip
import stat
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from terminal_app.data_types import TerminalPaths
from terminal_app.dispatch import (
    build_ttyd_argv,
    install_dispatch_scripts,
    install_ttyd_web_client,
    render_agent_script,
    render_dispatch_snippet,
    render_session_script,
    render_workdir_script,
)
from terminal_app.errors import UnsafeDispatchPathError

_COMMANDS_DIR = Path("/home/user/workspace/data/.state/terminal/commands")


def test_dispatch_snippet_is_todays_apart_from_the_commands_directory() -> None:
    assert render_dispatch_snippet(_COMMANDS_DIR) == snapshot("""\

KEY="${1:-}"
if [ -z "$KEY" ]; then
  exec bash
fi
SCRIPT="/home/user/workspace/data/.state/terminal/commands/$KEY.sh"
if [ -f "$SCRIPT" ]; then
  shift
  exec bash "$SCRIPT" "$@"
fi
echo "Unknown ttyd key: $KEY" >&2
read -r
exit 1
""")


def test_agent_script_is_todays_verbatim() -> None:
    assert render_agent_script() == snapshot("""\
#!/bin/bash
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
""")


def test_workdir_script_is_todays_verbatim() -> None:
    assert render_workdir_script() == snapshot("""\
#!/bin/bash
cd "$1" 2>/dev/null && exec bash
""")


def test_session_script_is_todays_apart_from_the_clients_directory_and_the_tab_argument() -> (
    None
):
    assert render_session_script(_COMMANDS_DIR / "clients") == snapshot("""\
#!/bin/bash
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
    CLIENTS_DIR="/home/user/workspace/data/.state/terminal/commands/clients"
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
""")


def test_a_directory_that_needs_shell_quoting_is_refused() -> None:
    with pytest.raises(UnsafeDispatchPathError, match="needs shell quoting"):
        render_dispatch_snippet(Path("/tmp/has space"))


def test_install_writes_executable_scripts_and_keeps_an_existing_workdir_script(
    terminal_paths: TerminalPaths,
) -> None:
    install_dispatch_scripts(terminal_paths)
    (terminal_paths.commands_dir / "workdir.sh").write_text(
        "#!/bin/bash\n# customised\n"
    )
    (terminal_paths.commands_dir / "agent.sh").write_text("stale")

    install_dispatch_scripts(terminal_paths)

    scripts = {path.name: path for path in terminal_paths.commands_dir.iterdir()}
    assert sorted(scripts) == ["agent.sh", "session.sh", "workdir.sh"]
    assert scripts["agent.sh"].read_text() == render_agent_script()
    assert scripts["session.sh"].read_text() == render_session_script(
        terminal_paths.clients_dir
    )
    assert scripts["workdir.sh"].read_text() == "#!/bin/bash\n# customised\n"
    for script in scripts.values():
        assert script.stat().st_mode & stat.S_IXUSR


def test_install_ttyd_web_client_decompresses_the_vendored_client(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "ttyd_index.html.gz"
    archive.write_bytes(gzip.compress(b"<html>patched client</html>"))
    destination = tmp_path / "commands" / "index.html"
    destination.parent.mkdir()

    assert install_ttyd_web_client(archive, destination) is True
    assert destination.read_bytes() == b"<html>patched client</html>"


def test_install_ttyd_web_client_falls_back_when_the_asset_is_missing_or_broken(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "index.html"

    assert install_ttyd_web_client(tmp_path / "absent.gz", destination) is False
    assert not destination.exists()

    broken = tmp_path / "broken.gz"
    broken.write_bytes(b"not gzip at all")
    assert install_ttyd_web_client(broken, destination) is False
    assert not destination.exists()


def test_ttyd_argv_is_todays_command_line() -> None:
    argv = build_ttyd_argv("ttyd", 7681, _COMMANDS_DIR / "index.html", _COMMANDS_DIR)

    assert argv[:9] == [
        "ttyd",
        "-p",
        "7681",
        "-a",
        "-t",
        "disableLeaveAlert=true",
        "-I",
        "/home/user/workspace/data/.state/terminal/commands/index.html",
        "-W",
    ]
    assert argv[9:] == ["bash", "-c", render_dispatch_snippet(_COMMANDS_DIR)]
    assert "-I" not in build_ttyd_argv("ttyd", 7681, None, _COMMANDS_DIR)
