"""The workspace color (docs/system/blueprint/brand-design-system/): the color Imbue Studio keeps as the ``color``
label of the workspace's primary agent, read by the shell and kept where every app serves it from.

The shell reads the label when it starts, and again whenever Imbue Studio says it changed (the
``minds:workspace-color`` embedder message), so a pick in Imbue Studio recolors every open window at once. The
message names the color, which the shell wears at once, since Studio announces a pick before its write of the label
lands; it then rereads the label until the label says that color, and keeps whatever the label says if it never does. A change is announced with ``themes_changed``,
whose catalog carries the color's revision."""

import json
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr
from workspace_themes.workspace_color import WorkspaceColorRecord
from workspace_themes.workspace_color import normalize_hex
from workspace_themes.workspace_color import read_workspace_color
from workspace_themes.workspace_color import write_workspace_color

from imbue.concurrency_group.errors import ProcessError
from imbue.concurrency_group.subprocess_utils import run_local_command_modern_version
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure

# The primary agent carries the workspace's labels in Imbue Studio; the services agent beside it on the host does
# not, so the read asks for the one the color is written to.
PRIMARY_AGENT_LABEL: Final[str] = "is_primary"
WORKSPACE_COLOR_LABEL: Final[str] = "color"
LIST_PRIMARY_AGENT_ARGV: Final[tuple[str, ...]] = (
    "uv",
    "run",
    "--no-sync",
    "mngr",
    "list",
    "--local",
    "--label",
    f"{PRIMARY_AGENT_LABEL}=true",
    "--format",
    "json",
)
_LIST_TIMEOUT_SECONDS: Final[float] = 60.0
# Imbue Studio announces a pick as it shows it, before its write of the label has landed: the shell wears the pick at
# once and rereads the label on this beat until the label says it, for at most this long.
REREAD_INTERVAL_SECONDS: Final[float] = 0.5
REREAD_WINDOW_SECONDS: Final[float] = 15.0
_LIST_SHUTDOWN_TIMEOUT_SECONDS: Final[float] = 5.0


@pure
def color_label_of(listing: Any) -> str | None:
    """The normalized ``color`` label of the first listed agent that has one, from ``mngr list --format json``."""
    agents = listing.get("agents") if isinstance(listing, dict) else None
    for agent in agents if isinstance(agents, list) else []:
        labels = agent.get("labels") if isinstance(agent, dict) else None
        value = labels.get(WORKSPACE_COLOR_LABEL) if isinstance(labels, dict) else None
        if isinstance(value, str) and normalize_hex(value) is not None:
            return normalize_hex(value)
    return None


def list_primary_agent(repo_root: Path) -> Any:
    """``mngr list`` for this host's primary agent, parsed; raises ProcessError, OSError or ValueError."""
    result = run_local_command_modern_version(
        command=LIST_PRIMARY_AGENT_ARGV,
        cwd=repo_root,
        timeout=_LIST_TIMEOUT_SECONDS,
        shutdown_timeout_sec=_LIST_SHUTDOWN_TIMEOUT_SECONDS,
    )
    return json.loads(result.stdout)


class WorkspaceColorKeeper(MutableModel):
    """Reads the workspace color label and keeps it in ``state_path``, announcing a change through ``on_changed``."""

    repo_root: Path = Field(frozen=True, description="The workspace's repo root, which mngr runs from")
    state_path: Path = Field(frozen=True, description="Where the color is kept for every app to serve")
    on_changed: Callable[[], None] = Field(frozen=True, description="Called after the kept color changed")
    list_agents: Callable[[Path], Any] = Field(
        frozen=True, default=list_primary_agent, description="Lists the primary agent (injectable for tests)"
    )
    reread_interval_seconds: float = Field(
        frozen=True, default=REREAD_INTERVAL_SECONDS, description="How often a pick waiting on its label rereads it"
    )
    reread_window_seconds: float = Field(
        frozen=True, default=REREAD_WINDOW_SECONDS, description="How long a pick waits for its label to say it"
    )
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _stop: threading.Event = PrivateAttr(default_factory=threading.Event)
    # Which pick the rereads serve: a newer pick ends an older one's wait, so only the newest is kept.
    _generation: int = PrivateAttr(default=0)

    def read(self) -> WorkspaceColorRecord:
        return read_workspace_color(self.state_path)

    def stop(self) -> None:
        self._stop.set()

    def _keep(self, record: WorkspaceColorRecord) -> None:
        with self._lock:
            if record == self.read():
                return
            try:
                write_workspace_color(self.state_path, record)
            except OSError as e:
                logger.warning("Could not keep the workspace color {}: {}", record.hex, e)
                return
        logger.info("The workspace color is now {}", record.hex or "Imbue's own")
        self.on_changed()

    def _read_label(self) -> WorkspaceColorRecord | None:
        try:
            listing = self.list_agents(self.repo_root)
        except (ProcessError, OSError, ValueError) as e:
            logger.warning("Could not read the workspace color label: {}", e)
            return None
        return WorkspaceColorRecord(hex=color_label_of(listing))

    def refresh(self) -> None:
        """Read the label and keep it; a read that fails keeps the color already kept."""
        label = self._read_label()
        if label is not None:
            self._keep(label)

    def refresh_until(self, announced_hex: str | None) -> None:
        """Wear the color Imbue Studio announced at once, then reread the label until it says that color. A label
        that never comes to say it within the window is kept as it is: the label has the last word."""
        announced = normalize_hex(announced_hex) if announced_hex is not None else None
        if announced is None:
            self.refresh()
            return
        with self._lock:
            self._generation += 1
            generation = self._generation
        self._keep(WorkspaceColorRecord(hex=announced))
        attempts = max(1, int(self.reread_window_seconds / self.reread_interval_seconds))
        label: WorkspaceColorRecord | None = None
        for _ in range(attempts):
            if generation != self._generation:
                return
            label = self._read_label()
            if label is not None and label.hex == announced:
                return
            if self._stop.wait(self.reread_interval_seconds):
                return
        if label is not None and generation == self._generation:
            self._keep(label)

    def refresh_in_background(self, expected_hex: str | None = None) -> None:
        """``refresh_until`` on a thread of its own, so a slow mngr never holds the shell's start or a request."""
        threading.Thread(
            target=self.refresh_until, args=(expected_hex,), daemon=True, name="workspace-color-refresh"
        ).start()
