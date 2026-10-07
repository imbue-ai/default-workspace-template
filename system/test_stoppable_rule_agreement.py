"""System Monitor offers Stop by its own copy of the shell's rule for what it may quit (``activity.apps``), since one
app cannot import another's package at runtime. This keeps the two from drifting: over a spread of registry rows, the
shell's ``stoppable_program_of`` and System Monitor's ``is_quittable_by_shell`` must agree on every one."""

from activity.apps import is_quittable_by_shell
from app_manifest.registry import RegistryRow
from imbue.system_interface.shell.data_types import (
    AppInventoryEntry,
    stoppable_program_of,
)


def _row(name: str, program: str | None, critical: bool) -> RegistryRow:
    return RegistryRow.model_validate(
        {
            "name": name,
            "url": f"http://127.0.0.1:{9000 + len(name)}",
            "program": program,
            "critical": critical,
        }
    )


_ROWS = (
    _row("system_interface", "system_interface", True),
    _row("terminal", "terminal", True),
    _row("terminal-pty", "terminal", False),
    _row("files", "files", False),
    _row("browser", "browser", False),
    _row("activity", "activity", False),
    _row("notes", None, False),
    _row("shared-one", "shared", False),
    _row("shared-two", "shared", False),
)


def test_system_monitor_and_the_shell_agree_on_what_the_shell_may_quit() -> None:
    entries = [AppInventoryEntry(row=row, is_running=True) for row in _ROWS]
    for row, entry in zip(_ROWS, entries):
        assert is_quittable_by_shell(row, _ROWS) is (
            stoppable_program_of(entry, entries) is not None
        ), row.name
