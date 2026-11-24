"""The one-glance answer another agent (the Caretaker) asks for: how full is the disk, and what can go?

Served at ``GET /api/status`` and printed by ``disk-space status --json``, so it is reachable whether
or not the app is running.
"""

import time
from typing import Any

from disk_space import scan, store

ITEMS_LISTED = 10


def build_status() -> dict[str, Any]:
    settings = store.read_settings()
    watch = store.read_watch()
    disk = scan.disk_usage()
    snapshot = store.latest_snapshot()
    items = [node for node in (snapshot or {}).get("nodes", []) if node["item"]]
    safe = sorted(
        (n for n in items if n["verdict"] == scan.SAFE),
        key=lambda n: n["bytes"],
        reverse=True,
    )
    ask = sorted(
        (n for n in items if n["verdict"] == scan.ASK),
        key=lambda n: n["bytes"],
        reverse=True,
    )

    def brief(node: dict[str, Any]) -> dict[str, Any]:
        return {
            "name": node["name"],
            "path": node["path"],
            "bytes": node["bytes"],
            "why": node["why"],
            "deletable": node["deletable"],
        }

    return {
        "percent_used": disk["percent"],
        "used_bytes": disk["used"],
        "free_bytes": disk["free"],
        "total_bytes": disk["total"],
        "threshold_percent": settings["threshold"],
        "is_over_threshold": disk["percent"] >= settings["threshold"],
        "measured_at": snapshot.get("taken_at") if snapshot else None,
        "measured_minutes_ago": round((time.time() - snapshot["taken_at"]) / 60)
        if snapshot
        else None,
        "safe_to_delete_bytes": sum(n["bytes"] for n in safe),
        "ask_first_bytes": sum(n["bytes"] for n in ask),
        "safe_to_delete": [brief(n) for n in safe[:ITEMS_LISTED]],
        "ask_first": [brief(n) for n in ask[:ITEMS_LISTED]],
        "last_check_at": watch.get("last_check_at"),
        "helper_chat_id": watch.get("helper_chat_id") or None,
        "how_to_free_space": (
            "POST /api/delete with the paths under safe_to_delete frees them at once; ask-first items need "
            "the user's yes, which the cleanup helper (POST /api/helper) asks for."
        ),
    }
