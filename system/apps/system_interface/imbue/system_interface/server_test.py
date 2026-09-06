"""Tests for the Flask server."""

import html
import json
import re
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from imbue.system_interface.app_context import state_of
from imbue.system_interface.config import Config
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.server import _NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.server import _NOT_BUILT_REPAIR_COMMAND
from imbue.system_interface.server import _NOT_BUILT_REPAIR_MNGR_COMMAND
from imbue.system_interface.server import _handle_client_state_message
from imbue.system_interface.server import create_application
from imbue.system_interface.server import render_frontend_not_built_page
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app

# Generous: the first receive occasionally exceeded the previous 5.0s cap on a
# loaded machine (~1-in-8 locally, failing as ``json.loads(None)``) even though
# passing runs complete in well under a second -- the wait is pure scheduling
# delay, so a bigger cap costs nothing when healthy.
_WS_RECEIVE_TIMEOUT = 15.0


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def app(config: Config) -> Flask:
    return create_application(build_test_state(config=config))


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    return app.test_client()


def test_index_returns_html_when_static_exists(client: FlaskClient, tmp_path: Path) -> None:
    """When the static dir has index.html, the server serves it."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")
    assert response.status_code == 200
    assert "test" in response.text
    # Both the app and the placeholder are HTTP 200 HTML, so the header is
    # the only thing that distinguishes them to a health check.
    assert response.headers[FRONTEND_BUILT_HEADER] == "true"


def test_index_is_served_uncacheable(client: FlaskClient, tmp_path: Path) -> None:
    """The shell must never be cached, or a reload cannot pick up a new build.

    The built assets are content-hashed, so the shell is the only document whose
    freshness decides which bundle a reloaded page runs. A page cannot drop its
    own HTTP cache (``location.reload(true)`` is Firefox-only), so a cacheable
    shell would let a reveal's reload land right back on the old interface --
    including through a shared Cloudflare tunnel, where an intermediary may
    cache anything not marked otherwise.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>test</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"


def test_an_unknown_path_falls_through_to_the_shell_document(tmp_path: Path) -> None:
    """A path the shell does not serve is a client-side route: it answers the shell document, not a 404."""
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<html><body>the shell</body></html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/some/client/route")

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert "the shell" in response.text


def test_index_marks_the_not_built_placeholder_as_not_the_app(tmp_path: Path) -> None:
    """The placeholder and the real app are both HTTP 200 HTML.

    Only the header tells them apart, and the reveal flow's frontend probe
    decides whether to roll back on it -- a placeholder that claimed to be the
    app would let a reveal sign off on a UI the user cannot see.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "false"
    # The page keeps asking whether the bundle is back, which is the only thing
    # that returns an open tab to the interface once something else restores it
    # -- nothing on the page can produce one, and nothing notifies it.
    assert FRONTEND_BUILT_HEADER in response.text


def test_not_built_placeholder_polls_rather_than_refreshing_the_whole_page(tmp_path: Path) -> None:
    """The reader's terminal must survive the wait for a bundle.

    Returning to the interface unattended and hosting a live shell pull against
    each other: a whole-page refresh on a timer would tear down the terminal
    session every few seconds, right while it is being typed into. So the
    scripted page asks for the app-shell marker and reloads only once it says
    the bundle is back. A page-level ``http-equiv="refresh"`` may therefore
    appear only inside ``<noscript>``, where there is no terminal to protect.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    scriptless_only = re.sub(r"<noscript>.*?</noscript>", "", response.text, flags=re.DOTALL)
    assert 'http-equiv="refresh"' not in scriptless_only
    assert 'http-equiv="refresh"' in response.text
    # HEAD, because the marker is a header: the poll must not pull the page's
    # own body down every tick for the lifetime of the outage.
    assert '"HEAD"' in response.text


