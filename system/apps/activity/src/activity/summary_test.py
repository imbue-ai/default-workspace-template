from datetime import datetime
from datetime import timezone

from activity.chats import ChatInfo
from activity.memory_reading import ClosingPoint
from activity.memory_reading import MemoryCloser
from activity.memory_reading import MemoryReading
from activity.memory_reading import MemorySource
from activity.processes import ProcessReading
from activity.summary import ALWAYS_ON_REASONS
from activity.summary import ActivitySummary
from activity.summary import HELPER_AGENT_DESCRIPTION
from activity.summary import ItemKind
from activity.summary import MemoryStatus
from activity.summary import OTHER_AGENT_DESCRIPTION
from activity.summary import PLUMBING_ITEM_ID
from activity.summary import RegisteredAgent
from activity.summary import SummaryInputs
from activity.summary import UNKNOWN_PROGRAM_STATE
from activity.summary import UNNAMED_CHAT_DESCRIPTION
from activity.summary import app_for_program
from activity.summary import build_summary
from activity.summary import credit_processes
from activity.summary import likely_first_to_close
from activity.summary import memory_status
from activity.supervised_programs import SupervisedProgram
from app_manifest.registry import RegistryRow

_MIB = 1024 * 1024
_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
_EARLYOOM_ARGV = ("earlyoom", "-m", "10,5", "--avoid", "^(sshd|supervisord|earlyoom|tini)$|^tmux")


def _process(pid: int, parent_pid: int, name: str, rss_mib: int, oom_score_adj: int) -> ProcessReading:
    return ProcessReading(
        pid=pid,
        parent_pid=parent_pid,
        command_name=name,
        command_line=name,
        rss_kib=rss_mib * 1024,
        swap_kib=0,
        page_table_kib=0,
        oom_score_adj=oom_score_adj,
    )


def _row(name: str, program: str | None, critical: bool, on_demand: bool, internal: bool = False) -> RegistryRow:
    return RegistryRow.model_validate(
        {
            "name": name,
            "url": "http://localhost:8000",
            "display_name": None if internal else name.title(),
            "program": program,
            "critical": critical,
            "stop_when_no_windows": on_demand,
            "internal": internal,
        }
    )


def _guard_closing(total_mib: int) -> ClosingPoint:
    return ClosingPoint(
        used_bytes=total_mib * _MIB * 90 // 100,
        closer=MemoryCloser.MEMORY_GUARD,
        min_available_percent=10,
        badness_total_kib=total_mib * 1024,
        detail="earlyoom",
    )


def test_status_steps_toward_the_closing_point() -> None:
    assert memory_status(80 * _MIB, 100 * _MIB) is MemoryStatus.COMFORTABLE
    assert memory_status(83 * _MIB, 100 * _MIB) is MemoryStatus.TIGHT
    assert memory_status(98 * _MIB, 100 * _MIB) is MemoryStatus.CRITICAL
    assert memory_status(1, 0) is MemoryStatus.CRITICAL


def test_a_program_belongs_to_the_app_that_runs_it_or_whose_sidecar_it_is() -> None:
    rows = [
        _row("terminal", "terminal", True, False),
        _row("terminal-pty", "terminal-pty", True, False, internal=True),
    ]
    assert app_for_program("terminal", rows) == rows[0]
    assert app_for_program("terminal-pty", rows) == rows[0]
    assert app_for_program("cron", rows) is None


def test_processes_go_to_their_nearest_root_chromium_to_the_browser_and_the_rest_to_plumbing() -> None:
    processes = [
        _process(1, 0, "sh", 1, 0),
        _process(10, 1, "claude", 400, 300),
        _process(11, 10, "pytest", 900, 900),
        _process(20, 1, "chromium", 300, 1000),
        _process(30, 1, "sshd", 7, 0),
    ]
    credited = credit_processes(processes, {10: "chat:c1"}, "app:browser")
    assert credited == {1: PLUMBING_ITEM_ID, 10: "chat:c1", 11: "chat:c1", 20: "app:browser", 30: PLUMBING_ITEM_ID}
    assert credit_processes(processes, {}, None)[20] == PLUMBING_ITEM_ID


def test_the_prediction_follows_earlyooms_fork_including_its_avoid_penalty() -> None:
    processes = [
        _process(10, 1, "claude", 400, 300),
        _process(11, 10, "pytest", 1300, 900),
        _process(20, 1, "chromium", 350, 1000),
        _process(40, 1, "supervisord", 99999, -1000),
    ]
    credited = {10: "chat:c1", 11: "chat:c1", 20: "app:browser", 40: PLUMBING_ITEM_ID}
    pick = likely_first_to_close(processes, credited, _guard_closing(6656), _EARLYOOM_ARGV)
    assert pick is not None and (pick.item_id, pick.pid, pick.command_name) == ("chat:c1", 11, "pytest")

    # A huge tmux server: earlyoom's --avoid costs it 300 points rather than exempting it.
    huge_tmux = [*processes, _process(30, 1, "tmux: server", 3500, 1000)]
    avoided = likely_first_to_close(
        huge_tmux, {**credited, 30: PLUMBING_ITEM_ID}, _guard_closing(6656), _EARLYOOM_ARGV
    )
    assert avoided is not None and avoided.pid == 30
    smaller_tmux = [*processes, _process(30, 1, "tmux: server", 2000, 1000)]
    not_avoided = likely_first_to_close(
        smaller_tmux, {**credited, 30: PLUMBING_ITEM_ID}, _guard_closing(6656), _EARLYOOM_ARGV
    )
    assert not_avoided is not None and not_avoided.pid == 11
    assert likely_first_to_close([], {}, _guard_closing(1024), _EARLYOOM_ARGV) is None


