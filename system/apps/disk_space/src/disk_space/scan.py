"""Measures what fills the disk and sorts every part of it into safe to delete, ask first, or keep.

A measurement runs one ``du`` per group (the workspace, chats, scratch space, download caches,
installed tools, system software); each answers the size of every folder a few levels down, which
becomes the treemap's tree. What the groups do not cover is "Everything else": the disk's used
space less everything measured. Walking the disk takes a minute or two under gVisor, so a
measurement runs in the background and the page shows the last one with its time.

The verdicts come from ``RULES``: the first rule whose pattern matches a folder decides it, and
everything inside a decided folder shares its verdict. Only a folder a rule decided is an "item" in
the lists, and only an item can be deleted. A folder no rule names is kept.
"""

import fnmatch
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final, NamedTuple

from disk_headroom.space import read_disk_space

from disk_space import store

WORKSPACE: Final[Path] = Path("/home/user/workspace").resolve()
HOME: Final[Path] = Path("/home/user").resolve()
SAFE, ASK, KEEP = "safe", "ask", "keep"
# A rule with this verdict only names a folder; its children are judged on their own.
CONTAINER = ""
DU_TIMEOUT_SECONDS: Final[float] = 600.0
KIB: Final[int] = 1024
MB: Final[int] = 1024 * 1024
# A folder smaller than this, or than this share of its parent, joins its parent's "Smaller items".
MIN_NODE_BYTES: Final[int] = 25 * MB
MIN_NODE_SHARE: Final[float] = 0.01
# A scratch folder nobody has changed for this long, with no project history inside, is a leftover.
SCRATCH_STALE_DAYS: Final[int] = 7
# How hard the "changed recently" check looks before it gives up and calls the folder recent: entries visited, and
# seconds spent. A scratch folder too big to judge quickly is treated as still in use, never as safe to delete.
CHANGED_CHECK_ENTRIES: Final[int] = 2000
CHANGED_CHECK_SECONDS: Final[float] = 20.0


class Group(NamedTuple):
    group_id: str
    label: str
    paths: tuple[Path, ...]
    depth: int
    excludes: tuple[Path, ...] = ()


def groups() -> tuple[Group, ...]:
    home_skip = (
        WORKSPACE,
        HOME / ".mngr",
        HOME / ".cache",
        HOME / ".npm",
        HOME / "worktrees",
    )
    return (
        Group("workspace", "Your workspace", (WORKSPACE,), 3),
        Group("chats", "Chats", (HOME / ".mngr",), 2),
        Group("scratch", "Scratch space", (Path("/var/tmp"), HOME / "worktrees"), 1),
        Group(
            "caches",
            "Download caches",
            (
                Path("/var/cache"),
                Path("/root/.npm"),
                Path("/root/.cache"),
                HOME / ".cache",
                HOME / ".npm",
            ),
            3,
        ),
        Group(
            "tools",
            "Installed tools",
            (HOME, Path("/root"), Path("/opt")),
            1,
            excludes=(*home_skip, Path("/root/.npm"), Path("/root/.cache")),
        ),
        Group(
            "system",
            "System software",
            (Path("/usr"), Path("/var/lib"), Path("/var/log"), Path("/etc")),
            1,
        ),
    )


class Rule(NamedTuple):
    pattern: str
    verdict: str
    label: Callable[[Path], str]
    why: str
    # How an item is deleted: "remove" (the folder and all in it), "uv-cache" (uv's own clean), or
    # "" when the page never deletes it (chats are ended through the cleanup helper instead).
    method: str = "remove"


def _named(text: str) -> Callable[[Path], str]:
    return lambda _path: text


def _app_label(path: Path) -> str:
    return f"{path.name} app's data"


def _folder_label(path: Path) -> str:
    return f"Your folder: {path.name}"


