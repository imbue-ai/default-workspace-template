import gzip
import stat
from pathlib import Path

import pytest
from inline_snapshot import snapshot

from terminal_app.data_types import TerminalPaths
from terminal_app.dispatch import (
    build_session_command,
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
_SESSIONS_DIR = Path("/home/user/workspace/data/.state/terminal/sessions")
_OOM_TAG_SCRIPT = Path("/home/user/workspace/system/services/oom_priority/bin/oom_tag_service.py")


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


def test_session_script_attaches_by_recorded_id_and_creates_a_tagged_shell() -> None:
    assert render_session_script(_COMMANDS_DIR / "clients", _SESSIONS_DIR, _OOM_TAG_SCRIPT) == snapshot("""\
#!/bin/bash
# Attach to (or create) a named, in-memory tmux terminal session.
#
# Args (passed by the ttyd dispatch after the "session" key is consumed):
#   $1 = session name (e.g. "terminal-1"), the terminal's key
#   $2 = tab id       (per-tab id used to map this ttyd client's pty back to
#                      the dockview tab for live tab-title tracking; may be "")
#   $3 = working directory to anchor a newly-created session in (may be "")
#
# The terminal app records the tmux session id and creation time of every
# terminal it created under the sessions directory, named by key; attaching by
# that id keeps the tab on its session even after someone renamed the session
# inside tmux. When there is no id (a record from before the app kept them) or
# the session is gone (a container restart cleared the tmux server, whose
# successor hands the same ids out again), `tmux new-session -A`
# attaches when a session of that name exists and creates it otherwise, so the
# tab comes back as a fresh shell. A created session runs the login shell
# through the memory-shedding tag, as the app's own creates do.
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

# The id file holds the session id and its creation time: tmux reuses ids across servers, so
# only a session created when the file says was the terminal's.
SESSION_ID_FILE="/home/user/workspace/data/.state/terminal/sessions/$SESSION_NAME"
if [ -f "$SESSION_ID_FILE" ]; then
    SESSION_ID="$(sed -n 1p "$SESSION_ID_FILE" 2>/dev/null || true)"
    SESSION_CREATED="$(sed -n 2p "$SESSION_ID_FILE" 2>/dev/null || true)"
    if [ -n "$SESSION_ID" ] && [ -n "$SESSION_CREATED" ] && tmux has-session -t "$SESSION_ID" 2>/dev/null; then
        LIVE_CREATED="$(tmux display-message -p -t "$SESSION_ID" '#{session_created}' 2>/dev/null || true)"
        if [ "$LIVE_CREATED" = "$SESSION_CREATED" ]; then
            exec tmux attach-session -t "$SESSION_ID"
        fi
    fi
fi

WORKDIR_ARGS=()
if [ -n "$WORKDIR" ] && [ -d "$WORKDIR" ]; then
    WORKDIR_ARGS=(-c "$WORKDIR")
fi

exec tmux new-session -A -s "$SESSION_NAME" "${WORKDIR_ARGS[@]}" python3 /home/user/workspace/system/services/oom_priority/bin/oom_tag_service.py terminal-session bash -l
""")


def test_a_directory_that_needs_shell_quoting_is_refused() -> None:
    with pytest.raises(UnsafeDispatchPathError, match="needs shell quoting"):
        render_dispatch_snippet(Path("/tmp/has space"))
    with pytest.raises(UnsafeDispatchPathError, match="needs shell quoting"):
        build_session_command(Path("/tmp/has space/oom_tag_service.py"))


def test_session_command_tags_the_login_shell_into_the_terminal_session_band() -> None:
    assert build_session_command(_OOM_TAG_SCRIPT) == [
        "python3",
        str(_OOM_TAG_SCRIPT),
        "terminal-session",
        "bash",
        "-l",
    ]


def test_install_writes_executable_scripts_and_keeps_an_existing_workdir_script(
    terminal_paths: TerminalPaths,
) -> None:
    install_dispatch_scripts(terminal_paths, _OOM_TAG_SCRIPT)
    (terminal_paths.commands_dir / "workdir.sh").write_text(
        "#!/bin/bash\n# customised\n"
    )
    (terminal_paths.commands_dir / "agent.sh").write_text("stale")

    install_dispatch_scripts(terminal_paths, _OOM_TAG_SCRIPT)

    scripts = {path.name: path for path in terminal_paths.commands_dir.iterdir()}
    assert sorted(scripts) == ["agent.sh", "session.sh", "workdir.sh"]
    assert scripts["agent.sh"].read_text() == render_agent_script()
    assert scripts["session.sh"].read_text() == render_session_script(
        terminal_paths.clients_dir, terminal_paths.sessions_dir, _OOM_TAG_SCRIPT
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
    good = gzip.compress(b"<html>patched client</html>" * 100)
    broken_archives = {
        "not-gzip.gz": b"not gzip at all",
        "truncated.gz": good[: len(good) // 2],
        "corrupt.gz": good[:20] + b"xx" + good[22:],
    }

    assert install_ttyd_web_client(tmp_path / "absent.gz", destination) is False
    assert not destination.exists()

    for name, contents in broken_archives.items():
        broken = tmp_path / name
        broken.write_bytes(contents)
        assert install_ttyd_web_client(broken, destination) is False, name
        assert not destination.exists(), name


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
