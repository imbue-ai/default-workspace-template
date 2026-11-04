"""What the user has shared with this workspace from their computers, and how each item reaches it.

A share is a file-sharing permission the workspace holds on a device (``minds-file-server-read-<device id>:<path>``,
and ``-write-`` beside it when the user allowed changes; a device that predates per-device grants names no device id).
A shared folder may also be synced: Imbue Studio then keeps a copy of it at ``~/synced_folders/<device id>/<path>``,
two-way with write access and one-way (from the computer) with read only, and says how each sync stands when asked.
"""

import re
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterable
from collections.abc import Mapping
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from typing import Final
from typing import Literal

import httpx
from loguru import logger
from pydantic import ConfigDict

from files_app.gateway import Gateway
from files_app.gateway import device_files_path
from files_app.webdav import DavEntry
from files_app.webdav import PROPFIND_BODY
from files_app.webdav import parse_propfind
from imbue.imbue_common.frozen_model import FrozenModel

SYNCED_FOLDERS: Final[Path] = Path("/home/user/synced_folders")

# A device id never holds a ``:`` or a ``/``, and a path starts with ``/``, so the two names cannot be mistaken.
_RULE: Final[re.Pattern[str]] = re.compile(r"^minds-file-server-(read|write)-(?:([^:/]+):)?(/.*)$")

Access = Literal["read", "write"]
Kind = Literal["folder", "file", "unknown"]
SyncState = Literal[
    "STARTING", "RESTARTING", "SYNCING", "SYNCED", "DEACTIVATING", "DISCARDING", "STOPPED", "UNKNOWN", "FAILED"
]
# Each shared path, keyed with the device its grant names (None for a grant that names none), and its access.
Grants = dict[tuple[str | None, str], Access]


class FolderSync(FrozenModel):
    """A sync as Imbue Studio reports it (``GET /api/v1/agents/<agent id>/folder-syncs``): the part Files shows."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    state: SyncState
    # Why it failed, in Imbue Studio's words.
    message: str | None
    # How far the transfer under way has got; both null when none is.
    bytes_done: int | None
    bytes_total: int | None


class Device(FrozenModel):
    id: str
    name: str
    # Whether its file server is reached under its id, which a device that grants file sharing per device requires.
    has_device_paths: bool


class Share(FrozenModel):
    device: str
    path: str
    access: Access
    kind: Kind
    is_reachable: bool
    # Where the copy is, when there is one; a copy with no ``sync`` is one whose computer did not say how it stands.
    synced_copy: str | None
    sync: FolderSync | None


class Connections(FrozenModel):
    devices: tuple[Device, ...]
    shares: tuple[Share, ...]


def shared_paths(permissions: object) -> Grants:
    """Each shared path, keyed with the device its grant names (None for a grant that names none), and the access it
    was given, from a ``permissions/self`` answer: its ``rules`` are a list of ``{scope: [permission, ...]}``, and a
    path with both a read and a write permission has write access."""
    found: Grants = {}
    rules = permissions.get("rules", []) if isinstance(permissions, dict) else []
    for rule in rules if isinstance(rules, list) else []:
        for granted in rule.values() if isinstance(rule, dict) else []:
            for permission in granted if isinstance(granted, list) else []:
                match = _RULE.match(permission) if isinstance(permission, str) else None
                if match is None:
                    continue
                access, key = match.group(1), (match.group(2), match.group(3))
                if access == "write" or key not in found:
                    found[key] = "write" if access == "write" else "read"
    return found


def list_on_device(
    gateway: Gateway, device: str, has_device_paths: bool, path: str, depth: int
) -> list[DavEntry] | None:
    """The PROPFIND of a path on a device (the path first, then its children with depth 1); None when the device
    did not answer or refused."""
    try:
        response = gateway.request(
            "PROPFIND",
            device_files_path(path, device if has_device_paths else None),
            device=device,
            headers={"Depth": str(depth), "Content-Type": "application/xml"},
            content=PROPFIND_BODY,
        )
    except httpx.HTTPError as error:
        logger.debug("{} did not answer for {}: {}", device, path, error)
        return None
    if response.status_code != 207:
        logger.debug("{} answered {} for {}", device, response.status_code, path)
        return None
    try:
        return parse_propfind(response.content)
    except ElementTree.ParseError as error:
        logger.warning("{} sent a listing of {} that does not parse: {}", device, path, error)
        return None


class StudioSyncs(FrozenModel):
    """What Imbue Studio on a device answered for this workspace's syncs."""

    # By the path each copies on the computer; None for an answer in a shape Files does not read.
    syncs: dict[str, FolderSync] | None


