from datetime import datetime
from datetime import timezone

from activity.closures import ClosedKind
from activity.closures import closures_since
from activity.closures import describe_closure


def _shed(timestamp: str, comm: str, agent_name: str | None, rss_kib: int | None = 1_300_000) -> dict[str, object]:
    return {
        "timestamp": timestamp,
        "type": "process_shed",
        "pid": 4321,
        "comm": comm,
        "agent_name": agent_name,
        "is_worker": None,
        "oom_score_adj": 900,
        "badness_kib": 9_000_000,
        "vm_rss_kib": rss_kib,
        "ordering": "badness",
    }


def test_a_shed_agent_a_browser_and_a_program_are_each_described_with_what_to_do() -> None:
    agent = describe_closure(_shed("2026-10-01T12:00:00.000000Z", "claude", "wallpaper"))
    browser = describe_closure(_shed("2026-10-01T12:00:00.000000Z", "chromium", None))
    program = describe_closure(_shed("2026-10-01T12:00:00.000000Z", "pytest", None, rss_kib=None))
    assert agent is not None and (agent.kind, agent.what) == (ClosedKind.CHAT_AGENT, 'the agent of "wallpaper"')
    assert "send it a message" in agent.next_step
    assert browser is not None and (browser.kind, browser.what) == (ClosedKind.BROWSER_TAB, "a browser tab")
    assert program is not None and program.what == "a program an agent was running (pytest)"
    assert (program.freed_kib, agent.freed_kib) == (None, 1_300_000)


def test_other_records_and_malformed_ones_are_not_closures() -> None:
    assert describe_closure({"type": "notice_delivered", "agent_name": "x", "up_to_timestamp": "t"}) is None
    assert describe_closure({"type": "process_shed", "pid": 1}) is None
    assert describe_closure({"type": "process_shed", "timestamp": "yesterday", "pid": 1}) is None
    assert describe_closure(_shed("2026-10-01T12:00:00", "claude", "wallpaper")) is None


def test_closures_since_a_moment_come_newest_first() -> None:
    records = [
        _shed("2026-09-20T12:00:00.000000Z", "pytest", None),
        _shed("2026-10-01T09:00:00.000000Z", "chromium", None),
        _shed("2026-10-01T11:00:00.000000Z", "claude", "wallpaper"),
    ]
    since = datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert [closure.command_name for closure in closures_since(records, since)] == ["claude", "chromium"]


def test_a_shed_helper_agent_and_a_shed_service_are_not_described_as_a_chat_or_an_agents_program() -> None:
    worker = {**_shed("2026-10-01T12:00:00.000000Z", "claude", "test-runner"), "is_worker": True}
    service = {**_shed("2026-10-01T12:00:00.000000Z", "host-backup", None), "oom_score_adj": 50}
    helper = describe_closure(worker)
    background = describe_closure(service)
    assert helper is not None and (helper.kind, helper.what) == (
        ClosedKind.HELPER_AGENT,
        'a helper agent ("test-runner")',
    )
    assert "send it a message" not in helper.next_step
    assert background is not None and (background.kind, background.what) == (
        ClosedKind.SERVICE,
        "a background program (host-backup)",
    )
    assert "starts again on its own" in background.next_step
