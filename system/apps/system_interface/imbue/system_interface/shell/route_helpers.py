"""The request helpers the shell's route modules share: status codes, the loopback gate, error bodies, and how an op
settles on the one client it targets (desktop contracts.md section 8)."""

from collections.abc import Mapping
from typing import Any
from typing import Final
from typing import TypeVar

from app_manifest.manifest import describe_validation_error
from flask import jsonify
from flask import request
from flask.typing import ResponseReturnValue
from pydantic import BaseModel
from pydantic import ValidationError

from imbue.imbue_common.pure import pure
from imbue.system_interface.shell.client_activity import find_client_id_for_page
from imbue.system_interface.shell.errors import ClientNotFoundError
from imbue.system_interface.shell.errors import InvalidShellValueError
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
TARGET_ARG_KEYS: Final[frozenset[str]] = frozenset({"client", "desktop"})

_RequestModel = TypeVar("_RequestModel", bound=BaseModel)


def detail_response(message: str, status_code: int) -> ResponseReturnValue:
    return jsonify({"detail": message}), status_code


def parse_request_body(model: type[_RequestModel]) -> _RequestModel:
    """The current request's body as ``model``; a body that is not a JSON object or not the shape raises
    InvalidShellValueError (a 400)."""
    # force=True: a script and curl alike may post without a JSON content type.
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        raise InvalidShellValueError("the request body must be a JSON object")
    try:
        return model.model_validate(body)
    except ValidationError as e:
        raise InvalidShellValueError(describe_validation_error(e)) from e


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
        attributed = find_client_id_for_page(shell.activity.read_events(), str(requester.app), requester.marker)
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
        ", ".join(f"{info['client_id']} (desktop={info['active_desktop']})" for info in connected_clients) or "none"
    )
    raise NoTargetClientError(
        "Could not tell which client this op is for: no client has messaged the requesting agent and "
        f"{len(connected_clients)} client(s) are connected. Pass --client <id> (see `layout.py context`). "
        f"Connected clients: {client_summary}."
    )
