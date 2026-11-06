"""How much room the workspace has, and whether an app fits in it.

Free disk is read where the store installs (the workspace volume); available memory is read the way the memory guard
reads it (``oom_priority.meminfo.read_meminfo``), so the store and System Monitor agree. A verdict compares an app's
estimate against both: "won't fit" blocks the install, "tight" leaves little room for anything else.
"""

import shutil
from abc import ABC
from abc import abstractmethod
from enum import auto
from pathlib import Path
from typing import Final

from pydantic import Field

from app_store.catalog import CatalogApp
from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from oom_priority.meminfo import read_meminfo

BYTES_PER_MB: Final[int] = 1024 * 1024
# Less than this left over after an install, of disk or of memory, reads as tight.
TIGHT_DISK_LEFT_MB: Final[int] = 1024
TIGHT_MEMORY_LEFT_MB: Final[int] = 512


class WorkspaceRoom(FrozenModel):
    """What the workspace has free right now."""

    disk_free_mb: int = Field(description="Free disk on the workspace volume, in MB")
    memory_available_mb: int = Field(description="Memory available, as the memory guard reads it, in MB")


class RoomVerdict(UpperCaseStrEnum):
    """Whether an app fits."""

    FITS = auto()
    TIGHT = auto()
    WONT_FIT = auto()


class RoomCheck(FrozenModel):
    """An app's estimate against the room, with the plain reason an install is blocked."""

    verdict: RoomVerdict = Field(description="Whether it fits")
    blocked_reason: str = Field(description="Why it cannot be installed; empty unless the verdict is WONT_FIT")


class ResourceReaderInterface(MutableModel, ABC):
    """Reads what the workspace has free."""

    @abstractmethod
    def read_room(self) -> WorkspaceRoom:
        """Free disk and available memory now."""


class SystemResourceReader(ResourceReaderInterface):
    """The production reader: the volume under ``disk_path`` and the memory guard's figures."""

    disk_path: Path = Field(frozen=True, description="A path on the volume installs are written to")

    def read_room(self) -> WorkspaceRoom:
        probe = self.disk_path
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        disk_free = shutil.disk_usage(probe).free
        memory_available = read_meminfo()["available"]
        return WorkspaceRoom(
            disk_free_mb=disk_free // BYTES_PER_MB, memory_available_mb=memory_available // BYTES_PER_MB
        )


@pure
def format_mb(megabytes: int) -> str:
    """A size the way the page writes it: MB under a gigabyte, GB to one decimal above."""
    if megabytes >= 1024:
        return f"{megabytes / 1024:.1f} GB"
    return f"{megabytes} MB"


@pure
def check_app_room(app: CatalogApp, room: WorkspaceRoom) -> RoomCheck:
    """Whether ``app`` fits: the most disk and memory it needs at any point, building included, named by when."""
    resources = app.resources
    is_build_heavier = (resources.build_memory_mb or 0) > (resources.memory_in_use_mb or 0)
    return check_room(
        app.title,
        resources.install_disk_mb,
        resources.install_memory_mb,
        room,
        memory_moment="while it builds" if is_build_heavier else "while in use",
    )


def check_room(
    title: str, disk_mb: int | None, memory_mb: int | None, room: WorkspaceRoom, memory_moment: str = "while in use"
) -> RoomCheck:
    """Whether an app needing ``disk_mb`` of disk and ``memory_mb`` of memory while in use fits in ``room``. The
    in-use figure is the one checked: an app that fits only while nobody uses it does not fit.

    A figure that is not known (an app a user added: its memory until it has run, its disk when the estimate failed)
    cannot block an install; the verdict is only as good as what is known, and is tight when what is free is
    already low."""
    shortfalls: list[str] = []
    if memory_mb is not None and memory_mb > room.memory_available_mb:
        shortfalls.append(
            f"about {format_mb(memory_mb)} of memory {memory_moment}, and {format_mb(room.memory_available_mb)} is available"
        )
    if disk_mb is not None and disk_mb > room.disk_free_mb:
        shortfalls.append(f"about {format_mb(disk_mb)} of disk, and {format_mb(room.disk_free_mb)} is free")
    if shortfalls:
        return RoomCheck(
            verdict=RoomVerdict.WONT_FIT,
            blocked_reason=f"{title} needs {'; and '.join(shortfalls)}. Uninstall an app or close a few windows to make room.",
        )
    if (
        room.disk_free_mb - (disk_mb or 0) < TIGHT_DISK_LEFT_MB
        or room.memory_available_mb - (memory_mb or 0) < TIGHT_MEMORY_LEFT_MB
    ):
        return RoomCheck(verdict=RoomVerdict.TIGHT, blocked_reason="")
    return RoomCheck(verdict=RoomVerdict.FITS, blocked_reason="")


def measure_directory_mb(path: Path) -> int:
    """The disk a folder's files take, in MB (rounded up); 0 when it does not exist. Symlinks are not followed."""
    total = 0
    for directory, _subdirectories, filenames in path.walk():
        for filename in filenames:
            try:
                total += (directory / filename).lstat().st_blocks * 512
            except OSError:
                continue
    return -(-total // BYTES_PER_MB)
