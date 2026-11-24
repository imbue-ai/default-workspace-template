"""Deletes what the latest measurement listed, and nothing else.

A path is deleted only when it is an item of the latest measurement (a folder a rule decided) and
its verdict still allows it: "safe" always, "ask first" only when the caller says the user approved
it (the cleanup helper, after asking), "keep" never. Chats are never deleted here: the cleanup
helper ends a finished chat through the workspace, which removes its files properly.
"""

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Final

from disk_space import scan, store

UV_CLEAN_TIMEOUT_SECONDS: Final[float] = 300.0
# How long a polite clean may wait on the cache's in-use mark before the clean is forced.
UV_POLITE_WAIT_SECONDS: Final[float] = 10.0
FREED_SETTLE_SECONDS: Final[float] = 8.0
FREED_POLL_SECONDS: Final[float] = 0.5


def _is_backup_running() -> bool:
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if (entry / "comm").read_text().strip() == "restic":
                    return True
            except OSError:
                continue
    return False


def _cache_in_use(cache: Path) -> bool:
    """Whether a running program runs from, works in, or has a file open or loaded inside ``cache``."""
    prefix = str(cache).rstrip("/") + "/"
    # uv's in-use mark: every uv run holds the cache's lock file open for its whole run, so holding it is not use.
    lock_file = prefix + ".lock"
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            places = [os.readlink(entry / "exe"), os.readlink(entry / "cwd")]
            places += (
                (entry / "cmdline").read_bytes().decode(errors="replace").split("\0")
            )
            places += [
                line.rsplit(" ", 1)[-1]
                for line in (entry / "maps").read_text().splitlines()
            ]
            fd_dir = entry / "fd"
            places += [os.readlink(fd_dir / fd) for fd in os.listdir(fd_dir)]
        except OSError:
            continue
        if any(
            (place.startswith(prefix) and place != lock_file)
            or place == prefix.rstrip("/")
            for place in places
        ):
            return True
    return False


