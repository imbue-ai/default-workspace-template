"""Origin policy for shared-workspace requests.

Cross-site request forgery protection at the gateway: WebSocket upgrades must
present an Origin belonging to this workspace (browsers always send one);
non-GET requests may omit Origin (curl, same-origin form posts on old
browsers) but a *present* foreign Origin is rejected; plain GETs are exempt
(they are safe methods and panels legitimately hot-link resources).
"""

from collections.abc import Mapping

from share_gateway.hostnames import workspace_origins_allow

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


def is_request_origin_allowed(
    method: str,
    origin_header: str | None,
    is_websocket_upgrade: bool,
    workspace_domain: str,
    label_to_name: Mapping[str, str],
    auth_label: str,
    chrome_origin: str = "",
) -> bool:
    """Whether a request's Origin may reach this workspace's services.

    ``chrome_origin`` is the hosted web chrome (from ``SHARE_CHROME_ORIGIN``):
    it drives the workspace cross-origin (health probes, the owner-exec
    channel), so a matching Origin is allowed alongside the workspace's own
    origin family. Empty (a share created without a chrome) allows nothing
    extra.
    """
    if is_websocket_upgrade:
        if not origin_header:
            return False
        return _origin_allowed(origin_header, workspace_domain, label_to_name, auth_label, chrome_origin)
    if method.upper() in _SAFE_METHODS:
        return True
    if not origin_header:
        return True
    return _origin_allowed(origin_header, workspace_domain, label_to_name, auth_label, chrome_origin)


def _origin_allowed(
    origin_header: str,
    workspace_domain: str,
    label_to_name: Mapping[str, str],
    auth_label: str,
    chrome_origin: str,
) -> bool:
    if chrome_origin and origin_header == chrome_origin:
        return True
    return workspace_origins_allow(origin_header, workspace_domain, label_to_name, auth_label)
