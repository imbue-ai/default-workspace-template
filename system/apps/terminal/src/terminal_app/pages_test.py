import json
from pathlib import Path

from flask.testing import FlaskClient

from terminal_app.pages import (
    PageConfig,
    SessionPage,
    render_page,
)
from terminal_app.sessions import TmuxSessionSource
from terminal_app.store import JsonTerminalSessionStore
from terminal_app.testing import (
    TEST_APP_CONTRACT_SOURCE,
    TEST_PTY_LABEL,
    FakeTmux,
    build_pages_test_client,
    make_terminal_record,
    make_tmux_session,
    write_registry_labels,
)


def _config_of(page_html: str) -> dict[str, object]:
    start = page_html.index('id="terminal-config">') + len('id="terminal-config">')
    end = page_html.index("</script>", start)
    return json.loads(page_html[start:end])


def test_the_wrapper_page_frames_the_session_with_the_ptys_origin_label(
    pages_client: FlaskClient, fake_tmux: FakeTmux, session_store: JsonTerminalSessionStore
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-2", "$5")])
    session_store.save_record(make_terminal_record("terminal-2", "Build", "/srv", session_id="$5"))

    response = pages_client.get("/?session=terminal-2")

    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "<title>Build</title>" in response.text
    assert _config_of(response.text) == {
        "session": "terminal-2",
        "page": {
            "name": "terminal-2",
            "title": "Build",
            "pty_path": "/?arg=_&arg=session&arg=terminal-2&arg=%2Fsrv",
            "pty_label": TEST_PTY_LABEL,
        },
    }


def test_the_bare_root_carries_no_session(pages_client: FlaskClient) -> None:
    response = pages_client.get("/")

    assert response.status_code == 200
    config = _config_of(response.text)
    assert config["session"] is None
    assert config["page"] is None
    assert "<title>Terminal</title>" in response.text


def test_an_unregistered_pty_leaves_the_label_empty(
    session_source: TmuxSessionSource, fake_tmux: FakeTmux, tmp_path: Path
) -> None:
    registry_path = write_registry_labels(tmp_path / "apps.toml", {})
    client = build_pages_test_client(session_source, registry_path, tmp_path / "app_contract.js")
    fake_tmux.set_sessions([make_tmux_session("terminal-3", "$3")])

    page = client.get("/?session=terminal-3")
    session = client.get("/api/sessions/terminal-3")

    assert page.status_code == 200
    config = _config_of(page.text)
    assert config["page"] == {
        "name": "terminal-3",
        "title": "Terminal 3",
        "pty_path": "/?arg=_&arg=session&arg=terminal-3",
        "pty_label": "",
    }
    assert session.json == config["page"]


def test_a_name_that_cannot_be_a_session_is_not_found(pages_client: FlaskClient) -> None:
    response = pages_client.get("/?session=not%20a%20name")

    assert response.status_code == 404
    assert "no terminal has the name" in response.json["detail"]


def test_new_allocates_a_terminal_and_redirects_to_its_page(pages_client: FlaskClient, fake_tmux: FakeTmux) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-1", "$1")])

    response = pages_client.get("/new?workdir=/srv")

    assert response.status_code == 302
    assert response.headers["Location"] == "/?session=terminal-2"
    assert fake_tmux.session_names() == ["terminal-1", "terminal-2"]
    assert fake_tmux.creates()[0][:6] == ["new-session", "-d", "-s", "terminal-2", "-c", "/srv"]


def test_new_refuses_a_bad_workdir_with_a_detail_body(pages_client: FlaskClient) -> None:
    response = pages_client.get("/new?workdir=" + "x" * 2000)

    assert response.status_code == 400
    assert "workdir" in response.json["detail"]


def test_new_answers_a_detail_body_when_tmux_refuses(pages_client: FlaskClient, fake_tmux: FakeTmux) -> None:
    fake_tmux.refuse_creates()

    response = pages_client.get("/new")

    assert response.status_code == 500
    assert "could not create session" in response.json["detail"]


def test_the_session_api_answers_what_the_page_refreshes_from(
    pages_client: FlaskClient, fake_tmux: FakeTmux
) -> None:
    fake_tmux.set_sessions([make_tmux_session("terminal-3", "$3")])

    response = pages_client.get("/api/sessions/terminal-3")

    assert response.status_code == 200
    assert response.json == {
        "name": "terminal-3",
        "title": "Terminal 3",
        "pty_path": "/?arg=_&arg=session&arg=terminal-3",
        "pty_label": TEST_PTY_LABEL,
    }


def test_health_answers(pages_client: FlaskClient) -> None:
    assert pages_client.get("/api/health").json == {"status": "ok"}


def test_the_app_contract_module_is_served_from_the_terminals_own_origin(pages_client: FlaskClient) -> None:
    response = pages_client.get("/_static/app_contract.js")

    assert response.status_code == 200
    assert response.mimetype == "text/javascript"
    assert response.text == TEST_APP_CONTRACT_SOURCE
    # The page imports it by that same-origin path, never from the shell's origin.
    assert 'import("/_static/app_contract.js")' in pages_client.get("/").text


def test_a_missing_app_contract_module_says_the_shell_is_not_built(
    session_source: TmuxSessionSource, tmp_path: Path
) -> None:
    client = build_pages_test_client(session_source, tmp_path / "apps.toml", tmp_path / "missing" / "app_contract.js")

    response = client.get("/_static/app_contract.js")

    assert response.status_code == 404
    assert "not built" in response.json["detail"]


def test_render_page_keeps_a_script_closer_out_of_the_config_and_escapes_the_title() -> None:
    page = SessionPage(
        name="terminal-1", title="R&D <tests>", pty_path="/?arg=_&arg=session&arg=terminal-1", pty_label="</script>"
    )
    page_html = render_page(PageConfig(session="terminal-1", page=page))

    assert "<title>R&amp;D &lt;tests&gt;</title>" in page_html
    start = page_html.index('id="terminal-config">') + len('id="terminal-config">')
    raw_config = page_html[start : page_html.index("</script>", start)]
    assert "</script" not in raw_config
    assert json.loads(raw_config)["page"]["pty_label"] == "</script>"
