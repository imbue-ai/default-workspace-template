"""Hostname coordinates: which service (if any) a request's Host header addresses.

The bare workspace domain is the shell; the label immediately before it names
a service; deeper labels are that service's own sub-origin space. This mirrors
the local ``[<labels>.]host-<hex>.localhost`` grammar and the frontend's
one-rule origin derivation.
"""


def service_for_host(host_header: str, workspace_domain: str) -> tuple[bool, str | None]:
    """Resolve a Host header against this share's workspace domain.

    Returns ``(is_ours, service_name)``: ``(True, None)`` for the bare shell
    origin, ``(True, name)`` for a service origin (deeper labels resolve to
    the label adjacent to the workspace domain), ``(False, None)`` for a host
    that does not belong to this workspace at all.
    """
    normalized = host_header.strip().lower().rstrip(".")
    host_without_port = normalized.rsplit(":", 1)[0] if ":" in normalized else normalized
    domain = workspace_domain.strip().lower()
    if host_without_port == domain:
        return True, None
    suffix = "." + domain
    if not host_without_port.endswith(suffix):
        return False, None
    label_chain = host_without_port[: -len(suffix)]
    if not label_chain:
        return False, None
    service_name = label_chain.rsplit(".", 1)[-1]
    if not service_name:
        return False, None
    return True, service_name


def workspace_origins_allow(origin_header: str, workspace_domain: str) -> bool:
    """Whether an Origin header names one of this workspace's own origins."""
    stripped = origin_header.strip().lower()
    scheme_separator = "://"
    if scheme_separator not in stripped:
        return False
    scheme, _, host_and_port = stripped.partition(scheme_separator)
    if scheme != "https":
        return False
    is_ours, _service = service_for_host(host_and_port, workspace_domain)
    return is_ours
