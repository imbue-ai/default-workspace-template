"""What an installed app is using right now: the resident memory of its supervisord program's whole process tree
(the server, and every notebook kernel or worker it started), read from ``/proc``.

None when the program is not running -- the shell stops a store app a minute after its last window closes, and it
then holds nothing.
"""

import subprocess
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

PROC: Final[Path] = Path("/proc")
KIB_PER_MB: Final[int] = 1024
SUPERVISORCTL_PID_TIMEOUT_SECONDS: Final[float] = 10.0


class ProcessMemoryReaderInterface(MutableModel, ABC):
    """Reads what a supervised program is using."""

    @abstractmethod
    def program_pid(self, program: str) -> int | None:
        """The running program's process id, from supervisord; None when it is not running."""

    @abstractmethod
    def pid_memory_mb(self, pid: int) -> int:
        """The resident memory of ``pid``'s process tree in MB."""

    def program_memory_mb(self, program: str) -> int | None:
        """The program's process tree's resident memory in MB; None when it is not running."""
        pid = self.program_pid(program)
        return None if pid is None else self.pid_memory_mb(pid)


@pure
def parse_parent_pid(stat_text: str) -> int:
    """The parent pid in a ``/proc/<pid>/stat`` line (the field after the parenthesised command, which may hold
    spaces)."""
    return int(stat_text.rsplit(")", 1)[1].split()[1])


@pure
def descendants_of(root: int, parent_by_pid: dict[int, int]) -> set[int]:
    """``root`` and every process under it."""
    tree = {root}
    children_by_parent: dict[int, list[int]] = {}
    for pid, parent in parent_by_pid.items():
        children_by_parent.setdefault(parent, []).append(pid)
    frontier = [root]
    while frontier:
        for child in children_by_parent.get(frontier.pop(), []):
            if child not in tree:
                tree.add(child)
                frontier.append(child)
    return tree


@pure
def parse_resident_kib(status_text: str) -> int:
    """The ``VmRSS`` of a ``/proc/<pid>/status`` text, in KiB (0 when absent, as for a zombie)."""
    for line in status_text.splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    return 0


def tree_memory_mb(root: int, proc: Path = PROC) -> int:
    """The resident memory of ``root``'s process tree in MB."""
    parent_by_pid: dict[int, int] = {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent_by_pid[int(entry.name)] = parse_parent_pid((entry / "stat").read_text())
        except (OSError, ValueError, IndexError):
            continue
    total_kib = 0
    for pid in descendants_of(root, parent_by_pid):
        try:
            total_kib += parse_resident_kib((proc / str(pid) / "status").read_text())
        except OSError:
            continue
    return round(total_kib / KIB_PER_MB)


class SupervisedProcessMemoryReader(ProcessMemoryReaderInterface):
    """The production reader: the program's pid from ``supervisorctl pid``, then its tree from ``/proc``."""

    proc: Path = Field(default=PROC, frozen=True, description="Where process information is read")

    def pid_memory_mb(self, pid: int) -> int:
        return tree_memory_mb(pid, self.proc)

    def program_pid(self, program: str) -> int | None:
        try:
            completed = subprocess.run(
                ["supervisorctl", "pid", program],
                capture_output=True,
                text=True,
                timeout=SUPERVISORCTL_PID_TIMEOUT_SECONDS,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            logger.warning("Could not ask supervisord for {}'s pid: {}", program, e)
            return None
        answer = completed.stdout.strip()
        if not answer.isdigit() or int(answer) == 0:
            return None
        return int(answer)
