"""What the user shares, read through the gateway at most once every couple of seconds however many pages ask.

Each open Files page asks the server for the listing every few seconds while it is on screen. A read asks each
computer what it shares and how its syncs stand -- two short requests, both answered from memory on the computer --
and probes only the shares it has not seen yet (and, on a computer answering again after it did not, those that did
not answer), so a quiet listing costs two requests per computer. Nothing runs between asks: no page asking means no requests at all.
"""

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from files_app.connections import Computer
from files_app.connections import Connections
from files_app.connections import ProbeKey
from files_app.connections import SYNCED_FOLDERS
from files_app.connections import Share
from files_app.connections import assemble
from files_app.connections import listed_devices
from files_app.connections import probe_all
from files_app.connections import read_folder_syncs
from files_app.connections import read_grants
from files_app.connections import shares_to_probe
from files_app.gateway import Gateway

# How long a read answers every ask, so several pages asking at once cost one read.
MAX_AGE_SECONDS: Final[float] = 2.0

_NOTHING_SHARED: Final[Connections] = Connections(devices=(), shares=())


class ConnectionsCache:
    """The listing of what the user shares, and what the last read learned that the next one can skip."""

    def __init__(
        self,
        gateway: Gateway | None,
        agent_id: str,
        synced_root: Path = SYNCED_FOLDERS,
        max_age_seconds: float = MAX_AGE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._gateway = gateway
        self._agent_id = agent_id
        self._synced_root = synced_root
        self._max_age_seconds = max_age_seconds
        self._clock = clock
        # One read at a time; an ask that comes meanwhile waits for it and answers with what it read.
        self._lock = threading.Lock()
        self._listing: Connections | None = None
        self._read_at = 0.0
        self._computers: dict[str, Computer] = {}
        self._probed: dict[ProbeKey, Share] = {}

    def current(self) -> Connections:
        """The listing, read again unless the last read is recent. Raises ``httpx.HTTPError`` or ``ValueError`` when
        the gateway could not be read."""
        asked_at = self._clock()
        with self._lock:
            if self._listing is None or asked_at - self._read_at >= self._max_age_seconds:
                # Dated from its start, so an ask that waited for it is answered by it however long it took.
                started_at = self._clock()
                self._listing = self._read()
                self._read_at = started_at
            return self._listing

    def _read(self) -> Connections:
        gateway = self._gateway
        if gateway is None:
            return _NOTHING_SHARED
        listed = listed_devices(gateway)
        with ThreadPoolExecutor(max_workers=8) as pool:
            grants = {device: pool.submit(read_grants, gateway, device) for device, _name in listed}
            syncs = {
                device: pool.submit(read_folder_syncs, gateway, self._agent_id, device) for device, _name in listed
            }
            computers: dict[str, Computer] = {}
            for device, name in listed:
                granted = grants[device].result()
                last = self._computers.get(device)
                computers[device] = Computer(
                    id=device,
                    name=name,
                    # A computer that did not answer keeps what it shared when it last did, listed as not answering.
                    grants=granted if granted is not None else ({} if last is None else last.grants),
                    studio=syncs[device].result() if granted is not None else None,
                )
        answering = {device for device, computer in computers.items() if computer.studio is not None}
        back = answering - {device for device, computer in self._computers.items() if computer.studio is not None}
        needed = shares_to_probe(list(computers.values()))
        known = {
            key: share
            for key, share in self._probed.items()
            if key in needed and (share.is_reachable or key[0] not in back)
        }
        asked = {key for key in needed - known.keys() if key[0] in answering}
        self._computers = computers
        self._probed = {**known, **probe_all(gateway, asked)}
        return assemble(list(computers.values()), self._probed, self._synced_root)