_NOT_REPORTED: Final[StudioSyncs] = StudioSyncs(syncs=None)
# A gateway's word that the device did not answer: its computer is asleep, or its Imbue Studio is closed.
_NOT_ANSWERED: Final[frozenset[int]] = frozenset({502, 503, 504})


def read_folder_syncs(gateway: Gateway, agent_id: str, device: str) -> StudioSyncs | None:
    """The syncs Imbue Studio on a device keeps on for this workspace; None when it did not answer (asleep, closed)."""
    if agent_id == "":
        return _NOT_REPORTED
    path = f"minds-api-proxy/api/v1/agents/{urllib.parse.quote(agent_id, safe='')}/folder-syncs"
    try:
        response = gateway.request("GET", path, device=device)
    except httpx.HTTPError as error:
        logger.debug("{} did not say how its syncs stand: {}", device, error)
        return None
    if response.status_code in _NOT_ANSWERED:
        logger.debug("{} answered {} for its syncs", device, response.status_code)
        return None
    if response.status_code != 200:
        # A Studio that does not report its syncs, or a refusal: either way nothing to read.
        logger.debug("{} answered {} for its syncs", device, response.status_code)
        return _NOT_REPORTED
    try:
        body = response.json()
    except ValueError as error:
        logger.warning("{} sent its syncs in a shape Files does not read: {}", device, error)
        return _NOT_REPORTED
    return StudioSyncs(syncs=_syncs_of(device, body))


def _syncs_of(device: str, body: Any) -> dict[str, FolderSync] | None:
    try:
        return {
            sync["local_path"]: FolderSync.model_validate(sync)
            for sync in body["syncs"]
            if sync["activity"] == "ACTIVE"
        }
    except (ValueError, TypeError, KeyError) as error:
        # A pydantic ValidationError is a ValueError: a state this Files does not know is an answer it cannot read.
        logger.warning("{} sent its syncs in a shape Files does not read: {}", device, error)
        return None


def probe_share(gateway: Gateway, device: str, has_device_paths: bool, path: str, access: Access) -> Share:
    """A share as its device answers for it: what it is, and whether the device answered. Its copy is added after."""
    remote = list_on_device(gateway, device, has_device_paths, path, depth=0)
    if remote is None or len(remote) == 0:
        kind: Kind = "unknown"
    else:
        kind = "folder" if remote[0].is_folder else "file"
    return Share(
        device=device,
        path=path,
        access=access,
        kind=kind,
        is_reachable=remote is not None,
        synced_copy=None,
        sync=None,
    )


def with_sync(share: Share, syncs: dict[str, FolderSync] | None, synced_root: Path) -> Share:
    """``share`` with its copy and Studio's word on that copy's sync, from what its device said of its syncs."""
    copy = synced_root / share.device / share.path.lstrip("/")
    sync = None if syncs is None else syncs.get(share.path)
    if syncs is None:
        # With no word from the computer, a copy on disk is still a copy, of a sync whose state is unknown.
        synced_copy = f"{copy}/" if copy.is_dir() else None
    else:
        synced_copy = None if sync is None else f"{copy}/"
    # A share its device did not describe is a folder when there is a copy of it.
    kind = "folder" if share.kind == "unknown" and synced_copy is not None else share.kind
    return share.model_copy(update={"synced_copy": synced_copy, "sync": sync, "kind": kind})


def device_permissions(gateway: Gateway, device: str) -> Grants:
    response = gateway.request("GET", "permissions/self", device=device)
    response.raise_for_status()
    return shared_paths(response.json())


def read_grants(gateway: Gateway, device: str) -> Grants | None:
    """What a device shares with this workspace, or None when it did not answer (asleep, Imbue Studio closed). Raises
    ``httpx.HTTPError`` for any other refusal, and ``ValueError`` for an answer that is not JSON."""
    try:
        return device_permissions(gateway, device)
    except httpx.TransportError as error:
        logger.debug("{} did not say what it shares: {}", device, error)
        return None
    except httpx.HTTPStatusError as error:
        if error.response.status_code not in _NOT_ANSWERED:
            raise
        logger.debug("{} answered {} for what it shares", device, error.response.status_code)
        return None


def devices_with_device_paths(granted: Iterable[Grants]) -> set[str]:
    """The devices whose file server is reached under their id: those a grant names. A device that grants per device
    gave each grant from before the change a twin naming it, so every share on it is reached that way."""
    return {named for grants in granted for named, _path in grants if named is not None}