def _chat_label(path: Path) -> str:
    try:
        data = json.loads((path / "data.json").read_text())
    except (OSError, ValueError):
        return f"Chat files ({path.name[:14]})"
    labels = data.get("labels") if isinstance(data.get("labels"), dict) else {}
    name = labels.get("display_name") or data.get("name") or path.name
    return f"Chat: {name}"


W, H = str(WORKSPACE), str(HOME)
RULES: Final[tuple[Rule, ...]] = (
    # Safe: download caches that refill themselves.
    Rule(
        "/var/cache/user/uv",
        SAFE,
        _named("Python package downloads"),
        "Downloaded again when needed; installed apps keep working.",
        "uv-cache",
    ),
    Rule(
        "/var/cache/user/huggingface",
        SAFE,
        _named("AI model downloads"),
        "Downloaded again the next time something needs them.",
    ),
    Rule(
        "/var/cache/user/restic",
        SAFE,
        _named("Backup cache"),
        "Rebuilt by the next backup; the backups themselves are not touched.",
    ),
    Rule(
        "/var/cache/user/npm",
        SAFE,
        _named("Node package downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        "/root/.npm/_cacache",
        SAFE,
        _named("Node package downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        f"{H}/.npm/_cacache",
        SAFE,
        _named("Node package downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        "/root/.cache/pip",
        SAFE,
        _named("Python package downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        f"{H}/.cache/pip",
        SAFE,
        _named("Python package downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        "/root/.cache/ms-playwright",
        SAFE,
        _named("Test browser downloads"),
        "Downloaded again when needed.",
    ),
    Rule(
        "/var/cache/apt/archives",
        SAFE,
        _named("System installer downloads"),
        "Downloaded again when needed.",
    ),
    # Ask first: the user's own things and copies only they can judge.
    Rule(
        f"{W}/.external_worktrees/*",
        ASK,
        lambda p: f"Project copy: {p.name}",
        "A separate copy of a project. Safe to delete once its changes are saved elsewhere.",
    ),
    Rule(
        f"{W}/data/.apps/*",
        ASK,
        _app_label,
        "Stored by the app. Some apps download it again; some lose it for good.",
    ),
    Rule(
        f"{W}/data/uploads",
        ASK,
        _named("Files you attached to chats"),
        "Only you know which you still need.",
    ),
    # Containers: folders whose own children are the items, so they decide nothing themselves.
    Rule(f"{W}/data/.apps", CONTAINER, _named("Apps' stored data"), ""),
    Rule(f"{W}/.external_worktrees", CONTAINER, _named("Project copies"), ""),
    Rule(f"{H}/.mngr/agents", CONTAINER, _named("Chats' files"), ""),
    Rule(f"{H}/worktrees", CONTAINER, _named("Background task copies"), ""),
    Rule("/var/cache/user", CONTAINER, _named("Download caches"), ""),
    Rule("/var/cache", CONTAINER, _named("System caches"), ""),
    Rule("/var/tmp", CONTAINER, _named("Scratch folders"), ""),
    Rule(f"{W}/data", CONTAINER, _named("Your data"), ""),
    Rule(f"{H}/.mngr/events", CONTAINER, _named("Activity logs"), ""),
    Rule(
        "/var/cache/user/venvs",
        KEEP,
        _named("Your apps' installed packages"),
        "The apps run on these.",
        "",
    ),
    Rule(
        f"{W}/data/.state",
        KEEP,
        _named("Workspace settings and state"),
        "The workspace needs it to run.",
        "",
    ),
    Rule(
        f"{W}/data/.tickets", KEEP, _named("Task notes"), "Small, and chats use it.", ""
    ),
    Rule(
        f"{W}/data/memories",
        KEEP,
        _named("Saved memories"),
        "What the chats remember about you.",
        "",
    ),
    Rule(
        f"{W}/data/.*",
        KEEP,
        lambda p: f"Workspace data: {p.name}",
        "Used by the workspace itself.",
        "",
    ),
    Rule(
        f"{W}/data/*",
        ASK,
        _folder_label,
        "A folder of yours. Only you know if it still matters.",
    ),
    Rule(
        f"{H}/.mngr/agents/*",
        ASK,
        _chat_label,
        "A chat's files. Old, finished chats can be cleared; the cleanup helper ends them properly.",
        "",
    ),
    Rule(
        f"{H}/worktrees/*",
        ASK,
        lambda p: f"Background task copy: {p.name}",
        "A working copy a background task made. Delete once that task is finished.",
    ),
    # Keep: what the workspace runs on.
    Rule(
        f"{W}/.git",
        KEEP,
        _named("History of changes"),
        "What lets any change be undone.",
        "",
    ),
    Rule(
        f"{W}/.venv",
        KEEP,
        _named("The workspace's installed packages"),
        "The chats and apps run on these.",
        "",
    ),
    Rule(
        f"{W}/system",
        KEEP,
        _named("Workspace code"),
        "The apps and services themselves.",
        "",
    ),
    Rule(f"{W}/.agents", KEEP, _named("Skills"), "What the chats know how to do.", ""),
)


def _matches(path: Path, pattern: str) -> bool:
    """Whether ``path`` matches ``pattern`` part by part, so ``*`` stands for exactly one folder name."""
    parts, pattern_parts = path.parts, Path(pattern).parts
    return len(parts) == len(pattern_parts) and all(
        fnmatch.fnmatchcase(part, wanted) for part, wanted in zip(parts, pattern_parts)
    )


def rule_for(path: Path) -> Rule | None:
    return next((rule for rule in RULES if _matches(path, rule.pattern)), None)


def scratch_verdict(path: Path, busy: set[Path]) -> tuple[str, str, str]:
    """The verdict, label and reason for one folder in the scratch space (/var/tmp)."""
    label = f"Scratch: {path.name}"
    if any(cwd == path or path in cwd.parents for cwd in busy):
        return ASK, label, "Something is running in it right now."
    if path.is_dir() and (path / ".git").exists():
        return (
            ASK,
            label,
            "A working copy of a project; it may hold changes saved nowhere else.",
        )
    if _changed_recently(path):
        return (
            ASK,
            label,
            f"Left over from recent work (changed in the last {SCRATCH_STALE_DAYS} days).",
        )
    return (
        SAFE,
        label,
        f"Leftover scratch files nothing has touched in over {SCRATCH_STALE_DAYS} days.",
    )


def _changed_recently(path: Path) -> bool:
    """Whether anything in ``path`` changed within the stale window, in one short walk that stops at the first hit.

    It walks the folder itself rather than running ``find``, which re-walks the whole tree: this stops as soon as it
    sees one recent entry, and gives up as recent after ``CHANGED_CHECK_ENTRIES`` entries or
    ``CHANGED_CHECK_SECONDS``, so a huge scratch folder cannot hold the measurement up. Symlinks are not followed,
    so a link elsewhere is never what makes a folder look used -- and never a loop.
    """
    cutoff = time.time() - SCRATCH_STALE_DAYS * 86400
    deadline = time.monotonic() + CHANGED_CHECK_SECONDS
    visited = 0
    pending = [path]
    while pending:
        entries = _entries_of(pending.pop())
        if entries is None:
            return True
        for entry in entries:
            visited += 1
            if _changed_since(entry, cutoff):
                return True
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            if visited >= CHANGED_CHECK_ENTRIES or time.monotonic() >= deadline:
                return True
    return False


def _entries_of(path: Path) -> list[os.DirEntry[str]] | None:
    """The entries of one folder; None when it cannot be read, which counts the folder as changed."""
    try:
        with os.scandir(path) as scanned:
            return list(scanned)
    except OSError:
        return None


def _changed_since(entry: os.DirEntry[str], cutoff: float) -> bool:
    """Whether one entry was modified at or after ``cutoff``; an entry that cannot be read counts as changed."""
    try:
        return entry.stat(follow_symlinks=False).st_mtime >= cutoff
    except OSError:
        return True


def busy_folders() -> set[Path]:
    """The working folders of every running process, so nothing in use is called safe."""
    folders: set[Path] = set()
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                folders.add(Path(os.readlink(entry / "cwd")))
            except OSError:
                continue
    return folders


def run_du(group: Group, raw_path: Path) -> dict[Path, int] | None:
    existing = [path for path in group.paths if path.exists()]
    if not existing:
        return {}
    command = [
        "du",
        "-xk",
        f"--max-depth={group.depth}",
        *(f"--exclude={p}" for p in group.excludes),
        *map(str, existing),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=DU_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw_path.write_text(result.stdout)
    sizes: dict[Path, int] = {}
    for line in result.stdout.splitlines():
        size_text, _, label = line.partition("\t")
        if size_text.isdigit():
            sizes[Path(label)] = int(size_text) * KIB
    return sizes


def disk_usage() -> dict[str, int]:
    """The workspace's disk as it can use it: on a cloud machine the disk quota when that runs out before
    the filesystem (``disk_headroom``), so the percentage is how close writes are to failing."""
    space = read_disk_space(WORKSPACE)
    percent = round(100 * space.used / space.total) if space.total else 0
    return {
        "total": space.total,
        "used": space.used,
        "free": space.free,
        "percent": percent,
    }


class TreeBuilder:
    """Turns ``du`` sizes into the flat node list the page draws and the lists read."""

    def __init__(self, busy: set[Path]) -> None:
        self.nodes: list[dict[str, Any]] = []
        self.busy = busy

    def add(
        self,
        parent: str | None,
        name: str,
        path: str,
        size: int,
        verdict: str,
        why: str,
        item: bool = False,
        method: str = "",
    ) -> str:
        node_id = f"n{len(self.nodes)}"
        self.nodes.append(
            {
                "id": node_id,
                "parent": parent,
                "name": name,
                "path": path,
                "bytes": size,
                "verdict": verdict,
                "why": why,
                "item": item,
                "deletable": item and verdict == SAFE and bool(method),
                "method": method,
            }
        )
        return node_id

    def add_path(
        self,
        parent: str,
        path: Path,
        sizes: dict[Path, int],
        inherited: tuple[str, str] | None,
    ) -> None:
        size = sizes.get(path, 0)
        rule = rule_for(path)
        item, method = False, ""
        name = path.name or str(path)
        if inherited is not None:
            verdict, why = inherited
        elif path.parent == Path("/var/tmp"):
            verdict, name, why = scratch_verdict(path, self.busy)
            item, method = True, "remove"
        elif rule is not None and rule.verdict == CONTAINER:
            verdict, why, name = KEEP, "", rule.label(path)
        elif rule is not None:
            verdict, why, name = rule.verdict, rule.why, rule.label(path)
            # A kept folder never carries a way to delete it, whatever the rule's default is.
            item, method = True, ("" if rule.verdict == KEEP else rule.method)
        else:
            verdict, why = KEEP, ""
        if (
            item
            and verdict == SAFE
            and any(cwd == path or path in cwd.parents for cwd in self.busy)
        ):
            verdict, why = ASK, "Something is running in it right now."
        node_id = self.add(parent, name, str(path), size, verdict, why, item, method)
        children = sorted(
            (p for p in sizes if p.parent == path), key=lambda p: sizes[p], reverse=True
        )
        floor = max(MIN_NODE_BYTES, MIN_NODE_SHARE * size)
        shown = [child for child in children if sizes[child] >= floor]
        decided = (verdict, why) if (item or inherited is not None) else None
        for child in shown:
            self.add_path(node_id, child, sizes, decided)
        rest = size - sum(sizes[child] for child in shown)
        if shown and rest >= MIN_NODE_BYTES:
            self.add(
                node_id,
                "Smaller items",
                "",
                rest,
                verdict,
                why or "Many small files and folders.",
            )


def measure() -> dict[str, Any]:
    """One full measurement, saved with its raw ``du`` output; returns the snapshot."""
    started = time.time()
    snapshot_dir = store.new_snapshot_dir()
    builder = TreeBuilder(busy_folders())
    disk = disk_usage()
    root = builder.add(None, "Whole disk", "", 0, KEEP, "")
    missing: list[str] = []
    measured_total = 0
    for group in groups():
        sizes = run_du(group, snapshot_dir / f"du-{group.group_id}.txt")
        if sizes is None:
            missing.append(group.label)
            continue
        tops = [path for path in group.paths if path in sizes]
        total = sum(sizes[path] for path in tops)
        measured_total += total
        if len(tops) == 1:
            sizes = dict(sizes)
            group_id = builder.add(root, group.label, "", total, KEEP, "")
            only = tops[0]
            for child in sorted(
                (p for p in sizes if p.parent == only),
                key=lambda p: sizes[p],
                reverse=True,
            ):
                if sizes[child] >= max(MIN_NODE_BYTES, MIN_NODE_SHARE * total):
                    builder.add_path(group_id, child, sizes, None)
            shown = sum(n["bytes"] for n in builder.nodes if n["parent"] == group_id)
            if total - shown >= MIN_NODE_BYTES:
                builder.add(
                    group_id,
                    "Smaller items",
                    "",
                    total - shown,
                    KEEP,
                    "Many small files and folders.",
                )
        else:
            group_id = builder.add(root, group.label, "", total, KEEP, "")
            for top in sorted(tops, key=lambda p: sizes[p], reverse=True):
                if sizes[top] >= MIN_NODE_BYTES:
                    builder.add_path(group_id, top, sizes, None)
    rest = disk["used"] - measured_total
    if rest > MIN_NODE_BYTES:
        builder.add(
            root,
            "Everything else",
            "",
            rest,
            KEEP,
            "System files, and space the disk reports as used that no folder here accounts for.",
        )
    builder.nodes[0]["bytes"] = sum(
        n["bytes"] for n in builder.nodes if n["parent"] == root
    )
    _fill_group_verdicts(builder.nodes)
    snapshot = {
        "id": snapshot_dir.name,
        "taken_at": time.time(),
        "took_seconds": round(time.time() - started, 1),
        "disk": disk,
        "missing": missing,
        "nodes": builder.nodes,
    }
    store.save_snapshot(snapshot_dir, snapshot)
    return snapshot


def _fill_group_verdicts(nodes: list[dict[str, Any]]) -> None:
    """A node no rule decided takes the verdict holding most of its space, for the treemap's colour."""
    by_id = {node["id"]: node for node in nodes}
    shares: dict[str, dict[str, int]] = {}
    for node in nodes:
        if not any(child["parent"] == node["id"] for child in nodes):
            parent = node["parent"]
            while parent is not None:
                bucket = shares.setdefault(parent, {SAFE: 0, ASK: 0, KEEP: 0})
                bucket[node["verdict"]] += node["bytes"]
                parent = by_id[parent]["parent"]
    for node_id, bucket in shares.items():
        node = by_id[node_id]
        node["safe_bytes"] = bucket[SAFE]
        if not node["item"] and not node["why"]:
            node["verdict"] = max(bucket, key=lambda verdict: bucket[verdict])


def scan_in_background() -> bool:
    """Starts a measurement in a separate process unless one is running; True when one was started."""
    if store.is_locked("scan"):
        return False
    # own-process: outlives-caller -- a measurement takes a minute or two, and the shell stops this app a minute
    # after its last window closes, so it runs detached (under the scan lock) rather than dying with the request.
    subprocess.Popen(
        [sys.executable, "-m", "disk_space.cli", "scan"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True


def scan_locked() -> dict[str, Any] | None:
    """One measurement under the scan lock; None when another is already running."""
    with store.held_lock("scan", wait=False) as acquired:
        if not acquired:
            return None
        store.update_watch(scan_started_at=time.time())
        try:
            return measure()
        finally:
            store.update_watch(scan_started_at=None)
