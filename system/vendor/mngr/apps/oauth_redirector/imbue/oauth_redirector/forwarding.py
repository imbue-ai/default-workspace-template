"""Pure logic for the OAuth callback forwarder: state parsing + target validation.

The redirector deliberately does NOT verify the state JWT (each env signs
with its own key; the destination connector verifies). It only reads the
``cb`` claim to learn where to forward, then clamps that URL against the
tier's allowed-host pattern so it can never act as an open redirector.
"""

import base64
import json
import re
from urllib.parse import urlsplit

# The connector's registered Google callback path (see
# ``accounts_web.OAUTH_GOOGLE_CALLBACK_PATH`` in remote_service_connector;
# duplicated here because nothing else from the monorepo ships into this
# container).
CONNECTOR_CALLBACK_PATH = "/share/oauth/google/callback"


class StateParseError(ValueError):
    """Raised when the OAuth state does not carry a readable ``cb`` claim."""


def read_callback_url_from_state(state: str) -> str:
    """Extract the ``cb`` claim from a JWT's payload without verifying it."""
    parts = state.split(".")
    if len(parts) != 3:
        raise StateParseError("state is not a JWT")
    payload_b64 = parts[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError) as exc:
        raise StateParseError("state payload is not readable JSON") from exc
    if not isinstance(payload, dict):
        raise StateParseError("state payload is not a JSON object")
    callback_url = payload.get("cb")
    if not isinstance(callback_url, str) or not callback_url:
        raise StateParseError("state carries no cb claim")
    return callback_url


def is_allowed_forward_target(callback_url: str, allowed_host_pattern: re.Pattern[str]) -> bool:
    """Whether ``callback_url`` is a callback we may forward a provider response to.

    https only, host matching the tier's connector pattern, path exactly the
    registered callback path, and no query/fragment of its own (the provider's
    query string is appended verbatim by the forwarder).
    """
    parsed = urlsplit(callback_url)
    if parsed.scheme != "https":
        return False
    if parsed.query or parsed.fragment:
        return False
    if parsed.path != CONNECTOR_CALLBACK_PATH:
        return False
    host = (parsed.hostname or "").lower()
    return bool(host) and allowed_host_pattern.fullmatch(host) is not None
