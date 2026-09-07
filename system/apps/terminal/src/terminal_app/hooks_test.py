from pathlib import Path

import pytest
from app_instances.testing import RecordedShellRequests, RecordingNudger
from flask.testing import FlaskClient

from terminal_app.data_types import TerminalPaths, TmuxSession
from terminal_app.hooks import resolve_tab_id_for_tty
from terminal_app.primitives import ClientTty
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import FakeTmux, make_terminal_record


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


def test_session_changed_repoints_the_tab_and_nudges(
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


def test_session_changed_to_a_session_that_cannot_be_a_key_only_nudges(
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

    assert recording_shell.paths() == []


def test_session_changed_keys_the_tab_by_the_terminal_whose_session_id_it_is(
    hook_client: FlaskClient,
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
) -> None:
    # The session was renamed inside tmux; the tab still shows terminal-1, the record's key.
    fake_tmux.set_sessions([TmuxSession(name="my-build", session_id="$4", last_activity=None)])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title=None, workdir=None, session_id="$4")
    )
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")

    hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/3",
            "session_name": "my-build",
            "session_id": "$4",
        },
    )

    assert recording_shell.requests[0].body == {"app": "terminal", "key": "terminal-1"}


def test_session_changed_adopts_a_session_recreated_on_attach(
    hook_client: FlaskClient,
    fake_tmux: FakeTmux,
    session_store: JsonTerminalSessionStore,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
) -> None:
    # The tab of a stopped terminal was opened: the dispatch created the session by name, and
    # the switch is how the app learns its id.
    fake_tmux.set_sessions([TmuxSession(name="terminal-1", session_id="$9", last_activity=None)])
    session_store.save_record(
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", is_stopped=True)
    )
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")

    hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/3",
            "session_name": "terminal-1",
            "session_id": "$9",
        },
    )

    assert recording_shell.requests[0].body == {"app": "terminal", "key": "terminal-1"}
    assert session_store.list_records() == [
        make_terminal_record(name="terminal-1", title="Build", workdir="/srv", session_id="$9")
    ]
    assert (terminal_paths.sessions_dir / "terminal-1").read_text() == "$9\n"


def test_session_renamed_changes_no_tab_and_only_nudges(
    hook_client: FlaskClient,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
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
    assert recording_shell.requests == []
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


def test_a_store_failure_on_the_hook_route_answers_500_with_a_detail_body(
    hook_client: FlaskClient,
    session_store: JsonTerminalSessionStore,
    terminal_paths: TerminalPaths,
    recording_shell: RecordedShellRequests,
    recording_nudger: RecordingNudger,
) -> None:
    session_store.store_path.parent.mkdir(parents=True)
    session_store.store_path.write_text("not json")
    _record_tab(terminal_paths, "term-a", "/dev/pts/3")

    response = hook_client.post(
        "/tmux-hook",
        json={
            "kind": "session-changed",
            "client_tty": "/dev/pts/3",
            "session_name": "deploy",
            "session_id": "$4",
        },
    )

    assert response.status_code == 500
    assert "is not valid JSON" in response.get_json()["detail"]
    assert recording_shell.requests == []
    assert recording_nudger.nudge_count == 0
