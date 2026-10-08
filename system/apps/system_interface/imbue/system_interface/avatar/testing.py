"""Test helpers for the avatar package: a minimal design the validator accepts, a registration of it, what a test
reads off a rendered icon, and a chat's background task marker written by the script that owns the format."""

import importlib.util
import struct
import subprocess
import sys
import zlib
from pathlib import Path
from types import ModuleType
from typing import Final

from imbue.system_interface.avatar.catalog import DesignRegistration
from imbue.system_interface.avatar.primitives import DesignId
from imbue.system_interface.avatar.status import BACKGROUND_TASKS_SCRIPT

# A body and closed-eye group on the 100 by 100 grid: the least a design that renders an expression needs.
MINIMAL_DESIGN_SVG: Final[str] = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
    '<g class="jelly-body"><circle cx="50" cy="60" r="30" fill="#8cd"/></g>'
    '<g class="jelly-eyes"><ellipse cx="42" cy="55" rx="2" ry="3"/><ellipse cx="58" cy="55" rx="2" ry="3"/></g>'
    "</svg>"
)


def design_registration(design_id: str, label: str = "Mine", svg: str = MINIMAL_DESIGN_SVG) -> DesignRegistration:
    """A registration of ``svg`` under ``design_id``, drawn at ``/tmp/<design_id>.svg``."""
    return DesignRegistration(id=DesignId(design_id), label=label, svg=svg, source_path=f"/tmp/{design_id}.svg")


# A drawing with nothing in it: its icon is the bare tile.
EMPTY_DESIGN_SVG: Final[str] = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"></svg>'

_PNG_SIGNATURE: Final[bytes] = b"\x89PNG\r\n\x1a\n"
_RGBA_COLOR_TYPE: Final[int] = 6


def png_size(png: bytes) -> tuple[int, int]:
    """A PNG's width and height, from its header."""
    assert png[:8] == _PNG_SIGNATURE and png[12:16] == b"IHDR"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def png_top_left_rgba(png: bytes) -> tuple[int, int, int, int]:
    """The top-left pixel of an 8-bit RGBA PNG. Every scanline filter leaves the first row's first pixel as stored
    (it has no neighbour to predict from), so it is the first four bytes after the first filter byte."""
    assert png[24] == 8 and png[25] == _RGBA_COLOR_TYPE
    offset = 8
    compressed = b""
    while offset < len(png):
        (length,) = struct.unpack(">I", png[offset : offset + 4])
        if png[offset + 4 : offset + 8] == b"IDAT":
            compressed += png[offset + 8 : offset + 8 + length]
        offset += 12 + length
    red, green, blue, alpha = zlib.decompress(compressed)[1:5]
    return red, green, blue, alpha


def _background_tasks_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("background_tasks_for_avatar_tests", BACKGROUND_TASKS_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_BACKGROUND_TASKS: Final[ModuleType] = _background_tasks_module()


def write_background_task_marker(root: Path, chat_id: str, task_id: str, pid: int) -> None:
    """A ``run_in_background`` marker for ``chat_id`` under ``root``, kept live by ``pid``, as the runner writes it."""
    _BACKGROUND_TASKS.write_marker(
        root,
        chat_id,
        _BACKGROUND_TASKS.BackgroundTask(
            source=_BACKGROUND_TASKS.SOURCE_RUN_IN_BACKGROUND,
            id=task_id,
            description="Wait for the background agent",
            started_at="2026-10-08T09:00:00+00:00",
            pid=pid,
        ),
    )


def remove_background_task_marker(root: Path, chat_id: str, task_id: str) -> None:
    _BACKGROUND_TASKS.remove_marker(root, chat_id, _BACKGROUND_TASKS.SOURCE_RUN_IN_BACKGROUND, task_id)


def exited_process_pid() -> int:
    """The pid of a process that has already exited, which a marker naming it is stale by."""
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait()
    return process.pid