def _run_uv_clean(
    cache: Path, force: bool, timeout: float
) -> subprocess.CompletedProcess[str]:
    argv = [
        "uv",
        "cache",
        "clean",
        "--cache-dir",
        str(cache),
        *(["--force"] if force else []),
    ]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _clear_uv_cache(cache: Path) -> str | None:
    """Clears the Python package cache with uv; the reason it could not, or None.

    Every ``uv run`` keeps the cache marked in use for as long as its program runs, and a workspace always has one
    running, so a polite clean waits forever. After a short wait the clean is forced, but only when no running program
    actually uses a file inside the cache (a script's environment, a download in progress).
    """
    try:
        result = _run_uv_clean(cache, force=False, timeout=UV_POLITE_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        if _cache_in_use(cache):
            return "A running program is using the Python package cache right now; try again once it finishes."
        try:
            result = _run_uv_clean(cache, force=True, timeout=UV_CLEAN_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return "The Python package cache was busy; try again in a few minutes."
    except OSError:
        return "The Python package cache was busy; try again in a few minutes."
    return (
        None
        if result.returncode == 0
        else (result.stderr.strip().splitlines() or ["uv cache clean failed"])[-1]
    )


def _remove(path: Path, method: str) -> str | None:
    """Deletes ``path``; the reason it could not, or None."""
    if method == "uv-cache":
        return _clear_uv_cache(path)
    if path.name == "restic" and _is_backup_running():
        return "A backup is running right now; try again once it finishes."
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        else:
            shutil.rmtree(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        return f"Could not delete it: {error.strerror or error}"
    return None


def _freed_since(free_before: int) -> int:
    """How much the free space grew after a deletion. The disk's figures (a cloud machine's quota above all) catch
    up a few seconds late, so a figure that has not moved yet is read again for a few seconds before it is believed."""
    readings: list[int] = []

    def has_moved() -> bool:
        readings.append(max(0, scan.disk_usage()["free"] - free_before))
        return readings[-1] > 0

    store.poll_until(has_moved, FREED_SETTLE_SECONDS, FREED_POLL_SECONDS)
    return readings[-1]


def delete(
    paths: list[str], user_approved: bool, by: str, requested_by: str = ""
) -> dict[str, Any]:
    """Deletes each of ``paths`` the latest measurement allows; what was deleted, refused, and freed."""
    snapshot = store.latest_snapshot()
    deleted: list[dict[str, Any]] = []
    refused: list[dict[str, Any]] = []
    if snapshot is None:
        return {
            "deleted": [],
            "refused": [
                {"path": p, "reason": "Nothing has been measured yet."} for p in paths
            ],
            "freed_bytes": 0,
        }
    items = {
        node["path"]: node
        for node in snapshot["nodes"]
        if node["item"] and node["path"]
    }
    free_before = scan.disk_usage()["free"]
    for text in paths:
        node = items.get(text)
        if node is None:
            refused.append(
                {
                    "path": text,
                    "reason": "It is not one of the items the last measurement listed.",
                }
            )
            continue
        if node["verdict"] == scan.KEEP:
            refused.append(
                {"path": text, "reason": "It is in Keep: deleting it breaks things."}
            )
            continue
        if node["verdict"] == scan.ASK and not user_approved:
            refused.append(
                {
                    "path": text,
                    "reason": "It is in Ask first: only you can approve deleting it.",
                }
            )
            continue
        if not node["method"]:
            refused.append(
                {
                    "path": text,
                    "reason": "Chats are ended through the cleanup helper, not deleted here.",
                }
            )
            continue
        path = Path(text)
        if Path(os.path.realpath(path)) != path:
            refused.append(
                {
                    "path": text,
                    "reason": "It points somewhere else, so it is left alone.",
                }
            )
            continue
        reason = _remove(path, node["method"])
        if reason is not None:
            refused.append({"path": text, "reason": reason})
            continue
        deleted.append(
            {
                "path": text,
                "name": node["name"],
                "bytes": node["bytes"],
                "verdict": node["verdict"],
            }
        )
        _drop_from_snapshot(snapshot, node)
    freed = _freed_since(free_before) if deleted else 0
    if deleted:
        snapshot["disk"] = scan.disk_usage()
        store.rewrite_latest_snapshot(snapshot)
        names = ", ".join(item["name"] for item in deleted[:3]) + (
            "..." if len(deleted) > 3 else ""
        )
        who = "The cleanup helper" if by == "helper" else "You"
        store.append_history(
            "delete",
            f"{who} deleted {len(deleted)} item{'s' if len(deleted) != 1 else ''} ({names}).",
            freed_bytes=freed,
            deleted=deleted,
            refused=refused,
            by=by,
            requested_by=requested_by,
            at_disk=snapshot["disk"],
        )
    return {"deleted": deleted, "refused": refused, "freed_bytes": freed}


def _drop_from_snapshot(snapshot: dict[str, Any], node: dict[str, Any]) -> None:
    """Takes a deleted item and everything under it out of the stored tree, and its size out of its parents."""
    nodes: list[dict[str, Any]] = snapshot["nodes"]
    by_id = {n["id"]: n for n in nodes}
    gone = {node["id"]}
    changed = True
    while changed:
        changed = False
        for candidate in nodes:
            if candidate["parent"] in gone and candidate["id"] not in gone:
                gone.add(candidate["id"])
                changed = True
    parent = node["parent"]
    while parent is not None:
        by_id[parent]["bytes"] = max(0, by_id[parent]["bytes"] - node["bytes"])
        if node["verdict"] == scan.SAFE and "safe_bytes" in by_id[parent]:
            by_id[parent]["safe_bytes"] = max(
                0, by_id[parent]["safe_bytes"] - node["bytes"]
            )
        parent = by_id[parent]["parent"]
    snapshot["nodes"] = [n for n in nodes if n["id"] not in gone]
    snapshot["edited_at"] = time.time()
