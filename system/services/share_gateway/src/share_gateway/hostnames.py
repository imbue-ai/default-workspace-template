"""Hostname coordinates: which service (if any) a request's Host header addresses.

Every service origin is an unguessable ``<label>.<ws-domain>`` where ``label``
is ``<name>-<rand>``; the label maps back to the service *name* (grants are
keyed by name) via the app registry. The dedicated ``auth-<rand>`` label is the
gateway's own auth origin and never a grantable service. The bare workspace
domain does not route on a share (no frpc claim), and only single-label origins
are claimed, so deeper labels are not ours.
"""

from collections.abc import Mapping


def service_for_host(
    host_header: str,
    workspace_domain: str,
    label_to_name: Mapping[str, str],
    auth_label: str,
) -> tuple[bool, str | None]:
    """Resolve a Host header against this share's workspace domain.

    Returns ``(is_ours, service_name)``: ``(True, name)`` for a registered
    service's label origin, ``(True, None)`` for the dedicated auth label
    (ours but not a grantable service), and ``(False, None)`` for the bare
    domain, an unknown label, a deeper (unclaimed) sub-origin, or a host that
    does not belong to this workspace at all.
    """
    normalized = host_header.strip().lower().rstrip(".")
    host_without_port = normalized.rsplit(":", 1)[0] if ":" in normalized else normalized
    domain = workspace_domain.strip().lower()
    suffix = "." + domain
    if not host_without_port.endswith(suffix):
        return False, None
    label_chain = host_without_port[: -len(suffix)]
    # Only single-label origins are claimed on the relay; a deeper chain (a dot
    # remains) is not ours.
    if not label_chain or "." in label_chain:
        return False, None
    if label_chain == auth_label.strip().lower():
        return True, None
    service_name = label_to_name.get(label_chain)
    if service_name is None:
        return False, None
    return True, service_name


def workspace_origins_allow(
    origin_header: str,
    workspace_domain: str,
    label_to_name: Mapping[str, str],
    auth_label: str,
) -> bool:
    """Whether an Origin header names one of this workspace's own origins."""
    stripped = origin_header.strip().lower()
    scheme_separator = "://"
    if scheme_separator not in stripped:
        return False
    scheme, _, host_and_port = stripped.partition(scheme_separator)
    if scheme != "https":
        return False
    is_ours, _service = service_for_host(host_and_port, workspace_domain, label_to_name, auth_label)
    return is_ours
