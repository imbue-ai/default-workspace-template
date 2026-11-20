"""Which loopback port a user app's main program gets.

A port is the lowest one at or above 8080 that nothing in the workspace holds: no supervisord
program registers on it, no registry row names it, no app record has it, it is not one of the
ports the Screen holds without a program line, and nothing is bound to it right now.
"""

import re
import socket
import tomllib
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from app_install.errors import PortUnavailableError
from app_install.paths import SUPERVISORD_CONF
from app_install.paths import SUPERVISORD_DROPIN_DIRECTORY
from app_install.records import HIGHEST_PORT
from app_install.records import AppRecord
from app_manifest.registry_location import registry_path

LOWEST_AUTOMATIC_PORT: Final[int] = 8080
# The Screen's CDP proxy and its video server: the first has no program line, and the second's port sits only in a
# comment of a program that is usually stopped, so neither a conf scan nor a bind check sees them.
RESERVED_PORTS: Final[frozenset[int]] = frozenset({8083, 8091})
_LOCALHOST_PORT_PATTERN: Final[re.Pattern[str]] = re.compile(r"http://(?:localhost|127\.0\.0\.1):(\d+)")


def is_port_bound(port: int) -> bool:
    """Whether something is listening on ``port`` on loopback right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def _supervisord_ports(repo_root: Path) -> set[int]:
    """Every port a program registers on: each registers its backend through a ``forward_port.py`` call naming
    ``http://localhost:<port>``."""
    files = [repo_root / SUPERVISORD_CONF]
    files.extend(sorted((repo_root / SUPERVISORD_DROPIN_DIRECTORY).glob("*.conf")))
    ports: set[int] = set()
    for path in files:
        if path.is_file():
            ports.update(int(match.group(1)) for match in _LOCALHOST_PORT_PATTERN.finditer(path.read_text()))
    return ports


def _registry_ports(repo_root: Path) -> set[int]:
    path = registry_path()
    registry = path if path.is_absolute() else repo_root / path
    if not registry.is_file():
        return set()
    try:
        rows = tomllib.loads(registry.read_text()).get("apps", [])
    except tomllib.TOMLDecodeError:
        # The registry is machine state the shell rebuilds; an unreadable one names no port.
        return set()
    ports: set[int] = set()
    for row in rows:
        match = _LOCALHOST_PORT_PATTERN.search(str(row.get("url", ""))) if isinstance(row, dict) else None
        if match is not None:
            ports.add(int(match.group(1)))
    return ports


def ports_in_use(repo_root: Path, records: Sequence[AppRecord]) -> set[int]:
    """Every port the workspace's files say is taken."""
    return (
        _supervisord_ports(repo_root)
        | _registry_ports(repo_root)
        | {record.port for record in records}
        | RESERVED_PORTS
    )


def allocate_port(
    repo_root: Path,
    records: Sequence[AppRecord],
    requested_port: int | None,
    is_bound: Callable[[int], bool],
) -> int:
    """``requested_port`` when it is free, else the lowest free port; raises PortUnavailableError for a requested
    port that is taken."""
    taken = ports_in_use(repo_root, records)
    if requested_port is not None:
        if requested_port in taken or is_bound(requested_port):
            raise PortUnavailableError(f"port {requested_port} is already in use by another app or service")
        return requested_port
    for port in range(LOWEST_AUTOMATIC_PORT, HIGHEST_PORT + 1):
        if port not in taken and not is_bound(port):
            return port
    raise PortUnavailableError(f"no free port at or above {LOWEST_AUTOMATIC_PORT}")
