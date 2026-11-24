"""The check the routine runs every 10 minutes: is the disk over the alert line?

Instant when the disk is fine. Over the line, it measures the disk first (unless a measurement is
under half an hour old) so the cleanup helper starts with fresh figures, then starts the helper.
It alerts once each time the disk crosses the line; while the disk stays over it, it nudges again
no sooner than ``renotify_hours`` later. The disk dropping back under the line re-arms the alert.
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Final

from disk_space import helper, scan, store

FRESH_SNAPSHOT_SECONDS: Final[float] = 30 * 60


def _format_gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB"


def run_check(now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    settings = store.read_settings()
    disk = scan.disk_usage()
    watch = store.read_watch()
    over = disk["percent"] >= settings["threshold"]
    store.update_watch(last_check_at=now, last_percent=disk["percent"])
    if not over:
        store.update_watch(armed=True)
        return {
            "summary": f"The disk is {disk['percent']}% full; the alert line is {settings['threshold']}%.",
            "alerted": False,
        }
    last_alert = watch.get("last_alert_at")
    due = (
        bool(watch.get("armed", True))
        or not isinstance(last_alert, (int, float))
        or now - last_alert >= settings["renotify_hours"] * 3600
    )
    if not due:
        return {
            "summary": f"The disk is {disk['percent']}% full; the cleanup helper was already started.",
            "alerted": False,
        }
    snapshot = store.latest_snapshot()
    if (
        snapshot is None
        or now - float(snapshot.get("taken_at", 0)) > FRESH_SNAPSHOT_SECONDS
    ):
        snapshot = scan.scan_locked() or store.latest_snapshot()
    reason = (
        f"The disk passed the alert line: it is {disk['percent']}% full ({_format_gb(disk['used'])} of "
        f"{_format_gb(disk['total'])}, {_format_gb(disk['free'])} free; the line is {settings['threshold']}%). "
        "Tell the user with a notification, open the Disk Space window beside this chat, and help them free space."
    )
    started = helper.start(reason, "check")
    store.update_watch(armed=False, last_alert_at=now)
    store.append_history(
        "alert",
        f"The disk passed {settings['threshold']}% ({disk['percent']}% full).",
        disk=disk,
        chat_id=started["chat_id"],
        helper_detail=started["detail"],
    )
    verb = "Started" if started["created"] else "Nudged"
    summary = (
        f"The disk is {disk['percent']}% full. {verb} the cleanup helper."
        if started["chat_id"]
        else f"The disk is {disk['percent']}% full, but the cleanup helper "
        f"could not start: {started['detail']}"
    )
    return {"summary": summary, "alerted": True, "chat_id": started["chat_id"]}


def write_routine_result(result: dict[str, Any]) -> None:
    """Leaves the run's one-line summary (and the helper chat) for the Routines app, when run as a routine."""
    run_dir = os.environ.get("ROUTINES_RUN_DIR")
    if not run_dir:
        return
    payload: dict[str, Any] = {"summary": result["summary"]}
    if result.get("chat_id"):
        payload["chat_id"] = result["chat_id"]
    (Path(run_dir) / "result.json").write_text(json.dumps(payload))