def test_not_built_placeholder_offers_the_registered_terminal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The way out of a missing interface is a shell, and the page has to name it.

    The terminal's origin label is minted per workspace, so the page cannot
    carry it -- it is read from the app registry at render time and handed to
    the script, which derives the origin from the browser's own location. If
    the label never reaches the page there is no frame to open, and the reader
    is back to prose about a repair they cannot perform here.
    """
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text('[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    # What ``ShellState.start`` does for the served app: read the registry once.
    state.shell.inventory.reload_registry()
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert '"terminal-x7k9q2w1"' in response.text
    assert 'id="terminal"' in response.text


def test_not_built_placeholder_renders_without_a_terminal_to_offer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace with no registered terminal still gets a usable page.

    ttyd registers itself alongside the other services rather than before them,
    so the placeholder can be served in the window where there is nothing to
    offer -- and this page exists precisely for states where things are missing.
    It must degrade to the prose rather than fail to render or show an empty
    frame pointed at nowhere.
    """
    apps_file = tmp_path / "apps.toml"
    apps_file.write_text('[[apps]]\nname = "browser"\nurl = "http://localhost:8081"\nlabel = "browser-aaaa1111"\n')
    monkeypatch.setenv("MINDS_APPS_FILE", str(apps_file))
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/")

    assert response.status_code == 200
    assert response.headers[FRONTEND_BUILT_HEADER] == "false"
    # The empty label is what the script reads as "no terminal", so the frame
    # stays hidden instead of loading a made-up origin.
    assert 'var terminalLabel = "";' in response.text
    assert "needs to be rebuilt" in response.text

    # The shell prefix is not part of the argv the CLI validates, but it is what
    # makes the connect half work from the workspace's own tmux-backed terminals.
    assert _NOT_BUILT_REPAIR_COMMAND == "env -u TMUX " + _NOT_BUILT_REPAIR_MNGR_COMMAND


def test_not_built_repair_message_quotes_the_heading_the_reader_is_looking_at() -> None:
    """What the message quotes has to be what the page says, or it quotes nothing.

    The message's whole claim on the agent's attention is that it repeats the
    line the reader is looking at, so the two are one statement written twice.
    Nothing else notices when they part: reword the heading and the message still
    parses, still validates against the CLI, and still reads as a quotation --
    of a sentence that now appears nowhere. The comparison is case-insensitive
    because the message is in the reader's voice and the heading is a title.
    """
    message = _NOT_BUILT_REPAIR_ARGV[_NOT_BUILT_REPAIR_ARGV.index("--message") + 1]
    quoted = re.search(r'"(.*?)[,.?!]?"', message)
    assert quoted is not None, f"the message no longer quotes anything: {message}"

    heading = re.search(r"<h1>(.*?)</h1>", render_frontend_not_built_page(None), re.DOTALL)
    assert heading is not None, "the page no longer carries a heading"
    assert quoted.group(1).lower().startswith(heading.group(1).strip().lower())


def _repair_line_shown_on(page: str) -> str:
    """The repair line as the page's own markup hands it to the reader.

    Undoing the escaping is what the browser does to fill ``textContent``, which
    is both what a reader sees in the block and what the copy button puts on the
    clipboard, so this is the line the page actually offers.
    """
    shown = re.search(r'<pre id="repair-command">(.*?)</pre>', page, re.DOTALL)
    assert shown is not None, "the page no longer carries a repair-command block"
    return html.unescape(shown.group(1))


def test_not_built_repair_command_reaches_the_page_as_text_not_markup() -> None:
    """The suggested line is prose, so the page has to render it as written.

    It carries a ``--message`` a maintainer will reword, and a browser reads an
    ``&`` in it as the start of an entity reference and a ``<`` as the start of
    a tag. Either would show a line other than the one the tests validated, and
    the copy button reads ``textContent``, so it would put that other line on
    the reader's clipboard.
    """
    with patch("imbue.system_interface.server._NOT_BUILT_REPAIR_COMMAND", 'mngr create --message "a & b <c>"'):
        page = render_frontend_not_built_page(None)

    assert 'mngr create --message "a &amp; b &lt;c&gt;"' in page
    assert "<c>" not in page

    # And the escaping has to be transparent to the line that ships: undoing it
    # is what the browser does to fill ``textContent``, so this is the line the
    # reader reads and copies, and it has to be the one the CLI check and the
    # shell split validated. The assertions above only show that escaping
    # happens; this is what says the real command survives it.
    shown = _repair_line_shown_on(render_frontend_not_built_page(None))
    assert shown == _NOT_BUILT_REPAIR_COMMAND


