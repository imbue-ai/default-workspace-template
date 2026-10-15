"""What the app keeps on disk, under its data directory.

- ``state.json`` -- the user's choices: a pending access request, disconnected, uninstalled (and the icons that
  uninstalling took off, so Undo puts them back where they were).
- ``channels/<id>.json`` -- each Studio channel as Discord last returned it.
- ``messages/<id>.json`` -- every message (or forum post) ever read from that channel, exactly as Discord returned
  it, keyed by id, with the time of the last successful read. Kept so the window can still show something when
  Discord is unreachable, and so a change to how messages are shown needs no refetch.
"""

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_lock = threading.Lock()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _read(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True))
    os.replace(temporary, path)


class Store:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    # --- choices ---

    def state(self) -> dict[str, Any]:
        with _lock:
            return {
                "pending_request": None,
                "disconnected": False,
                "uninstalled": False,
                "removed_icons": [],
                **_read(self.data_dir / "state.json", {}),
            }

    def update_state(self, **changes: Any) -> dict[str, Any]:
        with _lock:
            current = {
                "pending_request": None,
                "disconnected": False,
                "uninstalled": False,
                "removed_icons": [],
                **_read(self.data_dir / "state.json", {}),
            }
            current.update(changes)
            _write(self.data_dir / "state.json", current)
            return current

    # --- raw Discord records ---

    def save_channel(self, channel_id: str, raw: dict[str, Any]) -> None:
        with _lock:
            _write(self.data_dir / "channels" / f"{channel_id}.json", raw)

    def channel(self, channel_id: str) -> dict[str, Any] | None:
        with _lock:
            return _read(self.data_dir / "channels" / f"{channel_id}.json", None)

    def save_messages(self, channel_id: str, raw_items: list[dict[str, Any]]) -> None:
        """Merge freshly read records into everything kept for the channel; mark the read time."""
        path = self.data_dir / "messages" / f"{channel_id}.json"
        with _lock:
            kept = _read(path, {"loaded_at": None, "items": {}})
            for item in raw_items:
                kept["items"][str(item["id"])] = item
            kept["loaded_at"] = now_iso()
            _write(path, kept)

    def messages(self, channel_id: str) -> dict[str, Any]:
        with _lock:
            return _read(self.data_dir / "messages" / f"{channel_id}.json", {"loaded_at": None, "items": {}})
