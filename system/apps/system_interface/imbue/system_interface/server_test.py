"""Tests for the Flask server."""

import fcntl
import html
import io
import json
import os
import queue
import re
import subprocess
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from urllib.parse import quote

import pytest
from flask import Flask
from flask.testing import FlaskClient
from mngr_cli_contract.contract import assert_mngr_argv_valid
from oom_priority import bands

from imbue.concurrency_group.subprocess_utils import FinishedProcess
from imbue.mngr.errors import AgentStartError
from imbue.mngr.errors import MngrError
from imbue.mngr_codex.app_server_client import CodexModel
from imbue.system_interface.accounts import commit_account
from imbue.system_interface.accounts import mint_account_dir
from imbue.system_interface.activity_state import ActivityState
from imbue.system_interface.agent_discovery import AgentInfo
from imbue.system_interface.agent_manager import AgentManager
from imbue.system_interface.agent_manager import _build_chat_create_command
from imbue.system_interface.agent_manager import _build_chat_destroy_command
from imbue.system_interface.app_context import SystemInterfaceState
from imbue.system_interface.app_context import state_of
from imbue.system_interface.chat_document import _DEFAULT_TAIL_COUNT
from imbue.system_interface.chat_document import _agent_switch_options
from imbue.system_interface.chat_document import _build_fast_mode_answered_label_command
from imbue.system_interface.chat_document import _build_stop_command
from imbue.system_interface.chat_document import _revive_and_retry_send
from imbue.system_interface.chat_document import _stream_filtered_events
from imbue.system_interface.config import Config
from imbue.system_interface.documents import FRONTEND_BUILT_HEADER
from imbue.system_interface.event_queues import AgentEventQueues
from imbue.system_interface.harnesses.claude.tap import ClaudeInterruptToComposer
from imbue.system_interface.harnesses.codex.ledger import ShoulderTapResult
from imbue.system_interface.harnesses.codex.live_connection import CodexLiveConnection
from imbue.system_interface.harnesses.codex.model import codex_models_to_options
from imbue.system_interface.harnesses.codex.model import get_codex_model_options_path
from imbue.system_interface.harnesses.codex.model import read_codex_model_options
from imbue.system_interface.harnesses.codex.session import CodexHarnessSession
from imbue.system_interface.harnesses.harness_type import HarnessType
from imbue.system_interface.harnesses.pi_coding.model import PiInterruptToComposer
from imbue.system_interface.harnesses.registry import build_interrupt_to_composer
from imbue.system_interface.harnesses.registry import build_shoulder_tap
from imbue.system_interface.harnesses.session import FileHarnessSession
from imbue.system_interface.harnesses.session import SendOutcome
from imbue.system_interface.harnesses.session import SessionDeps
from imbue.system_interface.models import AgentStateItem
from imbue.system_interface.models import SendMessageRequest
from imbue.system_interface.oom_prioritizer import ChatOomPrioritizer
from imbue.system_interface.server import _NOT_BUILT_REPAIR_ARGV
from imbue.system_interface.server import _NOT_BUILT_REPAIR_COMMAND
from imbue.system_interface.server import _NOT_BUILT_REPAIR_MNGR_COMMAND
from imbue.system_interface.server import _handle_client_state_message
from imbue.system_interface.server import create_application
from imbue.system_interface.server import render_frontend_not_built_page
from imbue.system_interface.shell.primitives import DeviceKind
from imbue.system_interface.testing import RecordingMngrMessenger
from imbue.system_interface.testing import build_test_state
from imbue.system_interface.testing import close_ws
from imbue.system_interface.testing import open_ws
from imbue.system_interface.testing import serve_app
from imbue.system_interface.update_staleness import WORKSPACE_ROOT_DIRECTORY
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

# Generous: the first receive occasionally exceeded the previous 5.0s cap on a
# loaded machine (~1-in-8 locally, failing as ``json.loads(None)``) even though
# passing runs complete in well under a second -- the wait is pure scheduling
# delay, so a bigger cap costs nothing when healthy.
_WS_RECEIVE_TIMEOUT = 15.0


@pytest.fixture
def config() -> Config:
    return Config()


@pytest.fixture
def signed_in_account() -> str:
    """One provider account, because creating a chat now requires one.

    There is no shared login to fall back to -- `resolve_binding` raises rather than binding an
    agent to nothing -- so a create with no account is refused with a 400. Tests about naming,
    conflicts and projects all create chats and none of them are about that.
    """
    account_id, _ = mint_account_dir()
    commit_account(account_id, "anthropic", "Anthropic")
    return account_id


@pytest.fixture
def app(config: Config, signed_in_account: str) -> Flask:
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


def _chat_create_template() -> dict[str, object]:
    """The workspace's own ``[create_templates.chat]`` block, read from its settings.

    Parsed straight out of the TOML rather than through mngr's config loader: the
    question is what this repo ships, not what a particular machine resolves, and
    the loader would fold in user and local layers that a workspace being repaired
    may not have. ``server.py`` resolves the workspace root the same way.
    """
    settings = tomllib.loads((WORKSPACE_ROOT_DIRECTORY / ".mngr" / "settings.toml").read_text())
    return settings["create_templates"]["chat"]


def test_not_built_repair_command_is_the_one_the_app_runs_for_a_chat() -> None:
    """The suggested agent has to come up as a chat, or the suggestion misleads.

    The page tells a reader to create an agent to repair the workspace, and an
    agent created with the wrong flags is a different thing: a worktree of the
    tree instead of the tree itself, in the wrong memory band, without the chat
    role. So every flag the page suggests must be one the app itself passes
    when it creates a chat, and the command must be one the live CLI accepts.
    """
    argv = list(_NOT_BUILT_REPAIR_ARGV)
    assert_mngr_argv_valid(argv)

    real = _build_chat_create_command(
        mngr_binary="mngr",
        name="repair",
        agent_id="agent-123",
        primary_labels={},
        harness=HarnessType.CLAUDE,
    )
    assert argv[argv.index("--template") + 1] == real[real.index("--template") + 1]
    assert "user_created=true" in real

    # ``--no-connect`` is the one flag deliberately inverted: it exists to stop a
    # headless caller attaching, and a reader typing this wants to land in the
    # conversation.
    assert "--no-connect" in real
    assert "--connect" in argv
    assert "--no-connect" not in argv

    # ``--type`` is the one the builder must pass and the page must not: the app
    # is serving a harness the user picked from a menu, while the page has no
    # such choice to carry and would be pinning every reader to whichever harness
    # was current when this string was written. Omitted, mngr resolves it from
    # ``[commands.create] type``, so the repair agent comes up on whatever this
    # workspace opens chats as.
    assert "--type" in real
    assert "--type" not in argv

    # ``--transfer`` is left out for a different reason, and a weaker one: the
    # ``chat`` template already sets it, so the line does not have to. Unlike the
    # harness this is not the reader's to choose -- an agent in a worktree would
    # repair a copy of the workspace instead of the workspace -- so the template
    # is read rather than assumed. Losing that setting has to fail here and not
    # in a workspace that has already lost its interface.
    assert "--transfer" in real
    assert "--transfer" not in argv
    assert _chat_create_template()["transfer"] == "none"

    # No agent name, so mngr mints one and nothing collides with an earlier run.
    # The whole line has to stay flags-only for that: ``mngr create`` reads bare
    # words as positionals (the name, then the agent type), so one anywhere past
    # the subcommand -- not just directly after it -- puts the collision back.
    # ``assert_mngr_argv_valid`` does not catch that: it checks option shape and
    # throws the positionals away. A value-taking flag added to the line without
    # being named here reports its value as a positional, which fails in the
    # direction that gets looked at.
    assert argv[:2] == ["mngr", "create"]
    flags_taking_a_value = {"--template", "--transfer", "--label", "--message"}
    positionals = [
        token
        for index, token in enumerate(argv[2:], start=2)
        if not token.startswith("--") and argv[index - 1] not in flags_taking_a_value
    ]
    assert positionals == [], f"the suggested line passes positional arguments: {positionals}"

    # The message is what makes the created agent useful without the reader
    # having to describe anything, so it has to survive the shell as one word of
    # plain prose -- an escape dropped from the line above splits it into several
    # words, or leaves the escapes themselves in what the agent is told.
    assert argv[argv.index("--message") + 1] == (
        "i'm seeing \"this workspace's interface needs to be rebuilt, can you fix it?\""
    )

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


def test_list_agents_endpoint(client: FlaskClient) -> None:
    """The agents endpoint returns agent data."""
    with patch("imbue.system_interface.chat_document.discover_agents") as mock_discover:
        mock_discover.return_value = [
            AgentInfo(
                id="agent-123",
                name="test-agent",
                state="RUNNING",
                agent_state_dir=Path("/tmp/test"),
                claude_config_dir=Path("/tmp/.claude"),
            )
        ]
        response = client.get("/api/agents")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["agents"]) == 1
    assert data["agents"][0]["name"] == "test-agent"
    assert data["agents"][0]["state"] == "RUNNING"


def test_get_events_for_unknown_agent(client: FlaskClient) -> None:
    """Getting events for a nonexistent agent returns 404."""
    with patch("imbue.system_interface.chat_document.discover_agents", return_value=[]):
        response = client.get("/api/agents/nonexistent/events")
    assert response.status_code == 404


def test_send_message_for_unknown_agent(client: FlaskClient) -> None:
    """Sending a message to a nonexistent agent returns 404."""
    with patch("imbue.system_interface.chat_document.discover_agents", return_value=[]):
        response = client.post("/api/agents/nonexistent/message", json={"message": "hello"})
    assert response.status_code == 404


def test_http_errors_keep_their_status_codes(client: FlaskClient) -> None:
    """Routing-level HTTP errors pass through the unhandled-exception handler intact.

    Regression: the handler re-raised HTTPExceptions, which re-entered Flask's
    handle_exception and surfaced every 404/405 as a 500 (observed live on a
    method-not-allowed destroy call).
    """
    # Non-GET probes are the observable cases: the SPA catch-all intentionally
    # serves the frontend for any unknown GET, so those return 200 by design.
    assert client.post("/api/definitely-not-a-route").status_code == 405
    assert client.put("/api/agents/x/destroy").status_code == 405


def _upload_relative_path(stored_path: str) -> str:
    """Extract the ``<subdir>/<name>`` part of an absolute upload path."""
    return stored_path.split("/uploads/", 1)[1]


def test_upload_attachment_stores_file_and_returns_path(client: FlaskClient) -> None:
    """Uploading a file stores it under data/uploads/ and returns its path and size."""
    response = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"image-bytes"), "diagram.png")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    data = response.get_json()
    assert "/uploads/" in data["path"]
    assert data["path"].endswith("/diagram.png")
    assert data["size"] == len(b"image-bytes")
    assert Path(data["path"]).read_bytes() == b"image-bytes"


def test_upload_attachment_without_file_returns_400(client: FlaskClient) -> None:
    """Posting with no file part is a 400."""
    response = client.post("/api/uploads", data={}, content_type="multipart/form-data")

    assert response.status_code == 400


def test_serve_attachment_returns_stored_bytes(client: FlaskClient) -> None:
    """A stored attachment can be fetched back for preview."""
    upload = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"hello-bytes"), "note.txt")},
        content_type="multipart/form-data",
    )
    relative_path = _upload_relative_path(upload.get_json()["path"])

    response = client.get(f"/api/uploads/{relative_path}")

    assert response.status_code == 200
    assert response.data == b"hello-bytes"


def test_serve_attachment_missing_returns_404(client: FlaskClient) -> None:
    """Fetching an unknown attachment is a 404."""
    response = client.get("/api/uploads/deadbeef/missing.png")

    assert response.status_code == 404


