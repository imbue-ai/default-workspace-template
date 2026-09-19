import json
from pathlib import Path

import pytest
from app_instances.testing import RecordingNudger
from flask import Flask
from flask.testing import FlaskClient

from terminal_app.pages import PageConfig, SessionPage, build_pages_blueprint, render_page
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import FakeTmux, make_terminal_record, make_tmux_session


def _registry(tmp_path: Path, *rows: tuple[str, str]) -> Path:
    path = tmp_path / "apps.toml"
    path.write_text(
        "".join(
            f'[[apps]]\nname = "{name}"\nurl = "http://localhost:1"\nlabel = "{label}"\n\n' for name, label in rows
        )
    )
    return path


@pytest.fixture
def pages_client(
    session_source: TmuxSessionSource, recording_nudger: RecordingNudger, tmp_path: Path
) -> FlaskClient:
    registry_path = _registry(tmp_path, ("system_interface", "system_interface-a1b2"), ("terminal-pty", "terminal-pty-c3d4"))
    app = Flask(__name__, static_folder=None)
    app.register_blueprint(build_pages_blueprint(source=session_source, nudger=recording_nudger, registry_path=registry_path))
    return app.test_client()


def _config_of(page_html: str) -> dict[str, object]:
    start = page_html.index('id="terminal-config">') + len('id="terminal-config">')
    end = page_html.index("</script>", start)
    return json.loads(page_html[start:end])


def test_the_wrapper_page_frames_the_session_with_both_origin_labels(
    pages_client: FlaskClient, fake_tmux: FakeTmux, session_store: JsonTerminalSessionStore
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-2", "$5")])
    session_store.save_record(make_terminal_record("terminal-2", "Build", "/srv", session_id="$5"))

    response = pages_client.get("/?session=terminal-2&tab=tab-0123")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "<title>Build</title>" in response.text
    assert _config_of(response.text) == {
        "session": "terminal-2",
        "tab": "tab-0123",
        "shell_label": "system_interface-a1b2",
        "page": {
            "name": "terminal-2",
            "title": "Build",
            "pty_path": "/?arg=_&arg=session&arg=terminal-2&arg=tab-0123&arg=%2Fsrv",
            "pty_label": "terminal-pty-c3d4",
        },
    }


def test_the_bare_root_carries_no_session(pages_client: FlaskClient) -> None:
    response = pages_client.get("/")

    assert response.status_code == 200
    config = _config_of(response.text)
    assert config["session"] is None
    assert config["page"] is None
    assert "<title>Terminal</title>" in response.text


def test_a_name_that_cannot_be_a_session_is_not_found(pages_client: FlaskClient) -> None:
    response = pages_client.get("/?session=not%20a%20name")

    assert response.status_code == 404
    assert "no terminal has the name" in response.json["detail"]


def test_new_allocates_a_terminal_and_redirects_to_its_page(
    pages_client: FlaskClient, fake_tmux: FakeTmux, recording_nudger: RecordingNudger
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$1")])

    response = pages_client.get("/new?workdir=/srv")

    assert response.status_code == 302
    assert response.headers["Location"] == "/?session=terminal-2"
    assert fake_tmux.session_names() == ["terminal-1", "terminal-2"]
    assert fake_tmux.creates()[0][:6] == ["new-session", "-d", "-s", "terminal-2", "-c", "/srv"]
    assert recording_nudger.nudge_count == 1


def test_new_refuses_a_bad_workdir_with_the_instances_api_detail(pages_client: FlaskClient) -> None:
    response = pages_client.get("/new?workdir=" + "x" * 2000)

    assert response.status_code == 400
    assert "workdir" in response.json["detail"]


def test_the_session_api_answers_what_the_page_refreshes_from(
    pages_client: FlaskClient, fake_tmux: FakeTmux
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-3", "$3")])

    response = pages_client.get("/api/sessions/terminal-3?tab=tab-9")

    assert response.status_code == 200
    assert response.json == {
        "name": "terminal-3",
        "title": "Terminal 3",
        "pty_path": "/?arg=_&arg=session&arg=terminal-3&arg=tab-9",
        "pty_label": "terminal-pty-c3d4",
    }


def test_health_answers(pages_client: FlaskClient) -> None:
    assert pages_client.get("/api/health").json == {"status": "ok"}


def test_render_page_keeps_a_script_closer_out_of_the_config() -> None:
    page = SessionPage(
        name="terminal-1", title="Terminal 1", pty_path="/?arg=_&arg=session&arg=terminal-1&arg=", pty_label="</script>"
    )
    html = render_page(PageConfig(session="terminal-1", tab="", shell_label="", page=page))

    start = html.index('id="terminal-config">') + len('id="terminal-config">')
    raw_config = html[start : html.index("</script>", start)]
    assert "</script" not in raw_config
    assert json.loads(raw_config)["page"]["pty_label"] == "</script>"
