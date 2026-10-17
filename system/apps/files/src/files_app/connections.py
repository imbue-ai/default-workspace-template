"""What the user has shared with this workspace from their computers, and how each item reaches it.

A share is a file-sharing permission the workspace holds on a device (``minds-file-server-read-<path>``, and
``-write-`` beside it when the user allowed changes). A shared folder may also be synced: Imbue Studio then keeps a copy
of it at ``~/synced_folders/<device id>/<path>``, two-way with write access and one-way (from the computer) with read
only. The workspace learns nothing from the sync itself, so whether a copy has kept up is read off both sides: the
names in the copy against the names on the computer, when the computer answers.
"""

import re
import xml.etree.ElementTree as ElementTree
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final
from typing import Literal

import httpx

from files_app.gateway import Gateway
from files_app.gateway import device_files_path
from files_app.webdav import DavEntry
from files_app.webdav import PROPFIND_BODY
from files_app.webdav import parse_propfind
from imbue.imbue_common.frozen_model import FrozenModel

SYNCED_FOLDERS: Final[Path] = Path("/home/user/synced_folders")

_RULE: Final[re.Pattern[str]] = re.compile(r"^minds-file-server-(read|write)-(/.*)$")

# Names a sync leaves behind on purpose, so their absence from the copy is not a sign it has fallen behind.
_NOT_SYNCED_NAMES: Final[frozenset[str]] = frozenset({".DS_Store", ".git", ".localized"})

Access = Literal["read", "write"]
Kind = Literal["folder", "file", "unknown"]
# ``current``: the copy holds what the computer holds; ``behind``: it does not; ``unknown``: the computer did not
# answer. Only a synced share has one.
SyncState = Literal["current", "behind", "unknown"]


class Device(FrozenModel):
    id: str
    name: str


class Share(FrozenModel):
    device: str
    path: str
    access: Access
    kind: Kind
    is_reachable: bool
    synced_copy: str | None
    sync_state: SyncState | None


class Connections(FrozenModel):
    devices: tuple[Device, ...]
    shares: tuple[Share, ...]


def shared_paths(permissions: object) -> dict[str, Access]:
    """Each shared path and the access it was given, from a ``permissions/self`` answer: its ``rules`` are a list of
    ``{scope: [permission, ...]}``, and a path with both a read and a write permission has write access."""
    found: dict[str, Access] = {}
    rules = permissions.get("rules", []) if isinstance(permissions, dict) else []
    for rule in rules if isinstance(rules, list) else []:
        for granted in rule.values() if isinstance(rule, dict) else []:
            for permission in granted if isinstance(granted, list) else []:
                match = _RULE.match(permission) if isinstance(permission, str) else None
                if match is None:
                    continue
                access, path = match.group(1), match.group(2)
                if access == "write" or path not in found:
                    found[path] = "write" if access == "write" else "read"
    return found


def list_on_device(gateway: Gateway, device: str, path: str, depth: int) -> list[DavEntry] | None:
    """The PROPFIND of a path on a device (the path first, then its children with depth 1); None when the device
    did not answer or refused."""
    try:
        response = gateway.request(
            "PROPFIND",
            device_files_path(path),
            device=device,
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            content=PROPFIND_BODY,
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 207:
        return None
    try:
        return parse_propfind(response.content)
    except ElementTree.ParseError:
        return None


def _names_in_copy(copy: Path) -> set[str]:
    return {child.name for child in copy.iterdir() if not child.is_symlink()} - _NOT_SYNCED_NAMES


def describe_share(gateway: Gateway, device: str, path: str, access: Access, synced_root: Path) -> Share:
    copy = synced_root / device / path.lstrip("/")
    is_synced = copy.is_dir()
    remote = list_on_device(gateway, device, path, depth=1 if is_synced else 0)
    if remote is None or len(remote) == 0:
        kind: Kind = "folder" if is_synced else "unknown"
    else:
        kind = "folder" if remote[0].is_folder else "file"
    sync_state: SyncState | None = None
    if is_synced:
        if remote is None:
            sync_state = "unknown"
        else:
            on_computer = {entry.name for entry in remote[1:]} - _NOT_SYNCED_NAMES
            sync_state = "current" if on_computer <= _names_in_copy(copy) else "behind"
    return Share(
        device=device,
        path=path,
        access=access,
        kind=kind,
        is_reachable=remote is not None,
        synced_copy=f"{copy}/" if is_synced else None,
        sync_state=sync_state,
    )


def list_devices(gateway: Gateway) -> tuple[Device, ...]:
    response = gateway.request("GET", "devices")
    response.raise_for_status()
    devices = response.json().get("devices", [])
    return tuple(
        Device(id=device["device_id"], name=str(device.get("hostname") or device["device_id"]))
        for device in devices
        if isinstance(device, dict) and isinstance(device.get("device_id"), str)
    )


def list_connections(gateway: Gateway, synced_root: Path = SYNCED_FOLDERS) -> Connections:
    """Every share on every device, each probed on its device at once, since a sleeping computer answers slowly."""
    devices = list_devices(gateway)
    pending: list[tuple[str, str, Access]] = []
    for device in devices:
        response = gateway.request("GET", "permissions/self", device=device.id)
        response.raise_for_status()
        pending.extend((device.id, path, access) for path, access in sorted(shared_paths(response.json()).items()))
    with ThreadPoolExecutor(max_workers=8) as pool:
        shares = list(
            pool.map(lambda share: describe_share(gateway, share[0], share[1], share[2], synced_root), pending)
        )
    return Connections(devices=devices, shares=_one_per_path(shares))


def _one_per_path(shares: list[Share]) -> tuple[Share, ...]:
    """A path once, on the device it lives on. Permissions do not name a device, so with several devices each lists
    every path; the one with the synced copy, else the one that answered, else the first, is where it is."""
    chosen: dict[str, Share] = {}
    for share in shares:
        held = chosen.get(share.path)
        rank = (share.synced_copy is not None, share.is_reachable)
        if held is None or rank > (held.synced_copy is not None, held.is_reachable):
            chosen[share.path] = share
    return tuple(chosen.values())
