"""What is taking up the workspace's disk, in categories a user recognizes, measured with ``du`` when asked.

The categories cover disjoint folders, so their sizes add up. No limit is shown: inside the container ``df``
reports the host's disk, and a cloud workspace's quota is enforced outside the container and published nowhere
inside it. ``du`` walks every file, which is slow on a large tree (and slower under gVisor), so the page measures
once when the tab opens and again only when the user asks; one ``du`` process measures every folder, under a
timeout.
"""

import subprocess
from collections.abc import Callable
from collections.abc import Mapping
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import Field

from activity.commands import RunCommand
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure

DU_TIMEOUT_SECONDS: Final[float] = 60.0
LARGEST_FOLDER_COUNT: Final[int] = 6
DATA_DIR: Final[Path] = Path("data")


class StorageCategory(FrozenModel):
    """A group of folders the page shows as one line."""

    category_id: str = Field(description="A stable id for the page")
    name: str = Field(description="What the user calls it")
    description: str = Field(description="One plain line on what it holds and whether it is safe to remove")
    paths: tuple[Path, ...] = Field(description="The folders it covers, relative to the workspace root or absolute")


# The user's own folders under data/ are listed one by one at read time (``user_file_paths``); everything here is
# the workspace's.
WORKSPACE_CATEGORIES: Final[tuple[StorageCategory, ...]] = (
    StorageCategory(
        category_id="agents",
        name="Chats and agents",
        description="Conversations, what each agent did, and their saved state.",
        paths=(Path("/home/user/.mngr"), Path("/home/user/.minds")),
    ),
    StorageCategory(
        category_id="app_data",
        name="App data",
        description="What your apps and tasks keep, plus the workspace's own bookkeeping.",
        paths=(
            DATA_DIR / ".apps",
            DATA_DIR / ".state",
            DATA_DIR / ".tickets",
            DATA_DIR / ".tasks",
            DATA_DIR / ".skills",
        ),
    ),
    StorageCategory(
        category_id="tools",
        name="Apps and tools",
        description="Installed with your workspace. Removing these would break things.",
        paths=(Path("system"), Path(".venv"), Path("/root/.local")),
    ),
    StorageCategory(
        category_id="caches",
        name="Download caches",
        description="Copies of tools and packages kept so installs are faster. Tools download them again if missing.",
        paths=(
            Path("/var/cache/user"),
            Path("/root/.cache"),
            Path("/root/.npm"),
            Path("/home/user/.cache"),
            Path("/home/user/.npm"),
        ),
    ),
    StorageCategory(
        category_id="logs",
        name="Logs",
        description="Records of what the background services did, used to diagnose problems.",
        paths=(Path("/var/log/supervisor"),),
    ),
)
USER_FILES_CATEGORY_ID: Final[str] = "files"
USER_FILES_NAME: Final[str] = "Your files"
USER_FILES_DESCRIPTION: Final[str] = "Documents, uploads, notes and anything your agents made for you."


class MeasuredFolder(FrozenModel):
    """One folder's size."""

    path: str = Field(description="The folder, as the category names it")
    size_kib: int = Field(description="Its size on disk, in KiB")


class MeasuredCategory(FrozenModel):
    """A category with its measured folders."""

    category_id: str = Field(description="A stable id for the page")
    name: str = Field(description="What the user calls it")
    description: str = Field(description="One plain line on what it holds")
    size_kib: int = Field(description="The sum of its folders' sizes, in KiB")
    folders: tuple[MeasuredFolder, ...] = Field(description="Its folders that exist, largest first")


class LargestFolder(FrozenModel):
    """One of the largest measured folders, with the category it is in."""

    path: str = Field(description="The folder")
    size_kib: int = Field(description="Its size on disk, in KiB")
    category_name: str = Field(description="The category it belongs to")


