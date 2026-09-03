from pathlib import Path

import pytest
from app_instances.testing import RecordedShellRequests, RecordingNudger
from app_manifest.primitives import AppName
from flask import Flask
from flask.testing import FlaskClient

from terminal_app.data_types import TerminalPaths, TmuxClient
from terminal_app.hooks import (
    HttpShellPoster,
    build_tmux_hook_blueprint,
    resolve_tab_id_for_tty,
)
from terminal_app.primitives import ClientTty
from terminal_app.testing import FakeTmux
from terminal_app.tmux import SubprocessTmux


def _record_tab(paths: TerminalPaths, tab_id: str, tty: str) -> None:
    paths.clients_dir.mkdir(parents=True, exist_ok=True)
    (paths.clients_dir / tab_id).write_text(f"{tty}\n")


def test_resolve_tab_id_finds_the_file_holding_the_pty(
    terminal_paths: TerminalPaths,
) -> None:
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")
    _record_tab(terminal_paths, "term-b", "/dev/pts/4")
    (terminal_paths.clients_dir / "bad name").write_text("/dev/pts/5\n")

    clients_dir = terminal_paths.clients_dir
    assert resolve_tab_id_for_tty(clients_dir, ClientTty("/dev/pts/4")) == "term-b"
    assert resolve_tab_id_for_tty(clients_dir, ClientTty("/dev/pts/5")) is None
    assert resolve_tab_id_for_tty(clients_dir, ClientTty("/dev/pts/9")) is None
    assert (
        resolve_tab_id_for_tty(Path("/nonexistent/clients"), ClientTty("/dev/pts/4"))
        is None
    )


def test_session_changed_repoints_the_tab_forwards_with_the_resolved_id_and_nudges(
    hook_client: FlaskClient,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")

    response = hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/3",
            "session_name": "build",
            "session_id": "$4",
        },
    )

    assert response.status_code == 204
    assert [
        (received.method, received.path, received.body)
        for received in recording_shell.requests
    ] == [
        ("POST", "/api/tabs/term-a/instance", {"app": "terminal", "key": "build"}),
        (
            "POST",
            "/api/terminals/notify",
            {
                "kind": "session-changed",
                "client_tty": "/dev/pts/3",
                "session_name": "build",
                "session_id": "$4",
                "terminal_id": "term-a",
            },
        ),
    ]
    assert recording_nudger.nudge_count == 1


@pytest.mark.parametrize("client_tty", ["/dev/pts/9", ""])
def test_session_changed_from_a_pty_no_tab_recorded_or_from_no_pty_only_nudges(
    hook_client: FlaskClient,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
    client_tty: str,
) -> None:
    response = hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": client_tty,
            "session_name": "build",
            "session_id": "$4",
        },
    )

    assert response.status_code == 204
    assert recording_shell.requests == []
    # The switch may still be the attach that created the session, so the list is refetched.
    assert recording_nudger.nudge_count == 1


def test_session_changed_to_a_session_that_cannot_be_a_key_only_forwards(
    hook_client: FlaskClient,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
) -> None:
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")

    hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/3",
            "session_name": "hand made",
            "session_id": "$4",
        },
    )

    assert recording_shell.paths() == [("POST", "/api/terminals/notify")]


def test_session_renamed_repoints_every_attached_tab_forwards_once_and_nudges(
    hook_client: FlaskClient,
    fake_tmux: FakeTmux,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")
    _record_tab(terminal_paths, "term-b", "/dev/pts/4")
    _record_tab(terminal_paths, "term-c", "/dev/pts/5")
    fake_tmux.set_clients(
        [
            TmuxClient(client_tty="/dev/pts/3", session_name="deploy", session_id="$4"),
            TmuxClient(client_tty="/dev/pts/4", session_name="deploy", session_id="$4"),
            TmuxClient(
                client_tty="/dev/pts/5", session_name="terminal-2", session_id="$7"
            ),
            TmuxClient(client_tty="/dev/pts/6", session_name="deploy", session_id="$4"),
        ]
    )

    response = hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-renamed",
            "client_tty": "",
            "session_name": "deploy",
            "session_id": "$4",
        },
    )

    assert response.status_code == 204
    assert recording_shell.paths() == [
        ("POST", "/api/tabs/term-a/instance"),
        ("POST", "/api/tabs/term-b/instance"),
        ("POST", "/api/terminals/notify"),
    ]
    assert recording_shell.requests[0].body == {"app": "terminal", "key": "deploy"}
    assert recording_shell.requests[2].body == {
        "kind": "session-renamed",
        "client_tty": "",
        "session_name": "deploy",
        "session_id": "$4",
        "terminal_id": None,
    }
    assert recording_nudger.nudge_count == 1


def test_hook_rejects_non_loopback_callers_and_malformed_bodies(
    hook_client: FlaskClient,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
    forbidden = hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-renamed",
            "client_tty": "",
            "session_name": "x",
            "session_id": "$1",
        },
        environ_base={"REMOTE_ADDR": "10.0.0.7"},
    )
    assert forbidden.status_code == 403

    not_an_object = hook_client.post(
        "/tmux-hook", data="[]", content_type="application/json"
    )
    assert not_an_object.status_code == 400
    assert not_an_object.get_json() == {
        "detail": "the request body must be a JSON object"
    }

    wrong_shape = hook_client.post("/tmux-hook", json={"kind": "bogus"})
    assert wrong_shape.status_code == 400
    assert "kind" in wrong_shape.get_json()["detail"]

    assert recording_shell.requests == []
    assert recording_nudger.nudge_count == 0


def test_a_tmux_failure_on_the_hook_route_answers_500_with_a_detail_body(
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(
        build_tmux_hook_blueprint(
            tmux=SubprocessTmux(tmux_executable="/nonexistent/tmux-binary"),
            paths=terminal_paths,
            shell=HttpShellPoster(shell_url=recording_shell.base_url),
            nudger=recording_nudger,
            app_name=AppName("terminal"),
        )
    )

    response = app.test_client().post(
        "/tmux-hook",
        json={
            "kind": "session-renamed",
            "client_tty": "",
            "session_name": "deploy",
            "session_id": "$4",
        },
    )

    assert response.status_code == 500
    assert "cannot run /nonexistent/tmux-binary" in response.get_json()["detail"]
    assert recording_shell.requests == []
    assert recording_nudger.nudge_count == 0
