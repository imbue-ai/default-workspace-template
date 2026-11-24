"""Stand-ins the tests build their cases with: measured nodes without walking the disk, and a fake workspace.

Nothing here reads or writes anything outside the data directory the tests point the app at (``conftest.py``), and
nothing starts a chat, a measurement or a deletion it did not plant itself.
"""

import json
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from workspace_layout.errors import ShellOpError
from workspace_layout.ops import ShowArgs

from disk_space import scan, store

GIB: int = 1024**3


def a_node(
    node_id: str,
    parent: str | None,
    name: str,
    *,
    size: int = 0,
    path: str = "",
    verdict: str = scan.KEEP,
    why: str = "",
    item: bool = False,
    method: str = "",
) -> dict[str, Any]:
    """One node the way a measurement stores it: ``deletable`` follows from being a safe item with a method."""
    return {
        "id": node_id,
        "parent": parent,
        "name": name,
        "path": path,
        "bytes": size,
        "verdict": verdict,
        "why": why,
        "item": item,
        "deletable": item and verdict == scan.SAFE and bool(method),
        "method": method,
    }


def a_disk(
    total: int = 100 * GIB, used: int = 93 * GIB, free: int = 7 * GIB, percent: int = 93
) -> dict[str, int]:
    """The disk's figures as ``scan.disk_usage`` answers them."""
    return {"total": total, "used": used, "free": free, "percent": percent}


def save_measurement(
    nodes: Sequence[dict[str, Any]],
    *,
    disk: dict[str, int] | None = None,
    taken_at: float | None = None,
    missing: Sequence[str] = (),
) -> dict[str, Any]:
    """Saves a measurement the way ``scan.measure`` does, and answers it; ``store.latest_snapshot`` now finds it."""
    snapshot_dir = store.new_snapshot_dir()
    snapshot: dict[str, Any] = {
        "id": snapshot_dir.name,
        "taken_at": time.time() if taken_at is None else taken_at,
        "took_seconds": 1.0,
        "disk": a_disk() if disk is None else disk,
        "missing": list(missing),
        "nodes": list(nodes),
    }
    store.save_snapshot(snapshot_dir, snapshot)
    return snapshot


def latest_measurement() -> dict[str, Any]:
    """The latest measurement, which a test that just saved one always has."""
    snapshot = store.latest_snapshot()
    assert snapshot is not None, "a measurement was expected"
    return snapshot


def a_rule(path: Path) -> scan.Rule:
    """The rule that decides ``path``; a path no rule decides fails the test."""
    rule = scan.rule_for(path)
    assert rule is not None, f"no rule decides {path}"
    return rule


def a_saved_item(
    tmp_path: Path, name: str = "leftover-build", size: int = 4096
) -> tuple[str, Path]:
    """A real folder of ``size`` bytes for a measurement to list as a safe item: the path and the folder."""
    folder = tmp_path / name
    folder.mkdir()
    (folder / "blob.txt").write_bytes(b"x" * size)
    return str(folder), folder


class FakeShell:
    """Stands in for the shell's layout client: records a ``show`` and answers as told."""

    def __init__(self) -> None:
        self.shown: list[ShowArgs] = []
        self.refusal: ShellOpError | None = None

    def show(self, args: ShowArgs) -> None:
        if self.refusal is not None:
            raise self.refusal
        self.shown.append(args)


class FakeWorkspaceCommands:
    """Stands in for the workspace commands ``helper`` runs: records each call and answers as the test queues.

    A queued answer may be a ``CompletedProcess`` or an exception to raise (a timeout, say); with nothing queued,
    the call succeeds with one created chat's JSON line.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.answers: list[subprocess.CompletedProcess[str] | BaseException] = []

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        if self.answers:
            answer = self.answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        return subprocess.CompletedProcess(
            argv,
            0,
            json.dumps({"chat_id": "chat-abc", "name": "Free up disk space"}),
            "",
        )

    def queue(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.answers.append(subprocess.CompletedProcess([], returncode, stdout, stderr))

    def queue_chat(self, chat_id: str) -> None:
        self.queue(0, json.dumps({"chat_id": chat_id, "name": "Free up disk space"}))

    def creates(self) -> list[list[str]]:
        """The calls that asked the chat app for a new chat."""
        return [call for call in self.calls if "--create" in call]

    def messages(self) -> list[list[str]]:
        """The calls that sent a message to a chat that already existed."""
        return [
            call
            for call in self.calls
            if "--create" not in call and "workspace-layout" not in call
        ]


def install_fake_du(
    monkeypatch: Any, sizes_by_group: dict[str, dict[Path, int] | None]
) -> None:
    """Replaces ``scan.run_du``: each group answers the sizes planted for it, or fails when planted as None."""

    def fake_run_du(group: scan.Group, raw_path: Path) -> dict[Path, int] | None:
        raw_path.write_text(f"faked du output for {group.group_id}\n")
        return sizes_by_group.get(group.group_id)

    monkeypatch.setattr(scan, "run_du", fake_run_du)


def install_groups(monkeypatch: Any, *groups: scan.Group) -> None:
    """Replaces ``scan.groups``: a measurement over exactly the groups a test plants sizes for."""
    monkeypatch.setattr(scan, "groups", lambda: tuple(groups))


def install_disk(monkeypatch: Any, *figures: dict[str, int]) -> None:
    """Replaces ``scan.disk_usage``: answers each figure in turn, and the last one from then on."""
    remaining = list(figures)
    last = figures[-1]

    def fake_disk_usage() -> dict[str, int]:
        return remaining.pop(0) if len(remaining) > 1 else last

    monkeypatch.setattr(scan, "disk_usage", fake_disk_usage)