def test_delete_attachment_removes_stored_file(client: FlaskClient) -> None:
    """Deleting an attachment removes it from disk and from later fetches."""
    upload = client.post(
        "/api/uploads",
        data={"file": (io.BytesIO(b"bye-bytes"), "remove-me.txt")},
        content_type="multipart/form-data",
    )
    stored_path = upload.get_json()["path"]
    relative_path = _upload_relative_path(stored_path)

    delete_response = client.delete(f"/api/uploads/{relative_path}")

    assert delete_response.status_code == 200
    assert not Path(stored_path).exists()
    assert client.get(f"/api/uploads/{relative_path}").status_code == 404


def test_delete_attachment_missing_is_ok(client: FlaskClient) -> None:
    """Deleting an unknown attachment still reports success (idempotent)."""
    response = client.delete("/api/uploads/deadbeef/missing.png")

    assert response.status_code == 200


def test_get_events_with_session_files(client: FlaskClient, tmp_path: Path) -> None:
    """Getting events for an agent with session files returns parsed events."""
    # Set up agent state dir with session history
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)

    # Create a session file
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "Hello"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-2",
                "timestamp": "2026-01-01T00:00:01Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [{"type": "text", "text": "Hi!"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )

    # Write session history
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")

    assert response.status_code == 200
    data = response.get_json()
    assert len(data["events"]) == 2
    assert data["events"][0]["type"] == "user_message"
    assert data["events"][0]["content"] == "Hello"
    assert data["events"][1]["type"] == "assistant_message"
    assert data["events"][1]["text"] == "Hi!"


def test_get_event_detail_serves_and_404s(client: FlaskClient, tmp_path: Path) -> None:
    """The detail endpoint reconstructs one event's full payloads from disk, and answers a
    clean 404 (the frontend's quiet placeholder) for an unknown event."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)
    session_id = "detail-session"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "uuid-r",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "z" * 9000}],
                },
            }
        )
        + "\n"
    )
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        events = client.get("/api/agents/agent-123/events").get_json()["events"]
        result_event = next(e for e in events if e["type"] == "tool_result")
        # Payload-free wire: the output is not on the event.
        assert "output" not in result_event
        assert result_event["output_chars"] == 9000

        detail = client.get(f"/api/agents/agent-123/events/{result_event['event_id']}/detail")
        assert detail.status_code == 200
        assert detail.get_json()["output"] == "z" * 9000

        missing = client.get("/api/agents/agent-123/events/not-a-real-event/detail")
        assert missing.status_code == 404


def test_stop_and_remove_watcher_evicts_and_rebuilds_on_demand(tmp_path: Path) -> None:
    """Eviction releases the watcher (resident transcript, watch thread); a later read
    rebuilds it from disk transparently -- the chat-memory lifecycle's two halves."""
    state = build_test_state()
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / "claude_config"
    (claude_config_dir / "projects" / "hash123").mkdir(parents=True)
    (claude_config_dir / "projects" / "hash123" / "s1.jsonl").write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {"role": "user", "content": "hello"},
            }
        )
        + "\n"
    )
    (agent_state_dir / "claude_session_id_history").write_text("s1\n")
    agent_info = AgentInfo(
        id="evictable-agent",
        name="evictable-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )

    first = state.get_or_create_watcher(agent_info)
    assert state.watchers == {"evictable-agent": first}
    assert len(first.get_all_events()) == 1

    state.stop_and_remove_watcher("evictable-agent")
    assert state.watchers == {}
    # Idempotent for an unknown/already-evicted agent.
    state.stop_and_remove_watcher("evictable-agent")

    rebuilt = state.get_or_create_watcher(agent_info)
    assert rebuilt is not first
    assert [e["content"] for e in rebuilt.get_all_events()] == ["hello"]
    state.shutdown()


def test_get_events_caps_initial_load_to_tail(client: FlaskClient, tmp_path: Path) -> None:
    """The no-`before` events response is capped to the most recent N events,
    and older events remain reachable via the `before` backfill branch (issue I)."""
    agent_state_dir = tmp_path / "agent_state"
    agent_state_dir.mkdir(parents=True)
    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)

    total_events = _DEFAULT_TAIL_COUNT + 10
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    session_file.write_text(
        "".join(
            json.dumps(
                {
                    "type": "user",
                    "uuid": f"uuid-{i:03d}",
                    "timestamp": f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z",
                    "message": {"role": "user", "content": f"Message {i}"},
                }
            )
            + "\n"
            for i in range(total_events)
        )
    )
    (agent_state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    agent_info = AgentInfo(
        id="agent-123",
        name="test-agent",
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
    )

    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.get("/api/agents/agent-123/events")
        assert response.status_code == 200
        body = response.get_json()
        events = body["events"]
        # Only the most recent _DEFAULT_TAIL_COUNT events are returned.
        assert len(events) == _DEFAULT_TAIL_COUNT
        assert events[0]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT}"
        assert events[-1]["content"] == f"Message {total_events - 1}"
        # offset + total place the tail window in the full conversation: the first
        # tail event sits at index (total - tail), so offset > 0 tells the client
        # there is older history above to page in.
        assert body["total"] == total_events
        assert body["offset"] == total_events - _DEFAULT_TAIL_COUNT

        # Older events are still reachable by paging backwards from the oldest
        # event in the initial tail.
        oldest_in_tail = events[0]["event_id"]
        backfill = client.get(f"/api/agents/agent-123/events?before={oldest_in_tail}")
        assert backfill.status_code == 200
        backfill_body = backfill.get_json()
        backfill_events = backfill_body["events"]
        assert len(backfill_events) == total_events - _DEFAULT_TAIL_COUNT
        assert backfill_events[0]["content"] == "Message 0"
        assert backfill_events[-1]["content"] == f"Message {total_events - _DEFAULT_TAIL_COUNT - 1}"
        # The page reached the very first event (offset 0 => no more history above).
        assert backfill_body["offset"] == 0
        assert backfill_body["total"] == total_events

        # A jump lands a window at an arbitrary global offset in one request,
        # rather than paging through everything before it.
        jump = client.get("/api/agents/agent-123/events?offset=5&limit=4")
        assert jump.status_code == 200
        jump_body = jump.get_json()
        assert [e["content"] for e in jump_body["events"]] == [f"Message {i}" for i in range(5, 9)]
        assert jump_body["offset"] == 5

        # From that jumped window the client can page *newer* (toward the tail).
        after_id = jump_body["events"][-1]["event_id"]
        forward = client.get(f"/api/agents/agent-123/events?after={after_id}&limit=3")
        assert forward.status_code == 200
        forward_body = forward.get_json()
        assert [e["content"] for e in forward_body["events"]] == [f"Message {i}" for i in range(9, 12)]
        assert forward_body["offset"] == 9

        # A non-positive limit must not defeat the cap (``[-0:]`` would return
        # the whole list); it falls back to the default tail count.
        zero_limit = client.get("/api/agents/agent-123/events?limit=0")
        assert zero_limit.status_code == 200
        assert len(zero_limit.get_json()["events"]) == _DEFAULT_TAIL_COUNT


def test_send_message_success() -> None:
    """Sending a message to a known agent addresses it by id and succeeds."""
    agent_id = "agent-00000000000000000000000000000001"
    agent_info = AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hello"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    # The endpoint routes through AgentManager.send_message_to_agent, which addresses
    # the agent by id (the live cache supplies the known location as the 3rd arg).
    assert messenger.sent == [(agent_id, "hello")]


class _FakeCodexLedger:
    """A stand-in for the live codex ledger the endpoints reach through the agent manager."""

    def __init__(
        self,
        *,
        sending: bool = False,
        tap: bool = False,
        interrupt_block: str = "",
        tap_status: str = "tapped",
        tap_returned_block: str = "",
    ) -> None:
        self._sending = sending
        self._tap = tap
        self._interrupt_block = interrupt_block
        self._tap_status = tap_status
        self._tap_returned_block = tap_returned_block
        self.sent: list[tuple[str, str | None]] = []
        self.tap_calls = 0

    def send(self, text: str, client_id: str | None = None) -> str:
        self.sent.append((text, client_id))
        return client_id or "cid"

    def is_sending(self) -> bool:
        return self._sending

    def is_tap_available(self) -> bool:
        return self._tap

    def shoulder_tap(self) -> ShoulderTapResult:
        self.tap_calls += 1
        return ShoulderTapResult(status=self._tap_status, returned_block=self._tap_returned_block)

    def interrupt(self) -> str:
        return self._interrupt_block


def _codex_client(agent_info: AgentInfo) -> FlaskClient:
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    return create_application(build_test_state(agent_manager=manager)).test_client()


def _file_session_for(agent_info: AgentInfo, in_flight: str = "") -> FileHarnessSession:
    """A real FileHarnessSession over inert deps, optionally pre-seeded with an in-flight send."""
    deps = SessionDeps(
        harness=agent_info.harness,
        state_dir=agent_info.agent_state_dir,
        model_state_path=agent_info.agent_state_dir / "model_state.json",
        send_to_harness=lambda text: True,
        notify_agents_changed=lambda: None,
        is_tracked=lambda: True,
        on_queue_snapshot=lambda snapshot: None,
        on_user_turn=lambda event: None,
        recompute_activity=lambda: None,
        clear_queue_state=lambda: None,
        catalog_options=lambda: (),
        build_interrupter=build_interrupt_to_composer,
        build_shoulder_tap=build_shoulder_tap,
    )
    file_session = FileHarnessSession.build(deps)
    if in_flight:
        file_session._sending.record("t-in-flight", in_flight)
    return file_session


def _codex_session_over(ledger: "_FakeCodexLedger | None") -> CodexHarnessSession:
    """A codex session whose live ledger is the given fake (None = daemon down/starting)."""
    session = CodexHarnessSession.__new__(CodexHarnessSession)
    session.ensure_live = lambda: None
    session._live_ledger = lambda: ledger
    return session


def test_send_message_codex_routes_through_the_ledger(tmp_path: Path) -> None:
    """A codex send is submitted through the live ledger (backend authority), not the mngr send."""
    agent_id = "codex-agent-1"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    ledger = _FakeCodexLedger()
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(ledger)),
    ):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hi", "message_id": "m1"})
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert ledger.sent == [("hi", "m1")]


def test_send_message_codex_returns_503_when_the_daemon_is_not_ready(tmp_path: Path) -> None:
    """No live ledger and a failed revive surface an explicit, retryable not-ready error.

    A NOT_READY send first tries to revive the agent through the same start path the
    start endpoint uses; when even that fails (here: mngr cannot start it), the honest
    503 stands."""
    agent_id = "codex-agent-2"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    client = _codex_client(agent_info)
    started: list[str] = []

    def failing_start(agent_name: str) -> None:
        started.append(agent_name)
        raise MngrError("no such agent")

    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch("imbue.system_interface.chat_document.start_agent", failing_start),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(None)),
    ):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hi"})
    assert response.status_code == 503
    assert started == [agent_info.name]


def test_send_message_codex_revives_a_stopped_agent_then_sends(tmp_path: Path) -> None:
    """A NOT_READY codex send starts the agent and retries, giving codex the same
    "sending the agent a message revives it" invariant the file-session harnesses get
    from mngr's own auto-start."""
    agent_id = "codex-agent-9"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    client = _codex_client(agent_info)
    ledger = _FakeCodexLedger()

    # The daemon is down until the revive starts the agent; the retry then finds the ledger.
    session = CodexHarnessSession.__new__(CodexHarnessSession)
    session.ensure_live = lambda: None
    live: list[_FakeCodexLedger] = []
    session._live_ledger = lambda: live[0] if live else None

    def fake_start(agent_name: str) -> None:
        live.append(ledger)

    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch("imbue.system_interface.chat_document.start_agent", fake_start),
        patch.object(AgentManager, "get_or_create_session", return_value=session),
    ):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hi", "message_id": "m9"})
    assert response.status_code == 200
    assert ledger.sent == [("hi", "m9")]