def test_not_built_repair_line_splits_the_way_a_shell_splits_it() -> None:
    """The argv the CLI validates has to be the argv the reader's shell builds.

    The readable line is the source of truth and the argv is parsed back out of
    it, which is only sound while the parse agrees with a shell's. ``shlex.split``
    quotes and splits but expands nothing, so a ``$`` or a backtick worded into
    the message -- prose, and prose gets reworded -- would reach the argv as
    itself, leaving the sentence assertion and the live-CLI check above both
    green while the line a reader copies tells the agent something else.

    So the split is checked against a real shell rather than assumed to match
    one. ``set --`` keeps the flags from being read as options to ``set`` and
    keeps the line's first word from being run as a command -- but the words are
    still expanded on the way in, which is how a ``$`` is caught here. Command
    substitution is an expansion too, and that one would be *run* rather than
    reported, so it is refused before a shell ever sees the line.
    """
    for substitution in ("`", "$("):
        assert substitution not in _NOT_BUILT_REPAIR_MNGR_COMMAND, (
            f"the suggested line contains a command substitution ({substitution}), which the shell below "
            "would execute rather than report: word it out of the message"
        )

    printed_words = subprocess.run(
        ["sh", "-c", f'set -- {_NOT_BUILT_REPAIR_MNGR_COMMAND}\nprintf "%s\\n" "$@"'],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert printed_words.stdout.splitlines() == list(_NOT_BUILT_REPAIR_ARGV)


def test_assets_404_rather_than_falling_through_to_the_spa_shell(tmp_path: Path) -> None:
    """A missing asset must 404, never come back as the SPA shell.

    The catch-all would answer with index.html as text/html, which the browser
    refuses as a module script -- a blank screen with no hint of the cause.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    response = test_client.get("/assets/index-abc123.js")

    assert response.status_code == 404
    # The app-shell marker is absent, proving the request did not reach the
    # catch-all and come back as index.html with a 200.
    assert FRONTEND_BUILT_HEADER not in response.headers


def test_assets_do_not_reveal_whether_files_outside_the_directory_exist(tmp_path: Path) -> None:
    """A ``..`` path must get the same plain 404 whether or not its target exists.

    Flask's ``<path:>`` converter passes ``..`` segments through unnormalized, so
    any pre-check that joins the raw filename onto the assets directory stats
    paths outside it -- and a response that differs between an existing and a
    missing target is an existence oracle for the whole filesystem.
    """
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<html>app</html>")

    state = build_test_state()
    state.static_directory = static_dir
    test_client = create_application(state).test_client()
    # index.html exists one level above assets/; a file two levels up does not.
    exists_outside = test_client.get("/assets/../index.html")
    missing_outside = test_client.get("/assets/../../no-such-file")

    for response in (exists_outside, missing_outside):
        assert response.status_code == 404
        assert response.data == b""


def test_assets_serve_a_bundle_that_appeared_after_startup(tmp_path: Path) -> None:
    """The route must survive being constructed before the bundle exists.

    Deciding at construction time whether to register it turned a recoverable
    state into a stuck one: rebuilding no longer helped until a restart.
    """
    static_dir = tmp_path / "static"
    static_dir.mkdir()

    state = build_test_state()
    state.static_directory = static_dir
    # App built while there is no bundle at all, as it is on a cold start
    # into a wiped tree.
    test_client = create_application(state).test_client()
    (static_dir / "assets").mkdir()
    (static_dir / "assets" / "index-abc123.js").write_text("console.log('app');")
    response = test_client.get("/assets/index-abc123.js")

    assert response.status_code == 200
    assert "javascript" in response.headers["Content-Type"]


def test_http_errors_keep_their_status_codes(client: FlaskClient) -> None:
    """Routing-level HTTP errors pass through the unhandled-exception handler intact.

    Regression: the handler re-raised HTTPExceptions, which re-entered Flask's
    handle_exception and surfaced every 404/405 as a 500 (observed live on a
    method-not-allowed destroy call).
    """
    # Non-GET probes are the observable cases: the SPA catch-all intentionally
    # serves the frontend for any unknown GET, so those return 200 by design.
    assert client.post("/api/definitely-not-a-route").status_code == 405
    assert client.put("/api/layout/broadcast").status_code == 405


@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_websocket_endpoint_sends_initial_snapshot(app: Flask) -> None:
    """On connect the socket sends the shell's inventory and projects."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(2)]
        finally:
            close_ws(ws)

    assert [message["type"] for message in messages] == ["apps_updated", "projects_updated"]
    assert messages[0]["apps"] == []
    assert messages[1]["projects"] == []


def test_a_client_state_report_survives_an_unwritable_state_file(app: Flask) -> None:
    """The live registration is what the layout ops need; a state file the shell cannot write is logged, not fatal."""
    shell = state_of(app).shell
    (shell.state_directory / "clients.json").mkdir(parents=True)
    (shell.activity.events_path).mkdir(parents=True)
    client_queue = shell.broadcaster.register()
    try:
        report = {"type": "client_state", "client_id": "c1", "device_kind": "desktop", "active_view": "everything"}
        assert _handle_client_state_message(json.dumps(report), client_queue, shell, is_first_report=True) is True
        switched = {**report, "active_view": "alpha", "previous_view": "everything"}
        assert _handle_client_state_message(json.dumps(switched), client_queue, shell, is_first_report=False) is True
        assert shell.broadcaster.get_client_info(client_queue) == {
            "client_id": "c1",
            "active_view": "alpha",
            "device_kind": "desktop",
        }
    finally:
        shell.broadcaster.unregister(client_queue)


def test_client_state_reports_register_the_client_and_log_only_real_view_switches(app: Flask) -> None:
    """A report registers the connection with the broadcaster and records the client; a view_switch is
    logged only when the report names a previous view that differs; anything malformed is ignored."""
    shell = state_of(app).shell
    client_queue = shell.broadcaster.register()
    try:
        first = json.dumps(
            {"type": "client_state", "client_id": "c1", "device_kind": "mobile", "active_view": "everything"}
        )
        assert _handle_client_state_message(first, client_queue, shell, is_first_report=True) is True
        assert shell.broadcaster.get_client_info(client_queue) == {
            "client_id": "c1",
            "active_view": "everything",
            "device_kind": "mobile",
        }
        recorded = shell.clients.get_client("c1")
        assert recorded is not None
        assert recorded.device_kind is DeviceKind.MOBILE and recorded.active_view == "everything"
        assert shell.activity.read_events() == []

        switched = json.dumps(
            {
                "type": "client_state",
                "client_id": "c1",
                "device_kind": "mobile",
                "active_view": "alpha",
                "previous_view": "everything",
            }
        )
        assert _handle_client_state_message(switched, client_queue, shell, is_first_report=False) is True
        unchanged = json.dumps(
            {
                "type": "client_state",
                "client_id": "c1",
                "device_kind": "mobile",
                "active_view": "alpha",
                "previous_view": "alpha",
            }
        )
        assert _handle_client_state_message(unchanged, client_queue, shell, is_first_report=False) is True
        events = shell.activity.read_events()
        assert [(event["type"], event["from_view_id"], event["to_view_id"]) for event in events] == [
            ("view_switch", "everything", "alpha")
        ]
        assert shell.broadcaster.get_client_info(client_queue) == {
            "client_id": "c1",
            "active_view": "alpha",
            "device_kind": "mobile",
        }

        for malformed in ("{", json.dumps({"type": "other"}), json.dumps({"type": "client_state", "client_id": "c1"})):
            assert _handle_client_state_message(malformed, client_queue, shell, is_first_report=False) is False
        assert shell.broadcaster.get_client_info(client_queue) == {
            "client_id": "c1",
            "active_view": "alpha",
            "device_kind": "mobile",
        }
    finally:
        shell.broadcaster.unregister(client_queue)


def test_not_built_page_coordinate_regex_matches_the_canonical_one() -> None:
    """The placeholder derives a service origin, so it carries a copy of the rule.

    The shared library's ``origin.ts`` is canonical. The placeholder cannot import
    it -- it runs in the browser, in the one state where the bundle it lives in is
    missing -- so it holds its own copy, and this pins that copy to the source of
    truth. Without it the rule can be corrected in one place and silently rot in the
    page that only renders when everything else is broken.
    """
    origin_ts = Path(__file__).parents[4] / "libs" / "workspace_ui" / "src" / "origin.ts"
    canonical = re.search(r"WORKSPACE_COORDINATE_LABEL = (/.+/i);", origin_ts.read_text())
    assert canonical is not None, f"the canonical regex is no longer declared in {origin_ts}"

    page = render_frontend_not_built_page("terminal-x7k9q2w1")
    in_page = re.findall(r"(/\^\(\?:.+?/i)\.test\(", page)
    assert in_page == [canonical.group(1)], (
        f"the placeholder's coordinate regex has drifted from {origin_ts}: "
        f"page has {in_page}, origin.ts has {canonical.group(1)!r}"
    )


def test_not_built_placeholder_answers_its_own_poll_cheaply(tmp_path: Path) -> None:
    """The poll reads a header, so HEAD must still carry it -- and nothing else.

    This is the page's only route back to the interface, so a HEAD that stopped
    reporting the marker would strand every open tab until someone reloaded by
    hand. It is also the request the page makes every ten seconds per tab for
    the length of an outage, so it must not re-render the page or re-read the
    app registry to answer.
    """
    empty_dir = tmp_path / "static"
    empty_dir.mkdir()

    state = build_test_state()
    state.static_directory = empty_dir
    test_client = create_application(state).test_client()
    head = test_client.head("/")
    get = test_client.get("/")

    assert head.headers[FRONTEND_BUILT_HEADER] == "false"
    assert get.headers[FRONTEND_BUILT_HEADER] == "false"
    # The GET is the one that renders; the HEAD carries no page to render.
    assert "needs to be rebuilt" in get.text
    assert head.text == ""
