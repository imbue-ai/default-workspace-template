"""Test helpers for layout.py: the shell's desktop-op answer and a window, as the wire spells them."""

from __future__ import annotations

from typing import Any


def desktop_answer(
    desktop_id: str = "home",
    client_id: str = "c1",
    windows: list[dict[str, Any]] | None = None,
    window_id: str | None = None,
    shortcuts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What the shell answers a desktop op with (desktop-interface contracts.md section 8)."""
    return {
        "ok": True,
        "desktop_id": desktop_id,
        "client_id": client_id,
        "desktop": {
            "id": desktop_id,
            "name": desktop_id.capitalize(),
            "wallpaper": None,
            "shortcuts": shortcuts or [],
            "windows": windows or [],
        },
        "layout": {"version": 1, "updated_at": None, "placements": []},
        "window_id": window_id,
    }


def window_json(window_id: str, app: str, path: str, title: str = "", is_settling: bool = False) -> dict[str, Any]:
    return {"id": window_id, "app": app, "path": path, "title": title, "opened_at": "2026-09-04T00:00:00+00:00", "is_settling": is_settling}