def test_revive_and_retry_send_gives_up_after_the_budget(tmp_path: Path, agent_manager: AgentManager) -> None:
    """A daemon that never comes up keeps the honest NOT_READY after the retry budget --
    the retries are paced by the injected sleep, never a spin."""
    agent_info = _model_agent_info("codex-agent-10", tmp_path, harness=HarnessType.CODEX)
    session = _codex_session_over(None)
    sleeps: list[float] = []
    request_body = SendMessageRequest(message="hi", message_id="m10")

    with patch("imbue.system_interface.chat_document.start_agent", lambda name: None):
        outcome = _revive_and_retry_send(
            agent_info, agent_manager, session, request_body, "m10", sleep=sleeps.append, budget_seconds=0.0
        )
    assert outcome is SendOutcome.NOT_READY
    assert sleeps == []


def test_shoulder_tap_codex_tapped_when_a_message_is_queued(tmp_path: Path) -> None:
    """The codex tap delivers the queue early through the ledger's ``shoulder_tap`` (Fix 3)."""
    agent_id = "codex-agent-3"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    ledger = _FakeCodexLedger(tap_status="tapped")
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(ledger)),
    ):
        response = client.post(f"/api/agents/{agent_id}/shoulder-tap-atomic")
    assert response.status_code == 200
    assert response.get_json()["status"] == "tapped"
    assert ledger.tap_calls == 1


def test_shoulder_tap_codex_is_a_benign_200_when_a_send_is_in_flight(tmp_path: Path) -> None:
    """A tap racing an in-flight send is a BENIGN 200 no-op (``send_in_flight``), never a 500 dialog
    (Fix 3): the pushed availability flag already greys the button, so a raced tap just does nothing."""
    agent_id = "codex-agent-4"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    ledger = _FakeCodexLedger(sending=True, tap_status="send_in_flight")
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(ledger)),
    ):
        response = client.post(f"/api/agents/{agent_id}/shoulder-tap-atomic")
    assert response.status_code == 200
    assert response.get_json()["status"] == "send_in_flight"


def test_shoulder_tap_codex_no_ledger_is_a_noop(tmp_path: Path) -> None:
    agent_id = "codex-agent-5"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(None)),
    ):
        response = client.post(f"/api/agents/{agent_id}/shoulder-tap-atomic")
    assert response.status_code == 200
    assert response.get_json()["status"] == "no_open_turn"


def test_shoulder_tap_codex_resend_failure_hands_the_block_back_to_the_composer(tmp_path: Path) -> None:
    """When the ledger's combined resend fails to submit, the endpoint returns the parked text as a
    composer block (contract A1a) so the frontend places it, rather than swallowing it (Fix 3)."""
    agent_id = "codex-agent-8"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    ledger = _FakeCodexLedger(tap_status="tapped", tap_returned_block="first\nsecond")
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(ledger)),
    ):
        response = client.post(f"/api/agents/{agent_id}/shoulder-tap-atomic")
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "tapped"
    assert body["block"] == "first\nsecond"


def test_drain_to_composer_codex_returns_the_ledger_block(tmp_path: Path) -> None:
    """codex's stop returns exactly the ledger's interrupt block (the non-committed messages)."""
    agent_id = "codex-agent-6"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    ledger = _FakeCodexLedger(interrupt_block="bring me back to edit")
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(ledger)),
    ):
        response = client.post(f"/api/agents/{agent_id}/drain-to-composer")
    assert response.status_code == 200
    assert response.get_json()["block"] == "bring me back to edit"


def test_drain_to_composer_codex_no_ledger_returns_empty_block(tmp_path: Path) -> None:
    agent_id = "codex-agent-7"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    client = _codex_client(agent_info)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(AgentManager, "get_or_create_session", return_value=_codex_session_over(None)),
    ):
        response = client.post(f"/api/agents/{agent_id}/drain-to-composer")
    assert response.status_code == 200
    assert response.get_json()["block"] == ""


def _model_agent_info(agent_id: str, tmp_path: Path, harness: HarnessType = HarnessType.CLAUDE) -> AgentInfo:
    """An AgentInfo with real (empty) config/state dirs for the given harness."""
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir(exist_ok=True)
    (tmp_path / "state").mkdir(exist_ok=True)
    return AgentInfo(
        id=agent_id,
        name="test-agent",
        state="RUNNING",
        agent_state_dir=tmp_path / "state",
        claude_config_dir=config_dir,
        harness=harness,
    )


def _manager_with_resolver(agent_info: AgentInfo) -> tuple[AgentManager, RecordingMngrMessenger]:
    """A recording-messenger manager for the switch endpoint. The endpoint builds the
    resolver inline from the ``_find_agent`` result, so nothing needs pre-seeding here."""
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    return manager, messenger


def test_get_harnesses_lists_the_claude_catalog(client: FlaskClient) -> None:
    """The catalog endpoint serves each harness's static model catalog."""
    response = client.get("/api/harnesses")
    assert response.status_code == 200
    data = response.get_json()
    assert "claude" in data
    claude = data["claude"]
    offered = [option["id"] for option in claude["options"] if option["in_picker"]]
    assert offered == ["fable[1m]", "opus[1m]", "sonnet[1m]", "haiku"]
    # The rest are display-only: served so a live read still resolves, never offered.
    assert any(not option["in_picker"] for option in claude["options"])
    # Each option carries the suffix-free reported id the matcher keys on. Keyed by id
    # rather than by position, so reordering the picker does not break this.
    reported = {option["id"]: option["harness_reported_model_id"] for option in claude["options"]}
    assert reported["fable[1m]"] == "claude-fable-5"
    assert reported["opus[1m]"] == "claude-opus-5"
    assert reported["sonnet[1m]"] == "claude-sonnet-5"
    assert claude["switch_mode"] == "eager_then_reconcile"
    assert claude["powered_by_text"] == ""


def test_get_harnesses_includes_every_harness(client: FlaskClient) -> None:
    """Every harness is in the catalog, whatever the user has signed in to.

    A codex or pi agent can exist without any account for it (made by ``mngr create``,
    or left behind after its account was removed), and its model bar resolves against
    this catalog -- so narrowing it to the signed-in harnesses would strand that agent's
    chip on an unrecognized model.
    """
    catalog = client.get("/api/harnesses").get_json()
    assert "claude" in catalog
    assert "codex" in catalog


def test_powered_by_is_empty_for_a_harness_that_declares_no_credit(client: FlaskClient, tmp_path: Path) -> None:
    """Claude declares "" as its credit text, so the endpoint returns it and nothing renders."""
    agent_id = "agent-00000000000000000000000000000010"
    agent_info = _model_agent_info(agent_id, tmp_path)
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/powered-by")
    assert response.status_code == 200
    assert response.get_json() == {"label": ""}


def test_powered_by_resolves_the_text_per_harness(client: FlaskClient, tmp_path: Path) -> None:
    """The text is a pure function of the agent's harness, prefix included."""
    agent_id = "agent-00000000000000000000000000000011"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/powered-by")
    assert response.status_code == 200
    assert response.get_json() == {"label": "Powered by Codex"}


def test_powered_by_unknown_agent_returns_404(client: FlaskClient) -> None:
    """A proto-agent (not yet discoverable) 404s, so the frontend shows no credit."""
    with patch("imbue.system_interface.chat_document._find_agent", return_value=None):
        response = client.get("/api/agents/nonexistent/powered-by")
    assert response.status_code == 404


def test_set_model_switch_sends_claude_commands(tmp_path: Path) -> None:
    """A claude switch sends exactly the axes the client says a click changed.

    The client reports model + effort changed (not fast), so the endpoint sends
    /model + /effort and not /fast.
    """
    agent_id = "agent-00000000000000000000000000000004"
    agent_info = _model_agent_info(agent_id, tmp_path)
    manager, messenger = _manager_with_resolver(agent_info)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post(
            f"/api/agents/{agent_id}/model",
            json={
                "model_id": "sonnet[1m]",
                "effort": "high",
                "fast": False,
                "axes": ["model", "effort"],
            },
        )

    assert response.status_code == 200
    assert messenger.sent == [(agent_id, "/model sonnet[1m]"), (agent_id, "/effort high")]


def test_set_model_rejects_unknown_model(tmp_path: Path) -> None:
    """An id outside the catalog is a 400 and no command is sent."""
    agent_id = "agent-00000000000000000000000000000005"
    agent_info = _model_agent_info(agent_id, tmp_path)
    manager, messenger = _manager_with_resolver(agent_info)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/model", json={"model_id": "gpt-4", "effort": "high"})

    assert response.status_code == 400
    assert messenger.sent == []


def test_set_model_rejects_fast_on_a_model_without_fast(tmp_path: Path) -> None:
    """Fast on a model that does not support it is a 400 and no command is sent."""
    agent_id = "agent-00000000000000000000000000000006"
    agent_info = _model_agent_info(agent_id, tmp_path)
    manager, messenger = _manager_with_resolver(agent_info)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post(
            f"/api/agents/{agent_id}/model", json={"model_id": "sonnet", "effort": "medium", "fast": True}
        )

    assert response.status_code == 400
    assert messenger.sent == []


def test_set_model_unknown_agent_returns_404(client: FlaskClient) -> None:
    with patch("imbue.system_interface.chat_document._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/model", json={"model_id": "sonnet", "effort": "high"})
    assert response.status_code == 404


