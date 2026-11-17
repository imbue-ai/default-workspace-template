from datetime import datetime
from datetime import timedelta
from datetime import timezone

from screen.memory_shed import is_pid_shed_since

_NOW = datetime(2026, 11, 16, 9, 0, tzinfo=timezone.utc)


def _shed(pid: int, at: datetime) -> dict[str, object]:
    """A record as earlyoom's kill hook writes it (oom_priority.ledger.append_shed_record)."""
    return {"timestamp": at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"), "type": "process_shed", "pid": pid, "comm": "neko"}


def test_a_recent_shed_of_the_pid_is_found_and_an_old_one_or_another_pid_is_not() -> None:
    since = _NOW - timedelta(minutes=10)
    assert is_pid_shed_since([_shed(4242, _NOW - timedelta(minutes=1))], 4242, since) is True
    assert is_pid_shed_since([_shed(4242, _NOW - timedelta(hours=2))], 4242, since) is False
    assert is_pid_shed_since([_shed(7, _NOW)], 4242, since) is False
    notice = {"timestamp": _NOW.isoformat(), "type": "notice_delivered", "pid": 4242}
    assert is_pid_shed_since([notice], 4242, since) is False
