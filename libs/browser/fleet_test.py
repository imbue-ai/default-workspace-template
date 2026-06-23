import pytest

from browser import fleet


def test_daemon_url_prefers_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MINDS_BROWSER_SERVICE_URL", "http://example:9000/")
    assert fleet._daemon_url() == "http://example:9000"


def test_daemon_url_reads_applications_registry(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("MINDS_BROWSER_SERVICE_URL", raising=False)
    registry = tmp_path / "applications.toml"
    registry.write_text(
        '[[applications]]\nname = "web"\nurl = "http://localhost:8080"\n'
        '[[applications]]\nname = "browser"\nurl = "http://localhost:8081"\n'
    )
    monkeypatch.setenv("MINDS_APPLICATIONS_FILE", str(registry))
    assert fleet._daemon_url() == "http://localhost:8081"


def test_daemon_url_falls_back_to_localhost(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("MINDS_BROWSER_SERVICE_URL", raising=False)
    monkeypatch.setenv("MINDS_APPLICATIONS_FILE", str(tmp_path / "missing.toml"))
    assert fleet._daemon_url() == "http://127.0.0.1:8081"


def test_agent_headers_requires_agent_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MNGR_AGENT_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        fleet._agent_headers()
    assert exc.value.code == fleet._EXIT_USAGE


def test_owner_label_distinguishes_self_other_free_and_pinned() -> None:
    me = "alice"
    assert fleet._owner_label({"controller": "agent", "owner_agent_id": "alice", "owner_name": "Alice"}, me) == "you"
    other = {"controller": "agent", "owner_agent_id": "bob", "owner_name": "Bob"}
    assert fleet._owner_label(other, me) == "agent Bob"
    assert fleet._owner_label({"controller": "human", "human_pinned": False}, me) == "free"
    assert fleet._owner_label({"controller": "human", "human_pinned": True}, me) == "human (took control)"


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"type": "done", "result": "ok"}, fleet._EXIT_OK),
        ({"type": "error", "text": "boom"}, fleet._EXIT_ERROR),
        ({"type": "preempted"}, fleet._EXIT_PREEMPTED),
        ({"type": "busy_human"}, fleet._EXIT_BUSY),
        ({"type": "busy_agent"}, fleet._EXIT_BUSY),
        ({"type": "timed_out"}, fleet._EXIT_TIMEOUT),
        ({"type": "thinking", "text": "..."}, None),
        ({"type": "action", "text": "click"}, None),
        ({"type": "waiting", "busy_name": "Bob"}, None),
        ({"type": "acquired"}, None),
        ({"type": "held"}, None),
    ],
)
def test_render_event_exit_codes(event: dict, expected: int | None) -> None:
    # The exit code an agent branches on is the load-bearing CLI contract.
    assert fleet._render_event(event, browser_id=0) == expected


def test_parser_accepts_task_flags() -> None:
    parser = fleet._build_parser()
    args = parser.parse_args(["task", "2", "do it", "--reclaim", "--no-wait", "--max-wait", "30"])
    assert args.id == 2 and args.prompt == "do it"
    assert args.reclaim is True and args.no_wait is True and args.max_wait == 30.0
    assert fleet._build_parser().parse_args(["ls"]).func is fleet.cmd_ls
    assert fleet._build_parser().parse_args(["new"]).func is fleet.cmd_new
    assert fleet._build_parser().parse_args(["unlock", "1"]).func is fleet.cmd_release