class _RecordingSwitchClient:
    """A stand-in for the short-lived app-server switch connection: records the settings_update
    kwargs and its close, never touching the pane. ``models`` backs the dynamic model-options fetch."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.closed = False
        self.models: tuple[CodexModel, ...] = ()

    def settings_update(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def model_list(self) -> tuple[CodexModel, ...]:
        return self.models

    def close(self) -> None:
        self.closed = True


def test_set_model_switches_codex_via_thread_settings_update(tmp_path: Path) -> None:
    """Codex switching validates against the per-agent model/list set and applies model + effort +
    fast over thread/settings/update (all three on a model change), not the pane send."""
    agent_id = "agent-00000000000000000000000000000007"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    manager, messenger = _manager_with_resolver(agent_info)
    application = create_application(build_test_state(agent_manager=manager))
    client = application.test_client()
    switch_client = _RecordingSwitchClient()
    # The per-agent option set the endpoint validates against is the ONE reconciled set on the
    # manager (seeded on connect / refreshed by each picker-open); seed it here (no live daemon).
    codex_models = (
        CodexModel.model_validate(
            {
                "id": "gpt-5.6-sol",
                "model": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority"}],
            }
        ),
    )
    manager.get_or_create_session(agent_info).note_offered_options(codex_models_to_options(codex_models))
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.harnesses.codex.model.open_bound_codex_client",
            return_value=switch_client,
        ),
    ):
        response = client.post(
            f"/api/agents/{agent_id}/model",
            json={"model_id": "gpt-5.6-sol", "effort": "high", "fast": False, "axes": ["model", "effort"]},
        )

    assert response.status_code == 200
    # A model switch (re)asserts all three axes over the app-server -- service_tier None clears any
    # stale priority -- and the pane send was never used.
    assert switch_client.calls == [{"model": "gpt-5.6-sol", "effort": "high", "service_tier": None}]
    assert switch_client.closed is True
    assert messenger.sent == []


def test_model_options_returns_full_per_agent_options_for_codex(tmp_path: Path) -> None:
    """The DYNAMIC codex picker gets full per-agent options (from model/list), not just ids."""
    agent_id = "agent-00000000000000000000000000000012"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    manager, _messenger = _manager_with_resolver(agent_info)
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    dynamic_client = _RecordingSwitchClient()
    dynamic_client.models = (
        CodexModel.model_validate(
            {
                "id": "gpt-5.6-sol",
                "model": "gpt-5.6-sol",
                "displayName": "GPT-5.6-Sol",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority"}],
            }
        ),
        CodexModel.model_validate({"id": "gpt-5.2", "model": "gpt-5.2", "displayName": "GPT-5.2"}),
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.harnesses.codex.model.open_bound_codex_client",
            return_value=dynamic_client,
        ),
    ):
        response = client.get(f"/api/agents/{agent_id}/model-options")

    assert response.status_code == 200
    data = response.get_json()
    # The dynamic shape: full options (not the `models` id list).
    assert data["models"] is None
    assert [opt["id"] for opt in data["options"]] == ["gpt-5.6-sol", "gpt-5.2"]
    assert data["options"][0]["supports_fast"] is True
    assert data["options"][1]["supports_fast"] is False


def test_picker_open_reconciles_the_chip_and_switch_model_sets_for_codex(tmp_path: Path) -> None:
    """A codex picker-open fetch (``model/list``) becomes the ONE per-agent set the chip-match and the
    switch-validation ALSO read (D2): after the open, all three agree, and a model the open just
    offered validates on switch."""
    agent_id = "agent-00000000000000000000000000000014"
    agent_info = _model_agent_info(agent_id, tmp_path, harness=HarnessType.CODEX)
    manager, messenger = _manager_with_resolver(agent_info)
    client = create_application(build_test_state(agent_manager=manager)).test_client()

    # Before any open, the chip-match and switch-validation sets are unpopulated -- the model below
    # would 400 on a switch.
    assert manager.get_or_create_session(agent_info).switch_options() == ()
    assert _agent_switch_options(manager, agent_info) == ()

    # A fresh picker-open fetch offers a model the sets did not have.
    picker_client = _RecordingSwitchClient()
    picker_client.models = (
        CodexModel.model_validate(
            {
                "id": "gpt-5.6-terra",
                "model": "gpt-5.6-terra",
                "displayName": "GPT-5.6-Terra",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority"}],
            }
        ),
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.harnesses.codex.model.open_bound_codex_client",
            return_value=picker_client,
        ),
    ):
        options_response = client.get(f"/api/agents/{agent_id}/model-options")
        assert options_response.status_code == 200
        picker_ids = [opt["id"] for opt in options_response.get_json()["options"]]

        # The reconciliation: the picker offer set, the chip-match set, and the switch-validation set
        # are now the SAME set -- the open's fetch updated the one stored per-agent set.
        chip_options = manager.get_or_create_session(agent_info).switch_options()
        assert chip_options != ()
        chip_ids = [opt.id for opt in chip_options]
        switch_ids = [opt.id for opt in _agent_switch_options(manager, agent_info)]
        assert picker_ids == chip_ids == switch_ids == ["gpt-5.6-terra"]

        # The newly-offered model validates on switch (200), applied over thread/settings/update.
        switch_response = client.post(
            f"/api/agents/{agent_id}/model",
            json={
                "model_id": "gpt-5.6-terra",
                "effort": "high",
                "fast": True,
                "axes": ["model", "effort", "fast"],
            },
        )

    assert switch_response.status_code == 200
    assert picker_client.calls == [{"model": "gpt-5.6-terra", "effort": "high", "service_tier": "priority"}]
    assert messenger.sent == []


class _FakeCodexConnection:
    """A minimal stand-in for a live ``CodexLiveConnection`` for the connect-seed write-through."""

    def __init__(self, models: tuple[CodexModel, ...]) -> None:
        self.codex_models = models
        self.is_alive = True
        self.ledger = None

    def stop(self) -> None:
        pass


def test_codex_connect_seed_persists_the_raw_model_options_sidecar(tmp_path: Path) -> None:
    """The connect-time ``model/list`` seed writes the RAW list through to the codex sidecar (as well
    as the in-memory set), so the chip resolves offline after a restart before the daemon reconnects."""
    agent_id = "agent-00000000000000000000000000000015"
    manager = AgentManager.build(WebSocketBroadcaster())
    # Point the manager's state-dir root at tmp_path so the sidecar write lands in the sandbox.
    manager._host_dir = tmp_path
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="seed-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path / "work"),
            harness=HarnessType.CODEX,
        )
        manager._activity_tracked_agents.add(agent_id)
    models = (
        CodexModel.model_validate(
            {
                "id": "gpt-5.6-terra",
                "model": "gpt-5.6-terra",
                "displayName": "GPT-5.6-Terra",
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "serviceTiers": [{"id": "priority"}],
            }
        ),
    )
    with patch.object(CodexLiveConnection, "build", return_value=_FakeCodexConnection(models)):
        session = manager._build_session(agent_id, HarnessType.CODEX)
        with manager._lock:
            manager._session_by_agent[agent_id] = session
        session.ensure_live()

    state_dir = manager._get_agent_state_dir(agent_id)
    assert read_codex_model_options(get_codex_model_options_path(state_dir)) == models
    in_memory = session.switch_options()
    assert [opt.id for opt in in_memory] == ["gpt-5.6-terra"]


def test_model_options_returns_null_models_for_claude(client: FlaskClient, tmp_path: Path) -> None:
    """A static/catalog-backed harness (claude) returns `models` (null = whole catalog), no options."""
    agent_id = "agent-00000000000000000000000000000013"
    agent_info = _model_agent_info(agent_id, tmp_path)
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.get(f"/api/agents/{agent_id}/model-options")
    assert response.status_code == 200
    data = response.get_json()
    assert data["models"] is None
    assert data["options"] is None


def test_fast_mode_answered_label_argv_accepted_by_live_cli() -> None:
    """The latch endpoint shells `mngr label`; the argv must resolve against the
    live CLI so a label-command rename fails here rather than at runtime."""
    argv = _build_fast_mode_answered_label_command("my-agent")
    assert_mngr_argv_valid(argv)
    assert "fast_mode_prompt_answered=true" in argv


def test_fast_mode_answered_returns_404_for_unknown_agent() -> None:
    client = create_application(build_test_state()).test_client()
    response = client.post("/api/agents/agent-doesnotexist/fast-mode-answered")
    assert response.status_code == 404


def _manager_with_capturing_prioritizer(writes: list[tuple[int, int]], pids: dict[str, int]) -> AgentManager:
    """An AgentManager whose OOM prioritizer captures its band writes.

    The prioritizer collaborator is swapped for one wired to a fake pid resolver
    and a capturing ``set_adj`` (mirrors how other tests seed ``_agents``), so a
    POST to the presence route drives the real endpoint -> ``record_presence`` ->
    prioritizer -> ``get_chat_agent_ids`` -> ``set_adj`` path without touching
    ``/proc``.
    """
    manager = AgentManager.build(WebSocketBroadcaster())
    manager._oom_prioritizer = ChatOomPrioritizer(
        list_chat_agent_ids=manager.get_chat_agent_ids,
        resolve_pid=lambda cid: pids.get(cid),
        set_adj=lambda pid, adj: (writes.append((pid, adj)), True)[1],
        # No process-start marker in this fake, so the chat's idle time comes from
        # the reported presence alone -- which is what these tests are about.
        resolve_process_started_at=lambda _cid: None,
    )
    return manager


def _client_with_tracked_chat(writes: list[tuple[int, int]], agent_id: str, pid: int) -> FlaskClient:
    manager = _manager_with_capturing_prioritizer(writes, pids={agent_id: pid})
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id, name="chat", state="RUNNING", labels={"user_created": "true"}, work_dir=None
        )
    return create_application(build_test_state(agent_manager=manager)).test_client()


def test_presence_endpoint_retags_a_chat_from_the_report() -> None:
    """A visible report flows through to re-tag the reported chat's band."""
    writes: list[tuple[int, int]] = []
    client = _client_with_tracked_chat(writes, "agent-c0ffee", 4242)

    response = client.post("/api/agents/agent-c0ffee/presence", json={"client_id": "client-1", "state": "visible"})

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    # Open + visible, never messaged -> the open-and-visible chat band.
    assert writes == [
        (
            4242,
            bands.chat_agent_oom_score_adj(
                is_open=True, is_visible=True, recency_rank=None, idle_seconds=0.0, is_mid_turn=False
            ),
        )
    ]


def test_presence_endpoint_closed_report_releases_the_chat() -> None:
    """A ``closed`` report drops the client's presence, so the chat reads as closed again."""
    writes: list[tuple[int, int]] = []
    client = _client_with_tracked_chat(writes, "agent-c0ffee", 4242)
    client.post("/api/agents/agent-c0ffee/presence", json={"client_id": "client-1", "state": "hidden"})
    open_adj = writes[-1][1]

    response = client.post("/api/agents/agent-c0ffee/presence", json={"client_id": "client-1", "state": "closed"})

    assert response.status_code == 200
    assert writes[-1][1] > open_adj
    assert writes[-1][1] == bands.chat_agent_oom_score_adj(
        is_open=False, is_visible=False, recency_rank=None, idle_seconds=None, is_mid_turn=False
    )


def test_presence_endpoint_rejects_a_malformed_report() -> None:
    writes: list[tuple[int, int]] = []
    client = _client_with_tracked_chat(writes, "agent-c0ffee", 4242)

    response = client.post("/api/agents/agent-c0ffee/presence", json={"client_id": "client-1", "state": "gone"})

    assert response.status_code == 400
    assert "detail" in response.get_json()
    assert writes == []


def test_presence_endpoint_refuses_an_id_that_is_not_an_agent_id() -> None:
    writes: list[tuple[int, int]] = []
    client = _client_with_tracked_chat(writes, "agent-c0ffee", 4242)

    response = client.post("/api/agents/not-an-agent/presence", json={"client_id": "client-1", "state": "visible"})

    assert response.status_code == 404


