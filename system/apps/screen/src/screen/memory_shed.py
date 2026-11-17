"""Whether the workspace's memory guard ended a process of the stack, read from its shed ledger.

earlyoom kills ("sheds") the most expendable processes under sustained memory pressure, and its kill hook appends a
``process_shed`` record naming each one by pid to the ledger ``oom_priority`` keeps (oom_priority/ledger.py). A member
of the stack that left RUNNING with its pid in a recent record was shed, which the viewer says in place of a crash.
"""

from collections.abc import Iterable
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from oom_priority import ledger

# A shed record older than this names an earlier process that happened to have the same pid.
_SHED_RECENCY: Final[timedelta] = timedelta(minutes=10)


@pure
def is_pid_shed_since(records: Iterable[Mapping[str, Any]], pid: int, since: datetime) -> bool:
    """Whether a ``process_shed`` record for ``pid`` was written at or after ``since``; a record of another shape is
    skipped."""
    for record in records:
        if record.get("type") != ledger.RECORD_TYPE_PROCESS_SHED or record.get("pid") != pid:
            continue
        try:
            shed_at = datetime.fromisoformat(str(record.get("timestamp")))
        except ValueError:
            continue
        if (shed_at if shed_at.tzinfo is not None else shed_at.replace(tzinfo=timezone.utc)) >= since:
            return True
    return False


def was_shed_recently(pid: int) -> bool:
    """Whether the memory guard shed ``pid`` in the last few minutes, by the workspace's shed ledger."""
    return is_pid_shed_since(ledger.read_records(), pid, datetime.now(timezone.utc) - _SHED_RECENCY)