def _inputs(chats: tuple[ChatInfo, ...] | None, programs: tuple[SupervisedProgram, ...] | None) -> SummaryInputs:
    return SummaryInputs(
        measured_at=_NOW,
        memory=MemoryReading(
            limit_bytes=8192 * _MIB, used_bytes=4000 * _MIB, source=MemorySource.CGROUP, source_detail="cgroup"
        ),
        closing=_guard_closing(8192),
        earlyoom_argv=_EARLYOOM_ARGV,
        processes=(
            _process(415, 1, "supervisord", 31, -1000),
            _process(544, 415, "chat-app", 160, 25),
            _process(559, 415, "terminal-app", 50, 10),
            _process(560, 415, "ttyd", 15, 12),
            _process(542, 415, "mngr", 120, 24),
            _process(13106, 13065, "claude", 330, 480),
            _process(13200, 13065, "claude", 90, 560),
            _process(14854, 14843, "codex", 200, 600),
            _process(9000, 1, "claude", 60, 0),
        ),
        programs=programs,
        chats=chats,
        agents=(
            RegisteredAgent(agent_id="agent-a", agent_name="wallpaper", is_worker=False, pids=(13106,)),
            RegisteredAgent(agent_id="agent-a0", agent_name="wallpaper-before-switch", is_worker=False, pids=(13200,)),
            RegisteredAgent(agent_id="agent-w", agent_name="test-runner", is_worker=True, pids=(14854,)),
            RegisteredAgent(agent_id="agent-main", agent_name="system-services", is_worker=False, pids=(9000,)),
        ),
        app_rows=(
            _row("chat", "chat", True, False),
            _row("terminal", "terminal", True, False),
            _row("terminal-pty", "terminal-pty", True, False, internal=True),
            _row("files", "files", False, True),
        ),
        notes=(),
        is_preview=False,
    )


_PROGRAMS = (
    SupervisedProgram(name="chat", state="RUNNING", pid=544),
    SupervisedProgram(name="terminal", state="RUNNING", pid=559),
    SupervisedProgram(name="terminal-pty", state="RUNNING", pid=560),
    SupervisedProgram(name="files", state="STOPPED", pid=None),
    SupervisedProgram(name="agent-observer", state="RUNNING", pid=542),
)
_CHATS = (
    ChatInfo(
        chat_id="c1",
        title="Wallpaper",
        status="idle",
        harness="claude",
        agent_id="agent-a",
        agent_ids=("agent-a0", "agent-a"),
        last_messaged_at=None,
    ),
)


def _drawn_kib(summary: ActivitySummary) -> int:
    return sum(item.rss_kib for group in (summary.chats, summary.apps, summary.services) for item in group)


def test_a_chat_keeps_its_earlier_agents_and_other_agents_are_labelled_for_what_they_are() -> None:
    summary = build_summary(_inputs(_CHATS, _PROGRAMS))
    assert [(item.name, item.kind, item.rss_kib // 1024, item.description) for item in summary.chats] == [
        ("Wallpaper", ItemKind.CHAT, 420, ""),
        ("test-runner", ItemKind.HELPER_AGENT, 200, HELPER_AGENT_DESCRIPTION),
        ("system-services", ItemKind.AGENT, 60, OTHER_AGENT_DESCRIPTION),
    ]
    assert all(item.state == "running" for item in summary.chats if item.kind is not ItemKind.CHAT)
    assert summary.are_chat_names_known is True


def test_apps_services_and_plumbing_carry_their_own_processes_and_reasons() -> None:
    summary = build_summary(_inputs(_CHATS, _PROGRAMS))
    apps = {item.name: item for item in summary.apps}
    assert set(apps) == {"Chat", "Terminal", "Files"}
    assert apps["Terminal"].rss_kib // 1024 == 65
    assert apps["Terminal"].always_on_reason == ALWAYS_ON_REASONS["terminal"]
    assert (apps["Files"].state, apps["Files"].rss_kib, apps["Files"].always_on_reason) == ("STOPPED", 0, None)
    services = {item.item_id: item for item in summary.services}
    assert services["service:agent-observer"].name == "Chat activity tracker"
    assert services[PLUMBING_ITEM_ID].rss_kib // 1024 == 31
    assert summary.memory is not None and summary.memory.closer is MemoryCloser.MEMORY_GUARD
    assert summary.likely_first_to_close is not None and summary.likely_first_to_close.item_id == "agent:agent-w"


def test_every_process_is_drawn_somewhere_even_without_the_chat_app_or_supervisord() -> None:
    every_process_kib = sum(process.rss_kib for process in _inputs(None, None).processes)
    for chats, programs in ((_CHATS, _PROGRAMS), (None, _PROGRAMS), (_CHATS, None), (None, None)):
        assert _drawn_kib(build_summary(_inputs(chats, programs))) == every_process_kib


def test_without_the_chat_app_agents_are_still_listed_and_the_pick_points_at_a_drawn_row() -> None:
    summary = build_summary(_inputs(None, _PROGRAMS))
    assert summary.are_chat_names_known is False
    by_name = {item.name: item for item in summary.chats}
    assert by_name["wallpaper"].description == UNNAMED_CHAT_DESCRIPTION
    assert by_name["test-runner"].kind is ItemKind.HELPER_AGENT
    drawn_ids = {item.item_id for group in (summary.chats, summary.apps, summary.services) for item in group}
    assert summary.likely_first_to_close is not None and summary.likely_first_to_close.item_id in drawn_ids


def test_without_supervisord_apps_say_their_state_is_unknown_rather_than_not_running() -> None:
    summary = build_summary(_inputs(_CHATS, None))
    assert summary.are_programs_known is False
    assert {item.state for item in summary.apps} == {UNKNOWN_PROGRAM_STATE}
