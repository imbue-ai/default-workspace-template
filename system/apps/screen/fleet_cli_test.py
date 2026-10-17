import pytest
from click.testing import CliRunner

from screen.cli import EXIT_BUSY
from screen.cli import EXIT_ERROR
from screen.cli import EXIT_OK
from screen.cli import EXIT_PREEMPTED
from screen.cli import EXIT_TIMEOUT
from screen.cli import EXIT_USAGE
from screen.fleet_cli import agent_headers
from screen.fleet_cli import main
from screen.fleet_cli import owner_label
from screen.fleet_cli import render_action


def test_agent_headers_require_a_chat_id_and_prefer_the_chats_over_the_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MINDS_CHAT_ID", raising=False)
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    with pytest.raises(SystemExit) as refused:
        agent_headers()
    assert refused.value.code == EXIT_USAGE
    monkeypatch.setenv("MNGR_AGENT_ID", "agent-successor")
    monkeypatch.setenv("MINDS_CHAT_ID", "agent-first")
    monkeypatch.setenv("MNGR_AGENT_NAME", "Alice")
    headers = agent_headers()
    assert headers["X-Mngr-Agent-Id"] == "agent-first"
    assert headers["X-Mngr-Agent-Name"] == "Alice"
    monkeypatch.delenv("MINDS_CHAT_ID")
    assert agent_headers()["X-Mngr-Agent-Id"] == "agent-successor"


def test_owner_label_distinguishes_self_other_human_free_and_the_lifecycles() -> None:
    me = "alice"
    assert (
        owner_label(
            {"lifecycle": "running", "controller": "agent", "owner_agent_id": "alice", "owner_name": "Alice"}, me
        )
        == "you"
    )
    assert (
        owner_label({"lifecycle": "running", "controller": "agent", "owner_agent_id": "bob", "owner_name": "Bob"}, me)
        == "agent Bob"
    )
    assert owner_label({"lifecycle": "running", "controller": "nobody", "human_pinned": False}, me) == "free"
    assert (
        owner_label({"lifecycle": "running", "controller": "human", "human_pinned": True}, me)
        == "human (took control)"
    )
    assert "driving" in owner_label({"lifecycle": "running", "controller": "human", "human_pinned": False}, me)
    assert "starting" in owner_label({"lifecycle": "init", "controller": "nobody"}, me)
    assert "crashed" in owner_label({"lifecycle": "crashed", "controller": "nobody"}, me)
    assert "stopped" in owner_label({"lifecycle": "stopped", "controller": "nobody"}, me)


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"ok": True}, EXIT_OK),
        ({"ok": False, "status": "busy_human", "enqueued": True}, EXIT_PREEMPTED),
        ({"ok": False, "status": "busy_human", "enqueued": False}, EXIT_BUSY),
        ({"ok": False, "status": "busy_agent", "enqueued": True}, EXIT_BUSY),
        ({"ok": False, "status": "busy_agent", "enqueued": False}, EXIT_BUSY),
        ({"ok": False, "status": "starting"}, EXIT_BUSY),
        ({"ok": False, "status": "crashed"}, EXIT_ERROR),
        ({"ok": False, "status": "stopped", "hint": "start it"}, EXIT_ERROR),
        ({"ok": False, "status": "timed_out"}, EXIT_TIMEOUT),
        ({"ok": False, "status": "error", "error": "boom"}, EXIT_ERROR),
    ],
)
def test_render_action_exit_codes(payload: dict, expected: int) -> None:
    # The exit code an agent branches on is the load-bearing contract of the fleet CLI.
    _, code, _ = render_action(payload, "browser-1", "acquire")
    assert code == expected


def test_the_command_offers_the_ownership_verbs_and_their_aliases() -> None:
    runner = CliRunner()
    assert runner.invoke(main, ["--help"]).exit_code == 0
    for verb in ("ls", "new", "close", "acquire", "release", "unlock", "handoff", "request-human"):
        assert runner.invoke(main, [verb, "--help"]).exit_code == 0, verb
    assert runner.invoke(main, ["drive"]).exit_code == 2
