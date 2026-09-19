"""The request helpers the shell's route modules share: status codes, the loopback gate, error bodies, and how an op
settles on the one client it targets (contracts.md section 12, desktop contracts.md section 8)."""

from collections.abc import Mapping
from typing import Any
from typing import Final

from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue

from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.client_activity import find_client_id_for_instance
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import NoTargetClientError
from imbue.system_interface.shell.layout_ops import OpRequester
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.state import ShellState

LOOPBACK_CLIENT_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

HTTP_OK: Final[int] = 200
HTTP_CREATED: Final[int] = 201
HTTP_NO_CONTENT: Final[int] = 204
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_FORBIDDEN: Final[int] = 403
HTTP_NOT_FOUND: Final[int] = 404
HTTP_CONFLICT: Final[int] = 409
HTTP_PRECONDITION_FAILED: Final[int] = 412
HTTP_INTERNAL_ERROR: Final[int] = 500
HTTP_BAD_GATEWAY: Final[int] = 502
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

# The keys that pick an op's target rather than describe the op; stripped before the op's own arguments are read.
TARGET_ARG_KEYS: Final[frozenset[str]] = frozenset({"view", "client", "desktop"})


def detail_response(message: str, status_code: int) -> ResponseReturnValue:
    return jsonify({"detail": message}), status_code


def require_loopback() -> ResponseReturnValue | None:
    if (request.remote_addr or "") not in LOOPBACK_CLIENT_HOSTS:
        return detail_response("this route is only callable from loopback", HTTP_FORBIDDEN)
    return None


@pure
def op_only_args(args_raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in args_raw.items() if key not in TARGET_ARG_KEYS}


def is_known_client(shell: ShellState, client_id: str) -> bool:
    return shell.clients.get_client(client_id) is not None or client_id in shell.broadcaster.connected_client_ids()


def resolve_client(shell: ShellState, args_raw: Mapping[str, Any], requester: OpRequester | None) -> ClientId | None:
    """The client an op addresses: ``args.client``, else the client that last messaged the requester's chat, else
    the one connected client; None when nothing settles it."""
    explicit = args_raw.get("client")
    if isinstance(explicit, str) and explicit:
        # Held to the client id rule before it names a layout file.
        client_id = ClientId(explicit)
        if not is_known_client(shell, client_id):
            raise ClientNotFoundError(f"No client {client_id!r}: see `layout.py context` for the known clients")
        return client_id
    # Only a requester with a marker has a client that last messaged it; a bare app names none.
    if requester is not None and requester.marker:
        attributed = find_client_id_for_instance(shell.activity.read_events(), str(requester.app), requester.marker)
        if attributed is not None and is_known_client(shell, attributed):
            return ClientId(attributed)
    connected = shell.broadcaster.connected_client_ids()
    if len(connected) == 1:
        return ClientId(next(iter(connected)))
    return None


def require_client(shell: ShellState, args_raw: Mapping[str, Any], requester: OpRequester | None) -> ClientId:
    """Exactly one client, or a 412 that lists the connected ones: an op is never applied to a guessed client."""
    client_id = resolve_client(shell, args_raw, requester)
    if client_id is not None:
        return client_id
    connected_clients = shell.broadcaster.get_connected_client_infos()
    client_summary = (
        ", ".join(
            f"{info['client_id']} (view={info['active_view']}, desktop={info.get('active_desktop', '')}, "
            f"device={info['device_kind']})"
            for info in connected_clients
        )
        or "none"
    )
    raise NoTargetClientError(
        "Could not tell which client this op is for: no client has messaged the requesting agent and "
        f"{len(connected_clients)} client(s) are connected. Pass --client <id> (see `layout.py context`). "
        f"Connected clients: {client_summary}."
    )