class StorageSummary(FrozenModel):
    """Everything the storage tab shows."""

    measured_at: datetime = Field(description="When the measurement finished")
    measure_seconds: float = Field(description="How long ``du`` took")
    total_kib: int = Field(description="The sum of every category, in KiB")
    categories: tuple[MeasuredCategory, ...] = Field(description="The categories, largest first")
    largest: tuple[LargestFolder, ...] = Field(description="The largest measured folders")
    command: str = Field(description="The command that measured them, for the page's details")
    notes: tuple[str, ...] = Field(description="What could not be measured")


def user_file_paths(data_dir: Path) -> tuple[Path, ...]:
    """The user's own folders and files: everything under data/ whose name does not start with a dot."""
    if not data_dir.is_dir():
        return ()
    return tuple(sorted(entry for entry in data_dir.iterdir() if not entry.name.startswith(".")))


@pure
def parse_du_output(text: str) -> dict[str, int]:
    """``du -sk`` lines (``<KiB>\\t<path>``) as sizes by path."""
    sizes: dict[str, int] = {}
    for line in text.splitlines():
        size_text, separator, path = line.partition("\t")
        if separator and size_text.strip().isdigit():
            sizes[path] = int(size_text)
    return sizes


@pure
def summarize_storage(
    categories: Sequence[StorageCategory],
    size_kib_by_path: Mapping[str, int],
    measured_at: datetime,
    measure_seconds: float,
    command: str,
    notes: Sequence[str],
) -> StorageSummary:
    measured: list[MeasuredCategory] = []
    largest: list[LargestFolder] = []
    for category in categories:
        folders = sorted(
            (
                MeasuredFolder(path=str(path), size_kib=size_kib_by_path[str(path)])
                for path in category.paths
                if str(path) in size_kib_by_path
            ),
            key=lambda folder: -folder.size_kib,
        )
        measured.append(
            MeasuredCategory(
                category_id=category.category_id,
                name=category.name,
                description=category.description,
                size_kib=sum(folder.size_kib for folder in folders),
                folders=tuple(folders),
            )
        )
        largest.extend(
            LargestFolder(path=folder.path, size_kib=folder.size_kib, category_name=category.name)
            for folder in folders
        )
    return StorageSummary(
        measured_at=measured_at,
        measure_seconds=round(measure_seconds, 1),
        total_kib=sum(category.size_kib for category in measured),
        categories=tuple(sorted(measured, key=lambda category: -category.size_kib)),
        largest=tuple(sorted(largest, key=lambda folder: -folder.size_kib)[:LARGEST_FOLDER_COUNT]),
        command=command,
        notes=tuple(notes),
    )


def measure_storage(
    data_dir: Path, run_command: RunCommand, now: Callable[[], datetime], clock: Callable[[], float]
) -> StorageSummary:
    categories = tuple(
        StorageCategory(
            category_id=category.category_id,
            name=category.name,
            description=category.description,
            paths=tuple(path.absolute() for path in category.paths),
        )
        for category in (
            StorageCategory(
                category_id=USER_FILES_CATEGORY_ID,
                name=USER_FILES_NAME,
                description=USER_FILES_DESCRIPTION,
                paths=user_file_paths(data_dir),
            ),
            *WORKSPACE_CATEGORIES,
        )
    )
    existing_paths = [str(path) for category in categories for path in category.paths if path.exists()]
    argv = ("du", "-sk", "--", *existing_paths)
    notes: list[str] = []
    started = clock()
    try:
        result = run_command(argv, DU_TIMEOUT_SECONDS)
        sizes = parse_du_output(result.stdout)
        if result.returncode != 0 and result.stderr.strip():
            notes.append(f"du could not read everything: {result.stderr.strip().splitlines()[0]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        sizes = {}
        notes.append(f"Measuring took too long or failed: {e}")
    return summarize_storage(
        categories=categories,
        size_kib_by_path=sizes,
        measured_at=now(),
        measure_seconds=clock() - started,
        command="du -sk -- <each folder listed>",
        notes=notes,
    )