def test_send_records_the_message_for_the_chats_recency() -> None:
    """The send route stamps the chat as just-messaged."""
    writes: list[tuple[int, int]] = []
    agent_id = "agent-c0ffee00000000000000000000c0ffee"
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=RecordingMngrMessenger())
    manager._oom_prioritizer = ChatOomPrioritizer(
        list_chat_agent_ids=manager.get_chat_agent_ids,
        resolve_pid=lambda cid: {agent_id: 4242}.get(cid),
        set_adj=lambda pid, adj: (writes.append((pid, adj)), True)[1],
        resolve_process_started_at=lambda _cid: None,
    )
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id, name="chat", state="RUNNING", labels={"user_created": "true"}, work_dir=None
        )
    client = create_application(build_test_state(agent_manager=manager)).test_client()
    agent_info = AgentInfo(
        id=agent_id,
        name="chat",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post(f"/api/agents/{agent_id}/message", json={"message": "hello"})

    assert response.status_code == 200
    assert writes[-1] == (
        4242,
        bands.chat_agent_oom_score_adj(
            is_open=False, is_visible=False, recency_rank=0, idle_seconds=0.0, is_mid_turn=False
        ),
    )


def test_interrupt_agent_returns_404_for_unknown_agent(client: FlaskClient) -> None:
    """Interrupting a nonexistent agent returns 404."""
    with patch("imbue.system_interface.chat_document._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/interrupt")
    assert response.status_code == 404


def test_interrupt_agent_success(client: FlaskClient) -> None:
    """Interrupting an agent restarts it via mngr and returns 200."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=0,
        stdout="Restarted agent: claude-agent",
        stderr="",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version",
            return_value=fake_result,
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state") as mock_reset,
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert mock_run.call_args.kwargs["command"] == [
        "mngr",
        "start",
        "claude-agent",
        "--restart",
        "--no-resume",
    ]
    # After a successful restart the endpoint resets the agent's activity
    # state so the indicator clears instead of staying pinned at THINKING.
    mock_reset.assert_called_once_with("agent-123")


def test_interrupt_agent_rejects_is_primary_agent(client: FlaskClient) -> None:
    """POST /api/agents/<id>/interrupt returns 400 for the services agent.

    Restarting the is_primary agent would stop the workspace services. The
    frontend hides such agents; this server-side guard protects direct callers.
    """
    services_agent = AgentInfo(
        id="services-1",
        name="system-services",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
        labels={"is_primary": "true", "workspace": "my-ws"},
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=services_agent),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/services-1/interrupt")

    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs before the restart subprocess, so mngr is never invoked.
    mock_run.assert_not_called()


def test_interrupt_agent_returns_500_on_failure(client: FlaskClient) -> None:
    """If the mngr restart command exits non-zero, return 500 with its stderr."""
    agent_info = AgentInfo(
        id="agent-123",
        name="claude-agent",
        state="RUNNING",
        agent_state_dir=Path("/tmp/test"),
        claude_config_dir=Path("/tmp/.claude"),
    )
    fake_result = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="mngr start failed",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version",
            return_value=fake_result,
        ),
    ):
        response = client.post("/api/agents/agent-123/interrupt")

    assert response.status_code == 500
    assert response.get_json()["detail"] == "Failed to interrupt agent 'claude-agent': mngr start failed"


def _agent_info(
    agent_id: str = "agent-00000000000000000000000000000001",
    name: str = "claude-agent",
    labels: dict[str, str] | None = None,
    harness: HarnessType = HarnessType.CLAUDE,
    agent_state_dir: Path = Path("/tmp/test"),
    claude_config_dir: Path = Path("/tmp/.claude"),
) -> AgentInfo:
    return AgentInfo(
        id=agent_id,
        name=name,
        state="RUNNING",
        agent_state_dir=agent_state_dir,
        claude_config_dir=claude_config_dir,
        labels=labels if labels is not None else {},
        harness=harness,
    )


def _restart_ok() -> FinishedProcess:
    return FinishedProcess(
        returncode=0,
        stdout="Restarted agent: claude-agent",
        stderr="",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )


def _fake_queue_watcher(
    block: str,
    events: list[dict[str, Any]] | None = None,
    events_after_clear: list[dict[str, Any]] | None = None,
    in_flight_block: str = "",
) -> SimpleNamespace:
    """A stand-in watcher exposing just the queue methods the endpoints call.

    ``clear_calls`` records each ``clear_queue`` invocation so a test can assert
    the tracked set was cleared, without pulling in ``unittest.mock``. ``method_calls``
    records the ordered method names so a test can assert the native overrides refresh
    (``get_all_events``) BEFORE they capture the block (``get_queued_block``). ``events``
    is what ``get_all_events`` returns -- empty by default (pi ignores the value; codex
    reads the turn markers from it), or open/closed-turn markers for a codex drain test.
    ``events_after_clear``, when set, is what ``get_all_events`` returns once ``clear_queue``
    has run -- scripting the patched codex binary's abort landing in the rollout so the
    stop's post-retract marker settle sees the turn end on its first poll.
    """
    clear_calls: list[bool] = []
    method_calls: list[str] = []

    def _clear() -> None:
        method_calls.append("clear_queue")
        clear_calls.append(True)

    def _get_all_events() -> list[dict[str, Any]]:
        method_calls.append("get_all_events")
        if clear_calls and events_after_clear is not None:
            return events_after_clear
        return events if events is not None else []

    def _get_queued_block() -> str:
        method_calls.append("get_queued_block")
        return block

    def _get_in_flight_block() -> str:
        method_calls.append("get_in_flight_block")
        return in_flight_block

    return SimpleNamespace(
        get_all_events=_get_all_events,
        get_queued_block=_get_queued_block,
        get_in_flight_block=_get_in_flight_block,
        clear_queue=_clear,
        clear_calls=clear_calls,
        method_calls=method_calls,
    )


def test_flush_queue_returns_404_for_unknown_agent(client: FlaskClient) -> None:
    with patch("imbue.system_interface.chat_document._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/flush-queue")
    assert response.status_code == 404


def test_flush_queue_restarts_and_resends_the_concatenated_block(client: FlaskClient) -> None:
    """Shoulder tap restarts the agent, clears the tracked set, and resends one combined turn."""
    fake_watcher = _fake_queue_watcher("first message\nsecond message")
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=_agent_info()),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version", return_value=_restart_ok()
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state"),
        patch.object(AgentManager, "send_message_to_agent", return_value=None) as mock_send,
    ):
        response = client.post("/api/agents/agent-123/flush-queue")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    assert mock_run.call_args.kwargs["command"] == ["mngr", "start", "claude-agent", "--restart", "--no-resume"]
    # Resent as ONE combined turn, in enqueue order.
    assert mock_send.call_count == 1
    assert mock_send.call_args.args[1] == "first message\nsecond message"
    assert fake_watcher.clear_calls == [True]


def test_flush_queue_is_a_noop_when_the_queue_is_empty(client: FlaskClient) -> None:
    """A flush with nothing queued neither restarts nor resends -- a clean 200."""
    fake_watcher = _fake_queue_watcher("")
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=_agent_info()),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
        patch.object(AgentManager, "send_message_to_agent") as mock_send,
    ):
        response = client.post("/api/agents/agent-123/flush-queue")

    assert response.status_code == 200
    mock_run.assert_not_called()
    mock_send.assert_not_called()


def test_flush_queue_rejects_is_primary_agent(client: FlaskClient) -> None:
    with (
        patch(
            "imbue.system_interface.chat_document._find_agent",
            return_value=_agent_info(agent_id="services-1", name="system-services", labels={"is_primary": "true"}),
        ),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/services-1/flush-queue")

    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    mock_run.assert_not_called()


def test_flush_queue_returns_500_on_restart_failure(client: FlaskClient) -> None:
    fake_watcher = _fake_queue_watcher("queued text")
    failed = FinishedProcess(
        returncode=1,
        stdout="",
        stderr="mngr start failed",
        command=("mngr", "start", "claude-agent", "--restart", "--no-resume"),
        is_output_already_logged=False,
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=_agent_info()),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version", return_value=failed),
        patch.object(AgentManager, "send_message_to_agent") as mock_send,
    ):
        response = client.post("/api/agents/agent-123/flush-queue")

    assert response.status_code == 500
    # The restart failed, so nothing is resent.
    mock_send.assert_not_called()


def test_shoulder_tap_atomic_returns_404_for_unknown_agent(client: FlaskClient) -> None:
    with patch("imbue.system_interface.chat_document._find_agent", return_value=None):
        response = client.post("/api/agents/nonexistent/shoulder-tap-atomic")
    assert response.status_code == 404


def test_shoulder_tap_atomic_rejects_non_atomic_harness(client: FlaskClient, tmp_path: Path) -> None:
    """A harness whose catalog reports no native tap gets a 400 with a clear message and no write.

    All shipping harnesses now support the atomic tap, so this exercises the defensive branch
    for a hypothetical future non-atomic harness by forcing the catalog flag off.
    """
    agent_info = _agent_info(name="codex-agent", harness=HarnessType.CODEX, agent_state_dir=tmp_path)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch(
            "imbue.system_interface.chat_document.get_catalog",
            return_value=SimpleNamespace(native_atomic_shoulder_tap_possible=False),
        ),
    ):
        response = client.post("/api/agents/agent-123/shoulder-tap-atomic")

    assert response.status_code == 400
    assert "does not support an atomic shoulder tap" in response.get_json()["detail"]


class _FakeClaudeTapWatcher:
    """A claude watcher stand-in for the shoulder-tap arm: scripts the mirror + session growth.

    Records ``clear_queue`` calls (there must be none -- the native tap never clears the mirror).
    """

    def __init__(
        self,
        queue_snapshots: list[list[dict[str, str]]],
        session_file: Path | None = None,
        answer_on_refresh: bool = False,
    ) -> None:
        self._queue_snapshots = queue_snapshots
        self._session_file = session_file
        self._answer_on_refresh = answer_on_refresh
        self._events_calls = 0
        self._queue_calls = 0
        self.clear_calls: list[bool] = []

    def get_all_events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        self._events_calls += 1
        if self._answer_on_refresh and self._events_calls == 2 and self._session_file is not None:
            with self._session_file.open("a") as f:
                f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "ok"}}) + "\n")
        return []

    def get_queued_messages(self) -> list[dict[str, str]]:
        index = min(self._queue_calls, len(self._queue_snapshots) - 1)
        self._queue_calls += 1
        return self._queue_snapshots[index]

    def get_latest_main_session_file(self) -> Path | None:
        return self._session_file

    def clear_queue(self) -> None:
        self.clear_calls.append(True)


def _claude_tap_dirs(tmp_path: Path) -> tuple[Path, Path]:
    """State dir with the active + process-started markers and a config dir with an active binding."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    keybindings = config_dir / "keybindings.json"
    keybindings.write_text(json.dumps({"bindings": [{"context": "Chat", "bindings": {"meta+q": "chat:cancel"}}]}))
    marker = state_dir / "claude_process_started"
    marker.write_text("")
    os.utime(keybindings, (1000, 1000))
    os.utime(marker, (2000, 2000))
    (state_dir / "active").write_text("")
    return state_dir, config_dir


def test_shoulder_tap_atomic_claude_nothing_queued_is_a_noop(client: FlaskClient, tmp_path: Path) -> None:
    """An empty claude mirror short-circuits to nothing_queued, never restarting the agent."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    agent_info = _agent_info(agent_state_dir=state_dir, claude_config_dir=config_dir)
    watcher = _FakeClaudeTapWatcher([[]])
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/agent-123/shoulder-tap-atomic")

    assert response.status_code == 200
    assert response.get_json()["status"] == "nothing_queued"
    mock_run.assert_not_called()
    assert watcher.clear_calls == []


def test_shoulder_tap_atomic_claude_flushed_presses_chord_and_never_restarts(
    client: FlaskClient, tmp_path: Path
) -> None:
    """A claude tap flushes via the meta+q chord (routed through mngr), never restarting or clearing."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    agent_id = "agent-00000000000000000000000000000042"
    agent_info = _agent_info(agent_id=agent_id, agent_state_dir=state_dir, claude_config_dir=config_dir)
    watcher = _FakeClaudeTapWatcher([[{"queued_id": "q1", "content": "hi"}], []], session, answer_on_refresh=True)
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    app = create_application(build_test_state(agent_manager=manager))
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = app.test_client().post(f"/api/agents/{agent_id}/shoulder-tap-atomic")

    assert response.status_code == 200
    assert response.get_json()["status"] == "tapped"
    # The chord is delivered via mngr's locked keypress -- never a raw restart, never a clear.
    mock_run.assert_not_called()
    assert messenger.pressed == [(agent_id, "M-q")]
    assert watcher.clear_calls == []


def test_shoulder_tap_atomic_claude_no_ops_benignly_when_a_send_is_in_flight(
    client: FlaskClient, tmp_path: Path
) -> None:
    """claude's tap takes the refresh-first mirror read under the same ``message.lock`` a send
    holds: with a send in flight past the bounded wait it flushes nothing -- never pressing the
    chord or clearing the mirror (the codex/pi discipline). But that refusal is a benign 200
    no-op, not a 500: the backend availability flag greys the button whenever a send is in flight,
    so a tap that still races one simply does nothing and the user retaps -- surfacing an error
    there is the button-then-error bug we removed."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    agent_id = "agent-00000000000000000000000000000042"
    agent_info = _agent_info(agent_id=agent_id, agent_state_dir=state_dir, claude_config_dir=config_dir)
    watcher = _FakeClaudeTapWatcher([[{"queued_id": "q1", "content": "hi"}], []])
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    app = create_application(build_test_state(agent_manager=manager))
    with (
        _hold_message_lock(state_dir),
        patch("imbue.system_interface.harnesses.interrupt.STOP_LOCK_WAIT_SECONDS", 0.1),
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = app.test_client().post(f"/api/agents/{agent_id}/shoulder-tap-atomic")

    assert response.status_code == 200
    assert response.get_json()["status"] == "send_in_flight"
    # No chord delivered, no restart, no mirror clear: the tap refused cleanly, just without erroring.
    assert messenger.pressed == []
    mock_run.assert_not_called()
    assert watcher.clear_calls == []


def test_shoulder_tap_atomic_writes_sentinel_for_pi(client: FlaskClient, tmp_path: Path) -> None:
    """A pi agent gets one interrupt sentinel appended to its inbox (a JSON object, so the queue
    watcher ignores it), the status is ``tapped``, and the agent is NOT restarted."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/agent-123/shoulder-tap-atomic")

    assert response.status_code == 200
    assert response.get_json()["status"] == "tapped"
    mock_run.assert_not_called()
    lines = (tmp_path / "pi_inbox").read_text().splitlines()
    assert lines == ['{"minds_interrupt": true}']


