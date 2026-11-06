"""The relay assignment: which relays this workspace's tunnels should dial right now.

Fetched from the connector's ``GET /shares/assignment`` (authenticated by the
share's relay token) and re-polled every ``poll_seconds``, so server-side fleet
changes converge without anyone touching the workspace. The last good answer
is cached on disk under ``data/.state/share_gateway/`` so a container restart
brings the tunnels up even while the connector is unreachable.
"""

import json
from pathlib import Path

import httpx
from share_grants.json_values import is_json_object

from share_gateway.log import log as _log

_ASSIGNMENT_TIMEOUT_SECONDS = 30.0

# Fallback when the connector's answer omits or corrupts poll_seconds.
DEFAULT_POLL_SECONDS = 60


_MEDIA_PROTOCOLS = ("tcp", "udp")


class PublishedEndpoint:
    """One published port of the workspace's host as the connector reports it: where a guest port is reachable."""

    def __init__(self, host: str, port: int, protocol: str, guest_port: int) -> None:
        self.host = host
        self.port = port
        # ``tcp`` or ``udp``, lower-case.
        self.protocol = protocol
        self.guest_port = guest_port

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, PublishedEndpoint):
            return NotImplemented
        return vars(self) == vars(other)

    def __repr__(self) -> str:
        return f"PublishedEndpoint({vars(self)!r})"


class RelayAssignment:
    """One assignment answer: the relay endpoints to tunnel to, the re-poll interval, and the Screen's media
    port and published endpoints when the connector knows them (specs/screen-studio-integration/spec.md
    section 6.1)."""

    def __init__(
        self,
        endpoint_by_relay_id: dict[str, tuple[str, int]],
        poll_seconds: int,
        media_port: int | None,
        published_endpoints: list[PublishedEndpoint],
    ) -> None:
        # relay_id -> (host, port) of that relay's tunnel-control endpoint.
        self.endpoint_by_relay_id = endpoint_by_relay_id
        self.poll_seconds = poll_seconds
        # The relay port every frpc claims for the Screen's ``screen-media`` tcp proxy; None from a connector
        # or a share without one, in which case no such proxy is rendered.
        self.media_port = media_port
        # The box's own published ports (a cloud workspace); empty for a local one.
        self.published_endpoints = published_endpoints

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, RelayAssignment):
            return NotImplemented
        return vars(self) == vars(other)


def _split_endpoint(endpoint: str) -> tuple[str, int] | None:
    host, separator, port_text = endpoint.rpartition(":")
    if not separator or not host or not port_text.isdigit():
        return None
    return (host, int(port_text))


def _parse_port(raw: object) -> int | None:
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and 0 < raw < 65536 else None


def _parse_published_endpoints(raw_endpoints: object) -> list[PublishedEndpoint]:
    """The ``published_endpoints`` entries that are whole; the key is absent from an older connector."""
    if not isinstance(raw_endpoints, list):
        return []
    endpoints: list[PublishedEndpoint] = []
    for entry in raw_endpoints:
        if not is_json_object(entry):
            continue
        host = entry.get("host")
        port = _parse_port(entry.get("port"))
        guest_port = _parse_port(entry.get("guest_port"))
        protocol = str(entry.get("protocol", "")).lower()
        if not isinstance(host, str) or not host or port is None or guest_port is None:
            continue
        if protocol not in _MEDIA_PROTOCOLS:
            continue
        endpoints.append(PublishedEndpoint(host=host, port=port, protocol=protocol, guest_port=guest_port))
    return endpoints


def parse_assignment(body: object) -> RelayAssignment | None:
    """Parse an assignment response body; None when it carries no usable relay endpoint."""
    if not is_json_object(body):
        return None
    raw_endpoints = body.get("relay_endpoints")
    if not isinstance(raw_endpoints, list):
        return None
    endpoint_by_relay_id: dict[str, tuple[str, int]] = {}
    for entry in raw_endpoints:
        if not is_json_object(entry):
            continue
        relay_id = entry.get("relay_id")
        split = _split_endpoint(str(entry.get("endpoint", "")))
        if isinstance(relay_id, str) and relay_id and split is not None:
            endpoint_by_relay_id[relay_id] = split
    if not endpoint_by_relay_id:
        return None
    raw_poll = body.get("poll_seconds")
    poll_seconds = raw_poll if isinstance(raw_poll, int) and raw_poll > 0 else DEFAULT_POLL_SECONDS
    return RelayAssignment(
        endpoint_by_relay_id=endpoint_by_relay_id,
        poll_seconds=poll_seconds,
        media_port=_parse_port(body.get("media_port")),
        published_endpoints=_parse_published_endpoints(body.get("published_endpoints")),
    )


def fetch_assignment(connector_url: str, relay_token: str) -> RelayAssignment | None:
    """One assignment fetch from the connector; None (logged) on any transport/shape failure."""
    try:
        response = httpx.get(
            f"{connector_url}/shares/assignment",
            headers={"Authorization": f"Bearer {relay_token}"},
            timeout=_ASSIGNMENT_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        _log(f"assignment fetch failed: {exc}")
        return None
    if response.status_code != 200:
        _log(f"assignment fetch rejected ({response.status_code}): {response.text[:200]}")
        return None
    try:
        body = response.json()
    except ValueError:
        _log("assignment fetch returned non-JSON")
        return None
    assignment = parse_assignment(body)
    if assignment is None:
        _log(f"assignment fetch returned no usable relay endpoints: {str(body)[:200]}")
    return assignment


def read_cached_assignment(cache_path: Path) -> RelayAssignment | None:
    if not cache_path.exists():
        return None
    try:
        raw = json.loads(cache_path.read_text())
    except (OSError, ValueError) as exc:
        _log(f"assignment cache unreadable: {exc}")
        return None
    return parse_assignment(raw)


def write_cached_assignment(cache_path: Path, assignment: RelayAssignment) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "relay_endpoints": [
            {"relay_id": relay_id, "endpoint": f"{host}:{port}"}
            for relay_id, (host, port) in sorted(assignment.endpoint_by_relay_id.items())
        ],
        "poll_seconds": assignment.poll_seconds,
        "media_port": assignment.media_port,
        "published_endpoints": [vars(endpoint) for endpoint in assignment.published_endpoints],
    }
    cache_path.write_text(json.dumps(payload))


def load_assignment(connector_url: str, relay_token: str, cache_path: Path) -> RelayAssignment | None:
    """Fetch the current assignment (caching a success); fall back to the cache when the connector is unreachable."""
    fetched = fetch_assignment(connector_url, relay_token)
    if fetched is not None:
        write_cached_assignment(cache_path, fetched)
        return fetched
    cached = read_cached_assignment(cache_path)
    if cached is not None:
        _log("using the cached relay assignment (connector unreachable)")
    return cached