def listed_devices(gateway: Gateway) -> list[tuple[str, str]]:
    """Each of the user's devices, by its id and the name it goes by."""
    response = gateway.request("GET", "devices")
    response.raise_for_status()
    devices = response.json().get("devices", [])
    return [
        (device["device_id"], str(device.get("hostname") or device["device_id"]))
        for device in devices
        if isinstance(device, dict) and isinstance(device.get("device_id"), str)
    ]


# A share as it is probed: its device, its path, and whether the device is reached under its id.
ProbeKey = tuple[str, str, bool]


class Computer(FrozenModel):
    """A device as Files last heard from it: its grants, and Imbue Studio's word on its syncs (None while it does not
    answer)."""

    id: str
    name: str
    grants: Grants
    studio: StudioSyncs | None


def _shares_of(computers: Sequence[Computer]) -> tuple[list[ProbeKey], list[ProbeKey], dict[ProbeKey, Access]]:
    """The shares the grants make, as the keys they are probed by: those whose grant names their device, those whose
    grant names none (listed once per path, see ``_one_per_path``), and each one's access."""
    with_device_paths = devices_with_device_paths(computer.grants for computer in computers)
    named: dict[tuple[str, str], Access] = {}
    unnamed: list[tuple[str, str, Access]] = []
    for computer in computers:
        for (device, path), access in computer.grants.items():
            if device is None:
                unnamed.append((computer.id, path, access))
            elif access == "write" or (device, path) not in named:
                named[(device, path)] = access
    named_paths = {path for _device, path in named}
    access_of: dict[ProbeKey, Access] = {}
    exact: list[ProbeKey] = []
    for (device, path), access in sorted(named.items()):
        key = (device, path, device in with_device_paths)
        exact.append(key)
        access_of[key] = access
    ambiguous: list[ProbeKey] = []
    for device, path, access in unnamed:
        if path not in named_paths:
            key = (device, path, device in with_device_paths)
            ambiguous.append(key)
            access_of[key] = access
    return exact, ambiguous, access_of


def shares_to_probe(computers: Sequence[Computer]) -> set[ProbeKey]:
    exact, ambiguous, _access = _shares_of(computers)
    return {*exact, *ambiguous}


def probe_all(gateway: Gateway, keys: Iterable[ProbeKey]) -> dict[ProbeKey, Share]:
    """Each share as its device answers for it, all asked at once, since a sleeping computer answers slowly."""
    asked = list(keys)
    with ThreadPoolExecutor(max_workers=8) as pool:
        probed = pool.map(lambda key: probe_share(gateway, key[0], key[2], key[1], "read"), asked)
        return dict(zip(asked, probed, strict=True))


def assemble(
    computers: Sequence[Computer], probed: Mapping[ProbeKey, Share], synced_root: Path = SYNCED_FOLDERS
) -> Connections:
    """The listing of every share on every computer, from what each computer granted, how each share answered its
    probe, and what each computer's Imbue Studio last said of its syncs. A computer that is not answering has its
    shares listed as unreachable, and their copies' syncs as unknown."""
    exact, ambiguous, access_of = _shares_of(computers)
    by_id = {computer.id: computer for computer in computers}

    def described(keys: Iterable[ProbeKey]) -> list[Share]:
        shares = []
        for key in keys:
            device, path, _has_device_paths = key
            computer = by_id.get(device)
            studio = None if computer is None else computer.studio
            share = probed.get(key) or Share(
                device=device,
                path=path,
                access="read",
                kind="unknown",
                is_reachable=False,
                synced_copy=None,
                sync=None,
            )
            is_answering = computer is None or computer.studio is not None
            share = share.model_copy(
                update={"access": access_of[key], "is_reachable": share.is_reachable and is_answering}
            )
            shares.append(with_sync(share, None if studio is None else studio.syncs, synced_root))
        return shares

    with_device_paths = devices_with_device_paths(computer.grants for computer in computers)
    devices = tuple(
        Device(id=computer.id, name=computer.name, has_device_paths=computer.id in with_device_paths)
        for computer in computers
    )
    return Connections(devices=devices, shares=(*described(exact), *_one_per_path(described(ambiguous))))


def _one_per_path(shares: list[Share]) -> tuple[Share, ...]:
    """A path granted with no device id once, on the device it lives on. Such a grant does not name its device, so
    with several devices each lists it; the one with the synced copy, else the one that answered, else the first, is
    where it is."""
    chosen: dict[str, Share] = {}
    for share in shares:
        held = chosen.get(share.path)
        rank = (share.synced_copy is not None, share.is_reachable)
        if held is None or rank > (held.synced_copy is not None, held.is_reachable):
            chosen[share.path] = share
    return tuple(chosen.values())