def test_shoulder_tap_atomic_rejects_is_primary_agent(client: FlaskClient, tmp_path: Path) -> None:
    agent_info = _agent_info(
        agent_id="services-1",
        name="system-services",
        labels={"is_primary": "true"},
        harness=HarnessType.CODEX,
        agent_state_dir=tmp_path,
    )
    with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
        response = client.post("/api/agents/services-1/shoulder-tap-atomic")

    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]


def test_shoulder_tap_atomic_pi_no_ops_benignly_when_a_send_is_in_flight(client: FlaskClient, tmp_path: Path) -> None:
    """The pi flush writer takes the same ``message.lock`` a send holds: with a send in flight
    past the bounded wait, no sentinel is written -- but that refusal is a benign 200 no-op, not
    a 500. The backend availability flag greys the button whenever a send is in flight, so a tap
    that still races one simply does nothing (the queue is unchanged) and the user retaps."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    with (
        _hold_message_lock(tmp_path),
        patch("imbue.system_interface.harnesses.interrupt.STOP_LOCK_WAIT_SECONDS", 0.1),
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
    ):
        response = client.post("/api/agents/agent-123/shoulder-tap-atomic")

    assert response.status_code == 200
    assert response.get_json()["status"] == "send_in_flight"
    # No sentinel written -- the flush refused cleanly, just without erroring.
    assert not (tmp_path / "pi_inbox").exists()


def _fake_claude_interrupt_watcher(
    *,
    block: str,
    queued: list[dict[str, Any]],
    session_file: Path | None = None,
    append_on_second_refresh: str | None = None,
    in_flight_block: str = "",
) -> SimpleNamespace:
    """A claude-shaped watcher stand-in for the stop override: mirror + session + block methods.

    ``get_queued_messages`` drives the empty/non-empty branch; ``get_latest_main_session_file``
    anchors the abort watch. When ``append_on_second_refresh`` is set, that raw line is appended
    to ``session_file`` on the SECOND ``get_all_events`` (the under-lock re-check, after the
    baseline) so the abort watch reads it as post-baseline evidence.
    """
    state = {"events": 0}
    clear_calls: list[bool] = []

    def _get_all_events(session_id: str | None = None) -> list[dict[str, Any]]:
        state["events"] += 1
        if state["events"] == 2 and append_on_second_refresh is not None and session_file is not None:
            with session_file.open("a") as handle:
                handle.write(append_on_second_refresh + "\n")
        return []

    return SimpleNamespace(
        get_all_events=_get_all_events,
        get_queued_messages=lambda: list(queued),
        get_queued_block=lambda: block,
        get_latest_main_session_file=lambda: session_file,
        get_in_flight_block=lambda: in_flight_block,
        clear_queue=lambda: clear_calls.append(True),
        clear_calls=clear_calls,
    )


def test_drain_to_composer_claude_nonempty_queue_delegates_to_base_restart(
    client: FlaskClient, tmp_path: Path
) -> None:
    """A NONEMPTY claude queue keeps the base restart-drain: restart, hand the block back unsent,
    clear the mirror -- a chord there would commit the very messages stop promises to retract."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    agent_info = _agent_info(agent_state_dir=state_dir, claude_config_dir=config_dir)
    fake_watcher = _fake_claude_interrupt_watcher(
        block="edit me before sending", queued=[{"queued_id": "q1", "content": "edit me before sending"}]
    )
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version", return_value=_restart_ok()
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state"),
        patch.object(AgentManager, "send_message_to_agent") as mock_send,
    ):
        response = client.post("/api/agents/agent-123/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == "edit me before sending"
    assert mock_run.call_args.kwargs["command"] == ["mngr", "start", "claude-agent", "--restart", "--no-resume"]
    # The block is handed back, never sent.
    mock_send.assert_not_called()
    assert fake_watcher.clear_calls == [True]


def test_drain_to_composer_claude_empty_queue_uses_the_chord_not_a_restart(tmp_path: Path) -> None:
    """Replaces the pi plan's pinned claude-empty-queue-restarts test: a claude stop mid-turn with
    NOTHING queued now interrupts via the meta+q chord (routed through mngr), confirms the abort by
    the interrupt sentinel, marks the stranded agent idle, and returns '' -- never restarting."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    agent_id = "agent-00000000000000000000000000000042"
    agent_info = _agent_info(agent_id=agent_id, agent_state_dir=state_dir, claude_config_dir=config_dir)
    # The mid-tool sentinel shape (the dominant stop scenario), appended past the baseline.
    sentinel = json.dumps(
        {"type": "user", "message": {"role": "user", "content": "[Request interrupted by user for tool use]"}}
    )
    fake_watcher = _fake_claude_interrupt_watcher(
        block="", queued=[], session_file=session, append_on_second_refresh=sentinel
    )
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    app = create_application(build_test_state(agent_manager=manager))
    idle_marks: list[bool] = []
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
        patch(
            "imbue.system_interface.harnesses.claude.tap.mark_claude_agent_idle",
            side_effect=lambda *_a, **_k: idle_marks.append(True),
        ),
    ):
        response = app.test_client().post(f"/api/agents/{agent_id}/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == ""
    # Interrupted via the chord (routed through mngr's locked keypress), never a restart.
    mock_run.assert_not_called()
    assert messenger.pressed == [(agent_id, "M-q")]
    # The stranded active marker was cleared via the mngr_claude idle-marking primitive.
    assert idle_marks == [True]
    # Nothing was queued, so the mirror is not cleared here (the chord path leaves it alone).
    assert fake_watcher.clear_calls == []


def test_drain_to_composer_pi_appends_retract_sentinel_and_returns_block(client: FlaskClient, tmp_path: Path) -> None:
    """pi's native override: append the retract sentinel to pi_inbox, hand the block back, and do
    NOT restart the agent."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    fake_watcher = _fake_queue_watcher("bring me back to edit")
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/agent-123/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == "bring me back to edit"
    # Native retract -> no restart.
    mock_run.assert_not_called()
    lines = (tmp_path / "pi_inbox").read_text().splitlines()
    assert lines == ['{"minds_interrupt_retract": true}']
    assert fake_watcher.clear_calls == [True]
    # pi captures the block via ``get_queued_block``, which refreshes the mirror itself
    # (unlike codex's) -- so the running turn's own initiating message is popped by its own
    # landed leave with no separate refresh-first call.
    assert "get_queued_block" in fake_watcher.method_calls
    assert "get_all_events" not in fake_watcher.method_calls


def test_drain_to_composer_pi_empty_mirror_still_appends_and_returns_empty(
    client: FlaskClient, tmp_path: Path
) -> None:
    """A pi stop mid-turn with nothing queued still writes the retract sentinel (interrupting the
    bare turn -- fixes the empty-queue no-op) and returns '', still without a restart."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    fake_watcher = _fake_queue_watcher("")
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/agent-123/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == ""
    mock_run.assert_not_called()
    lines = (tmp_path / "pi_inbox").read_text().splitlines()
    assert lines == ['{"minds_interrupt_retract": true}']
    assert fake_watcher.clear_calls == [True]


def test_drain_to_composer_pi_native_retract_does_not_fold_in_flight_block(
    client: FlaskClient, tmp_path: Path
) -> None:
    """On the native (lock-HELD) retract path pi returns the queued block ALONE and does NOT fold
    the in-flight block, even if the registry reports one. Holding the lock means any send has
    already released it, so a just-parked message is in the queued block already; also folding the
    in-flight block would double-return a message caught in the post-lock-release/pre-commit window
    (in the queued block AND still in the registry). This mirrors claude's held branch."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    fake_watcher = _fake_queue_watcher("queued only", in_flight_block="must NOT be folded here")
    with (
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version") as mock_run,
    ):
        response = client.post("/api/agents/agent-123/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == "queued only"
    mock_run.assert_not_called()
    assert fake_watcher.clear_calls == [True]


def test_drain_to_composer_dispatches_per_harness(tmp_path: Path) -> None:
    """The stop button resolves the interrupt-to-composer implementation from the harness: pi to
    its own native override and claude to its native empty-queue chord override, each plugging in
    without disturbing the others. codex is not here: it is handled directly in the endpoint via
    its live ledger, so it never routes through ``build_interrupt_to_composer``."""
    pi = build_interrupt_to_composer(_agent_info(harness=HarnessType.PI_CODING, agent_state_dir=tmp_path))
    claude = build_interrupt_to_composer(_agent_info(harness=HarnessType.CLAUDE))
    assert isinstance(pi, PiInterruptToComposer)
    assert isinstance(claude, ClaudeInterruptToComposer)


@contextmanager
def _hold_message_lock(agent_state_dir: Path) -> Generator[None, None, None]:
    """Hold the agent's ``message.lock`` through a separate fd, as an in-flight mngr send does,
    so a concurrent stop's bounded acquire fails and it falls back to the restart hammer."""
    lock_path = agent_state_dir / "message.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(other.fileno(), fcntl.LOCK_UN)


def test_drain_to_composer_pi_falls_back_to_restart_when_a_send_is_in_flight(
    client: FlaskClient, tmp_path: Path
) -> None:
    """A send holding ``message.lock`` blocks pi's native retract past the bounded wait, so the
    stop falls back to the base restart hammer: it restarts and writes NO retract sentinel (which,
    unordered against the in-flight send, could strand that message). The SIGKILL aborts the
    in-flight send before it commits, so its text is FOLDED into the returned block (contract
    Interrupt/A4: return every not-Delivered message) -- queued block first, then the still-in-
    flight send -- rather than being lost."""
    agent_info = _agent_info(name="pi-agent", harness=HarnessType.PI_CODING, agent_state_dir=tmp_path)
    fake_watcher = _fake_queue_watcher("bring me back to edit")
    in_flight_session = _file_session_for(agent_info, in_flight="a message still sending")
    with (
        _hold_message_lock(tmp_path),
        patch("imbue.system_interface.harnesses.interrupt.STOP_LOCK_WAIT_SECONDS", 0.1),
        patch.object(AgentManager, "get_or_create_session", return_value=in_flight_session),
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version", return_value=_restart_ok()
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state"),
    ):
        response = client.post("/api/agents/agent-123/drain-to-composer")

    assert response.status_code == 200
    # The queued block leads, the still-in-flight send follows (send order) -- the in-flight
    # message rides the block instead of dying silently with the SIGKILL.
    assert response.get_json()["block"] == "bring me back to edit\na message still sending"
    # The hammer fell: a restart ran, and NO native sentinel was written.
    assert mock_run.call_args.kwargs["command"] == ["mngr", "start", "pi-agent", "--restart", "--no-resume"]
    assert not (tmp_path / "pi_inbox").exists()
    assert fake_watcher.clear_calls == [True]


def test_drain_to_composer_claude_falls_back_to_restart_when_a_send_is_in_flight(tmp_path: Path) -> None:
    """A send holding ``message.lock`` past the bounded wait blocks claude's chord path, so the
    stop falls back to the base restart hammer instead of stalling behind the send's turn-confirm:
    it restarts, hands the (empty) block back, and delivers NO chord."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    agent_id = "agent-00000000000000000000000000000042"
    agent_info = _agent_info(agent_id=agent_id, agent_state_dir=state_dir, claude_config_dir=config_dir)
    fake_watcher = _fake_claude_interrupt_watcher(block="", queued=[], session_file=session)
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    app = create_application(build_test_state(agent_manager=manager))
    with (
        _hold_message_lock(state_dir),
        patch("imbue.system_interface.harnesses.interrupt.STOP_LOCK_WAIT_SECONDS", 0.1),
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch(
            "imbue.system_interface.chat_document.run_local_command_modern_version", return_value=_restart_ok()
        ) as mock_run,
        patch.object(AgentManager, "reset_activity_state"),
    ):
        response = app.test_client().post(f"/api/agents/{agent_id}/drain-to-composer")

    assert response.status_code == 200
    assert response.get_json()["block"] == ""
    # The hammer fell: a restart ran, and NO chord was delivered.
    assert mock_run.call_args.kwargs["command"] == ["mngr", "start", "claude-agent", "--restart", "--no-resume"]
    assert messenger.pressed == []
    assert fake_watcher.clear_calls == [True]


def test_drain_to_composer_claude_returns_in_flight_send_when_the_lock_stays_held(tmp_path: Path) -> None:
    """A send still in flight when stop fires (message.lock held past the bounded wait) is aborted
    by the hammer and returned to the composer, not lost -- the endpoint hands its text back in the
    block (contract A4/B: return every not-Delivered message)."""
    state_dir, config_dir = _claude_tap_dirs(tmp_path)
    session = tmp_path / "session.jsonl"
    session.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n")
    agent_id = "agent-00000000000000000000000000000043"
    agent_info = _agent_info(agent_id=agent_id, agent_state_dir=state_dir, claude_config_dir=config_dir)
    fake_watcher = _fake_claude_interrupt_watcher(block="", queued=[], session_file=session)
    messenger = RecordingMngrMessenger()
    manager = AgentManager.build(WebSocketBroadcaster(), messenger=messenger)
    in_flight_session = manager.get_or_create_session(agent_info)
    assert isinstance(in_flight_session, FileHarnessSession)
    in_flight_session._sending.record("t-in-flight", "message caught mid-send")
    app = create_application(build_test_state(agent_manager=manager))
    with (
        _hold_message_lock(state_dir),
        patch("imbue.system_interface.harnesses.interrupt.STOP_LOCK_WAIT_SECONDS", 0.1),
        patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info),
        patch.object(SystemInterfaceState, "get_or_create_watcher", return_value=fake_watcher),
        patch("imbue.system_interface.chat_document.run_local_command_modern_version", return_value=_restart_ok()),
        patch.object(AgentManager, "reset_activity_state"),
    ):
        response = app.test_client().post(f"/api/agents/{agent_id}/drain-to-composer")

    assert response.status_code == 200
    # The in-flight send is recovered to the composer instead of dying silently with the SIGKILL.
    assert response.get_json()["block"] == "message caught mid-send"
    assert messenger.pressed == []


def test_get_or_create_watcher_seeds_activity_before_starting_the_watcher() -> None:
    """Transcript-signal seeding runs BEFORE the watcher thread starts.

    The watcher's priming pass can push a replayed queued-message snapshot as
    soon as its thread runs, and the manager's pre-broadcast sweep derives
    activity from the seeded signals -- an unseeded tracker derives IDLE even
    for a live mid-turn agent, so seeding after ``start`` would let that first
    snapshot sweep a genuine queue. ``get_all_events`` reads synchronously, so
    seeding needs no running watcher thread.
    """
    calls: list[str] = []

    def _record_get_all_events() -> list[dict[str, Any]]:
        calls.append("get_all_events")
        return []

    fake_watcher = SimpleNamespace(
        set_queue_snapshot_callback=lambda _callback: None,
        notify_idle=lambda: [],
        set_flush_hooks=lambda _send, _is_alive: None,
        get_all_events=_record_get_all_events,
        start=lambda: calls.append("start"),
    )
    state = build_test_state()
    with patch("imbue.system_interface.app_context.build_watcher", return_value=fake_watcher):
        state.get_or_create_watcher(_agent_info())

    assert "get_all_events" in calls and "start" in calls
    assert calls.index("get_all_events") < calls.index("start")


def test_create_chat_agent_without_work_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    """Creating a chat agent without a primary agent work dir returns 400."""
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_client = create_application(build_test_state()).test_client()
    response = test_client.post(
        "/api/agents/create-chat",
        json={"name": "test-chat"},
    )
    assert response.status_code == 400


def test_create_chat_mints_a_numbered_display_name_server_side(
    client: FlaskClient, app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create with no name gets the first free "Chat N", counted against the machine's agents.

    "Chat 1" is a live agent's display label, so the mint lands on "Chat 2"; the response
    carries the pair.
    """
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    _register_agent(app, "agent-123", "primary", "RUNNING")
    agent_manager: AgentManager = state_of(app).agent_manager
    with agent_manager._lock:
        agent_manager._agents["agent-1"] = AgentStateItem(
            id="agent-1", name="Chat-1", state="RUNNING", labels={"display_name": "Chat 1"}, work_dir=None
        )

    response = client.post("/api/agents/create-chat", json={})

    assert response.status_code == 201
    body = response.get_json()
    assert body["display_name"] == "Chat 2"
    assert body["name"] == "Chat-2"
    assert body["agent_id"]


def test_create_chat_rejects_a_conflicting_explicit_name_with_a_409(
    client: FlaskClient, app: Flask, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly requested name that collides answers 409, so the caller can
    retry with another name instead of watching the background create fail."""
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-123")
    _register_agent(app, "agent-123", "primary", "RUNNING")
    agent_manager: AgentManager = state_of(app).agent_manager
    with agent_manager._lock:
        agent_manager._agents["agent-1"] = AgentStateItem(
            id="agent-1", name="Chat-2", state="RUNNING", labels={"display_name": "Chat 2"}, work_dir=None
        )

    response = client.post("/api/agents/create-chat", json={"name": "chat 2"})

    assert response.status_code == 409
    assert "chat 2" in response.get_json()["detail"]


# The initial snapshot crosses a real threaded server on the loopback; under CI load it has
# been seen arriving after the default pytest-timeout, so the test gets the receive helper's
# own budget and a retry.
@pytest.mark.flaky
@pytest.mark.timeout(15)
def test_websocket_endpoint_sends_initial_snapshot(app: Flask) -> None:
    """On connect the socket sends the shell's inventory and projects, then the chat carve-out's agent list."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(3)]
        finally:
            close_ws(ws)

    assert [message["type"] for message in messages] == ["apps_updated", "projects_updated", "agents_updated"]
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


def test_get_events_seeds_pending_tool_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Hitting /api/agents/{id}/events for a Claude session with an unmatched tool_use
    seeds the AgentManager's transcript-derived signals so the activity indicator
    reads ``TOOL_RUNNING`` immediately.
    """
    agent_id = "agent-pending-tool"
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path))
    monkeypatch.setenv("MNGR_AGENT_ID", agent_id)
    monkeypatch.setenv("MNGR_AGENT_WORK_DIR", str(tmp_path / "work"))

    state_dir = tmp_path / "agents" / agent_id
    state_dir.mkdir(parents=True)

    claude_config_dir = tmp_path / "claude_config"
    projects_dir = claude_config_dir / "projects" / "hash123"
    projects_dir.mkdir(parents=True)
    session_id = "test-session-id"
    session_file = projects_dir / f"{session_id}.jsonl"
    # An assistant message that includes a tool_use, with no matching tool_result.
    session_file.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "uuid-1",
                "timestamp": "2026-01-01T00:00:00Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-6",
                    "content": [
                        {"type": "text", "text": "running a command"},
                        {"type": "tool_use", "id": "call_a", "name": "Bash", "input": {"command": "ls"}},
                    ],
                    "stop_reason": "tool_use",
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        + "\n"
    )
    (state_dir / "claude_session_id_history").write_text(f"{session_id}\n")

    broadcaster = WebSocketBroadcaster()
    manager = AgentManager.build(broadcaster)
    with manager._lock:
        manager._agents[agent_id] = AgentStateItem(
            id=agent_id,
            name="seed-agent",
            state="RUNNING",
            labels={},
            work_dir=str(tmp_path / "work"),
        )
    manager._ensure_activity_tracking(agent_id)

    app = create_application(build_test_state(agent_manager=manager))
    agent_info = AgentInfo(
        id=agent_id,
        name="seed-agent",
        state="RUNNING",
        agent_state_dir=state_dir,
        claude_config_dir=claude_config_dir,
    )

    try:
        test_client = app.test_client()
        with patch("imbue.system_interface.chat_document._find_agent", return_value=agent_info):
            response = test_client.get(f"/api/agents/{agent_id}/events")
        assert response.status_code == 200

        # The watcher creation path seeds transcript-derived state
        # synchronously. Assert before ``stop()``, which clears these
        # caches alongside the marker watchers.
        with manager._lock:
            tracker = manager._activity_tracker_by_agent[agent_id]
            assert (
                tracker.derive(lifecycle_state="RUNNING", is_active_marker_present=False, process_started_at=None)
                == ActivityState.TOOL_RUNNING
            )
            assert manager._activity_state_by_agent[agent_id] == ActivityState.TOOL_RUNNING
    finally:
        manager.stop()


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_not_found_sends_error_and_closes(app: Flask) -> None:
    """When the proto-agent is missing, the endpoint sends a structured not-found message and closes."""
    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/missing-agent/logs")
        try:
            payload = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)
    assert payload == {"done": True, "success": False, "error": "Proto-agent not found"}


@pytest.mark.timeout(15)
def test_proto_agent_logs_endpoint_streams_messages_until_sentinel(app: Flask) -> None:
    """The endpoint forwards real log lines and closes when the queue yields ``None``."""
    log_queue: queue.Queue[str | None] = queue.Queue()
    log_queue.put(json.dumps({"line": "starting"}))
    log_queue.put(json.dumps({"line": "still going"}))
    log_queue.put(None)

    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._log_queues["proto-1"] = log_queue

    with serve_app(app) as served:
        ws = open_ws(served, "/api/proto-agents/proto-1/logs")
        try:
            first = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
            second = json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT))
        finally:
            close_ws(ws)

    assert first == {"line": "starting"}
    assert second == {"line": "still going"}


def test_stream_filtered_events_forwards_only_matching_events() -> None:
    """The shared stream loop yields only events that pass its predicate.

    This is the wiring behind Bug 2: the main stream forwards main-session
    events and drops subagent-session events, which share the same per-agent
    queue. A queued ``None`` ends the stream, keeping the test deterministic.
    """
    event_queues = AgentEventQueues()
    event_queue = event_queues.register("agent-1")

    # Subagent event first so a missing filter would forward it before the main one.
    event_queue.put({"event_id": "sub-evt", "session_id": "agent-sub"})
    event_queue.put({"event_id": "main-evt", "session_id": "main-1"})
    # Plugin/app events have no session_id and must still pass through.
    event_queue.put({"event_id": "no-session"})
    event_queue.put(None)

    def is_main_session_event(event: dict[str, object]) -> bool:
        session_id = event.get("session_id")
        return session_id is None or session_id == "main-1"

    frames = list(_stream_filtered_events("agent-1", event_queues, event_queue, is_main_session_event))
    forwarded_ids = [json.loads(frame[len("data: ") :])["event_id"] for frame in frames if frame.startswith("data: ")]

    assert forwarded_ids == ["main-evt", "no-session"]
    assert "sub-evt" not in forwarded_ids


def test_destroy_rejects_is_primary_agent(client: FlaskClient, app: Flask) -> None:
    """POST /api/agents/<id>/destroy returns 400 for the services agent.

    The frontend already hides agents carrying ``is_primary=true``; this
    server-side guard prevents direct callers (curl, scripted use, etc.)
    from accidentally tearing down the workspace.
    """
    agent_manager: AgentManager = state_of(app).agent_manager
    services_agent = AgentStateItem(
        id="services-1",
        name="system-services",
        state="RUNNING",
        labels={"is_primary": "true", "workspace": "my-ws"},
        work_dir="/home/user/workspace",
    )
    agent_manager._agents[services_agent.id] = services_agent

    response = client.post(f"/api/agents/{services_agent.id}/destroy")
    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    # The guard runs *before* the destroy subprocess, so the agent is still
    # present in the agent manager's state.
    assert services_agent.id in agent_manager._agents


def _register_agent(app: Flask, agent_id: str, name: str, state: str) -> None:
    """Insert an agent into the AgentManager's state for endpoint tests."""
    agent_manager: AgentManager = state_of(app).agent_manager
    agent_manager._agents[agent_id] = AgentStateItem(
        id=agent_id,
        name=name,
        state=state,
        labels={},
        work_dir="/code",
    )


def test_start_unknown_agent_returns_404(client: FlaskClient) -> None:
    """POST /api/agents/<id>/start returns 404 for an unknown agent."""
    response = client.post("/api/agents/nonexistent/start")
    assert response.status_code == 404


def test_start_invokes_in_process_start_with_agent_name(client: FlaskClient, app: Flask) -> None:
    """The endpoint delegates to the in-process ``start_agent`` keyed by name.

    Opening a terminal must go through the same in-process mngr start path that
    messaging an agent uses, so the two cannot diverge. The endpoint therefore
    calls ``start_agent(<name>)`` rather than shelling out to ``mngr start``.
    """
    _register_agent(app, "agent-running", "running-agent", "RUNNING")

    with patch("imbue.system_interface.chat_document.start_agent") as mock_start:
        response = client.post("/api/agents/agent-running/start")

    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"
    mock_start.assert_called_once_with("running-agent")


def test_start_failure_returns_500(client: FlaskClient, app: Flask) -> None:
    """A failed start surfaces as a 500 carrying the mngr error message."""
    _register_agent(app, "agent-stopped", "stopped-agent", "STOPPED")

    with patch(
        "imbue.system_interface.chat_document.start_agent",
        side_effect=AgentStartError("stopped-agent", "boom"),
    ):
        response = client.post("/api/agents/agent-stopped/start")

    assert response.status_code == 500
    assert "boom" in response.get_json()["detail"]


def test_destroy_argv_accepted_by_live_cli() -> None:
    """Confront the ``mngr destroy`` argv with the live ``imbue.mngr.main.cli``
    tree, so a system/vendor/mngr rename of that subcommand/flag fails here at merge
    time rather than only surfacing at runtime."""
    assert_mngr_argv_valid(_build_chat_destroy_command("mngr", "demo"))


def test_stop_argv_accepted_by_live_cli() -> None:
    """The ``mngr stop`` argv, confronted with the live CLI tree exactly as the
    destroy argv is."""
    assert_mngr_argv_valid(_build_stop_command("demo"))


def test_stop_unknown_agent_returns_404(client: FlaskClient) -> None:
    """POST /api/agents/<id>/stop returns 404 for an unknown agent."""
    response = client.post("/api/agents/nonexistent/stop")
    assert response.status_code == 404


def test_stop_rejects_is_primary_agent(client: FlaskClient, app: Flask) -> None:
    """POST /api/agents/<id>/stop returns 400 for the services agent.

    Stopping the services agent would take down every supervised service in
    the workspace, so the endpoint refuses it exactly as destroy does -- and
    the guard runs before any subprocess, so the agent's tracked state is
    untouched.
    """
    agent_manager: AgentManager = state_of(app).agent_manager
    services_agent = AgentStateItem(
        id="services-stop-1",
        name="system-services",
        state="RUNNING",
        labels={"is_primary": "true", "workspace": "my-ws"},
        work_dir="/home/user/workspace",
    )
    agent_manager._agents[services_agent.id] = services_agent

    response = client.post(f"/api/agents/{services_agent.id}/stop")
    assert response.status_code == 400
    assert "is_primary" in response.get_json()["detail"]
    assert services_agent.id in agent_manager._agents


# -- Agent file serving (markdown images + download links) --------------------
#
# An agent writes a file and references its absolute on-disk path in markdown;
# the catch-all serves that file -- images inline so they render, any other file
# as a download. These exercise the catch-all dispatch end to end via the Flask
# test client.


def test_serves_image_at_its_absolute_path(client: FlaskClient, tmp_path: Path) -> None:
    """A request for an existing image file's absolute path streams its bytes inline."""
    image_path = tmp_path / "chart.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image_bytes
    # Inline (rendered), not a forced download.
    assert "attachment" not in response.headers.get("Content-Disposition", "")
    # Cached aggressively: filenames are unique per image by convention.
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_ignores_requested_at_cache_busting_query(client: FlaskClient, tmp_path: Path) -> None:
    """The frontend's per-message ``?requested_at=`` cache key is ignored server-side.

    The query string never reaches ``try_serve_file`` (Flask splits it off before
    routing), so a request carrying it serves the same file with the same headers
    as the bare path. It exists only to make the browser treat each message's URL
    as distinct so a new message never renders a stale cached copy.
    """
    image_path = tmp_path / "chart.png"
    image_path.write_bytes(b"fake-png-bytes")

    tagged = client.get(f"{image_path}?requested_at=2026-07-24T00%3A00%3A00Z")

    assert tagged.status_code == 200
    assert tagged.content_type == "image/png"
    assert tagged.data == b"fake-png-bytes"
    assert tagged.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serves_image_in_nested_subdirectory(client: FlaskClient, tmp_path: Path) -> None:
    """Nested paths under the write directory are served (agents may organize per run)."""
    nested_dir = tmp_path / "images" / "run-3"
    nested_dir.mkdir(parents=True)
    image_path = nested_dir / "diagram.webp"
    image_path.write_bytes(b"fake-webp-bytes")

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/webp"


def test_serves_image_with_uppercase_extension(client: FlaskClient, tmp_path: Path) -> None:
    """Image extensions are matched case-insensitively."""
    image_path = tmp_path / "SHOT.PNG"
    image_path.write_bytes(b"fake-png-bytes")

    response = client.get(str(image_path))

    assert response.status_code == 200
    assert response.content_type == "image/png"


def test_serves_svg_with_hardened_headers(client: FlaskClient, tmp_path: Path) -> None:
    """SVG is served as an image but locked down for direct navigation."""
    image_path = tmp_path / "plot.svg"
    image_path.write_bytes(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>")

    response = client.get(str(image_path))

    assert response.status_code == 200
    # Werkzeug appends "; charset=utf-8" to the XML-based svg type; harmless.
    assert response.content_type.startswith("image/svg+xml")
    assert response.headers["Content-Security-Policy"] == "default-src 'none'; style-src 'unsafe-inline'"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_missing_image_path_returns_404_not_app_shell(client: FlaskClient, tmp_path: Path) -> None:
    """A typo'd image path renders a broken image (404), never the SPA shell."""
    missing_path = tmp_path / "nope.png"

    response = client.get(str(missing_path))

    assert response.status_code == 404


def test_directory_with_image_extension_returns_404(client: FlaskClient, tmp_path: Path) -> None:
    """A directory whose name ends in an image extension is not a servable file."""
    directory = tmp_path / "weird.png"
    directory.mkdir()

    response = client.get(str(directory))

    assert response.status_code == 404


def test_nonexistent_path_falls_through_to_app_shell(client: FlaskClient, tmp_path: Path) -> None:
    """A path matching no file is a client-side route: it returns the app shell, not a 404.

    Only paths that resolve to a real file are served; everything else falls
    through so the single-page-app's client-side routing keeps working.
    """
    response = client.get(str(tmp_path / "some" / "client" / "route"))

    assert response.status_code == 200
    assert "text/html" in response.content_type


def test_serves_image_with_spaces_in_filename(client: FlaskClient, tmp_path: Path) -> None:
    """A descriptive filename with spaces (percent-encoded in the URL) still serves.

    The whole feature relies on the framework percent-decoding the catch-all path
    before the handler reconstructs the on-disk path; pin that for a filename an
    agent told to use 'descriptive' names could realistically produce.
    """
    image_path = tmp_path / "my chart 2026.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(quote(str(image_path)))

    assert response.status_code == 200
    assert response.content_type == "image/png"
    assert response.data == image_bytes


def test_serves_image_with_unicode_filename(client: FlaskClient, tmp_path: Path) -> None:
    """A non-ASCII filename (percent-encoded in the URL) serves the right bytes."""
    image_path = tmp_path / "gráfico.png"
    image_bytes = b"fake-png-bytes"
    image_path.write_bytes(image_bytes)

    response = client.get(quote(str(image_path)))

    assert response.status_code == 200
    assert response.data == image_bytes


def test_serves_non_image_file_as_download(client: FlaskClient, tmp_path: Path) -> None:
    """A non-image file is served as an attachment (download), not rendered inline."""
    file_path = tmp_path / "q4-report.pdf"
    file_bytes = b"%PDF-1.4 fake-pdf-bytes"
    file_path.write_bytes(file_bytes)

    response = client.get(str(file_path))

    assert response.status_code == 200
    assert response.data == file_bytes
    disposition = response.headers.get("Content-Disposition", "")
    assert "attachment" in disposition
    assert "q4-report.pdf" in disposition
    # Downloaded, not sniffed into an inline-executable type.
    assert response.headers.get("X-Content-Type-Options") == "nosniff"
    # Cached forever like inline images; per-message ``requested_at`` keeps a new
    # message's link URL distinct so it still fetches the current file.
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


def test_serves_extensionless_file_as_download(client: FlaskClient, tmp_path: Path) -> None:
    """A file with no extension is still served as a download when it exists."""
    file_path = tmp_path / "server-log"
    file_bytes = b"line one\nline two\n"
    file_path.write_bytes(file_bytes)

    response = client.get(str(file_path))

    assert response.status_code == 200
    assert response.data == file_bytes
    assert "attachment" in response.headers.get("Content-Disposition", "")


def test_missing_non_image_path_is_not_a_download(client: FlaskClient, tmp_path: Path) -> None:
    """A non-image path with no file behind it falls through to the app shell, not a download."""
    response = client.get(str(tmp_path / "does-not-exist.pdf"))

    assert response.status_code == 200
    assert "text/html" in response.content_type
    assert "attachment" not in response.headers.get("Content-Disposition", "")


def test_create_chat_carries_the_project_id_beside_the_request_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``project_id`` is accepted on create-chat and is not mistaken for a chat field.

    Chat membership rides the agent's ``project`` label rather than the member
    list, so the project a chat is created in travels with the create request.
    The request model forbids unknown fields, so this guards the split.
    """
    monkeypatch.delenv("MNGR_AGENT_WORK_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    test_client = create_application(build_test_state()).test_client()

    response = test_client.post("/api/agents/create-chat", json={"name": "test-chat", "project_id": "alpha"})

    # Still the no-work-dir failure, i.e. the extra field reached the label path
    # rather than being rejected as an unknown request field.
    assert response.status_code == 400
    assert "project_id" not in response.get_json()["detail"]


@pytest.mark.timeout(15)
def test_websocket_snapshot_exposes_each_agent_project_label(app: Flask) -> None:
    """The agent payload the frontend already receives carries the project label.

    That label is where a chat starts out filed; an agent without one is in no
    project at all, which is ordinary -- Everything enumerates the machine, so
    it still shows up there.
    """
    agent_manager = state_of(app).agent_manager
    with agent_manager._lock:
        agent_manager._agents["chat-1"] = AgentStateItem(
            id="chat-1",
            name="filed-chat",
            state="RUNNING",
            labels={"user_created": "true", "project": "alpha"},
            work_dir=None,
        )
        agent_manager._agents["chat-2"] = AgentStateItem(
            id="chat-2",
            name="loose-chat",
            state="RUNNING",
            labels={"user_created": "true"},
            work_dir=None,
        )

    with serve_app(app) as served:
        ws = open_ws(served, "/api/ws")
        try:
            # The shell's own snapshot (apps, projects) comes first; the agent list follows.
            messages = [json.loads(ws.receive(timeout=_WS_RECEIVE_TIMEOUT)) for _ in range(3)]
        finally:
            close_ws(ws)

    agents_message = messages[2]
    assert agents_message["type"] == "agents_updated"
    project_by_agent_id = {agent["id"]: agent["project"] for agent in agents_message["agents"]}
    assert project_by_agent_id == {"chat-1": "alpha", "chat-2": None}


def test_not_built_page_coordinate_regex_matches_the_canonical_one() -> None:
    """The placeholder derives a service origin, so it carries a copy of the rule.

    ``frontend/src/origin.ts`` is canonical. The placeholder cannot import it -- it
    runs in the browser, in the one state where the bundle it lives in is missing --
    so it holds its own copy, and this pins that copy to the source of truth.
    Without it the rule can be corrected in one place and silently rot in the page
    that only renders when everything else is broken.
    """
    origin_ts = Path(__file__).parents[2] / "frontend" / "src" / "origin.ts"
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
