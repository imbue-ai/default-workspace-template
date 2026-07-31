"""Manage a shared workspace's materials in the agent's secrets directory.

The share-gateway service inside the workspace watches
``data/.secrets/share.env`` (relay coordinates + relay token): the whole share
stack starts when it appears and stops when it is removed. The grants document
lives next to it at ``data/.secrets/share_grants.toml`` and is re-read by the
gateway on every request, so a grants update takes effect immediately without
restarting anything.

Both files are written via ``mngr exec`` through the shared warm-process
``MngrCaller``, base64-encoded in transit so arbitrary emails and tokens never
need shell quoting.
"""

import base64
from typing import Final

from loguru import logger

from imbue.minds.utils.mngr_caller import MngrCaller
from imbue.mngr.primitives import AgentId

_SHARE_ENV_FILE: Final[str] = "data/.secrets/share.env"
_SHARE_GRANTS_FILE: Final[str] = "data/.secrets/share_grants.toml"

_SHARE_EXEC_TIMEOUT_SECONDS: Final[float] = 60.0


class ShareInjectionError(RuntimeError):
    """Raised when the share materials could not be written into the agent."""


def build_share_env_text(
    workspace_domain: str,
    relay_endpoint: str,
    relay_token: str,
    connector_url: str,
    broker_url: str,
) -> str:
    """Render share.env in the shape the workspace's share-gateway parses."""
    return (
        f"export SHARE_WORKSPACE_DOMAIN={workspace_domain}\n"
        f"export SHARE_RELAY_ENDPOINT={relay_endpoint}\n"
        f"export SHARE_RELAY_TOKEN={relay_token}\n"
        f"export SHARE_CONNECTOR_URL={connector_url}\n"
        f"export SHARE_BROKER_URL={broker_url}\n"
    )


def _toml_string_array(values: list[str]) -> str:
    """Render a list of plain strings as a TOML array (json-style quoting is valid TOML here)."""
    quoted = ", ".join('"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"' for value in values)
    return f"[{quoted}]"


def render_grants_toml(workspace_grants: dict[str, list[str]], service_grants: dict[str, dict[str, list[str]]]) -> str:
    """Render the grants document the workspace gateway evaluates.

    ``workspace_grants`` is ``{"emails": [...], "email_domains": [...]}``;
    ``service_grants`` maps service name to the same shape. Values are emitted
    as TOML string arrays (json-style quoting is valid TOML for plain strings).
    """
    lines = [
        "[workspace]",
        f"emails = {_toml_string_array(workspace_grants.get('emails', []))}",
        f"email_domains = {_toml_string_array(workspace_grants.get('email_domains', []))}",
    ]
    for service_name in sorted(service_grants):
        grants = service_grants[service_name]
        lines.append("")
        lines.append(f"[services.{_quote_toml_key(service_name)}]")
        lines.append(f"emails = {_toml_string_array(grants.get('emails', []))}")
        lines.append(f"email_domains = {_toml_string_array(grants.get('email_domains', []))}")
    return "\n".join(lines) + "\n"


def _quote_toml_key(key: str) -> str:
    if key.replace("-", "").replace("_", "").isalnum():
        return key
    return '"' + key.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _write_file_via_exec(agent_id: AgentId, relative_path: str, content: str, mngr_caller: MngrCaller) -> None:
    encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
    result = mngr_caller.call(
        [
            "exec",
            str(agent_id),
            f"mkdir -p data/.secrets && printf '%s' {encoded} | base64 -d > {relative_path}.tmp "
            f"&& mv {relative_path}.tmp {relative_path}",
        ],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ShareInjectionError(f"Failed to write {relative_path} into agent {agent_id}: {result.stderr.strip()}")


def inject_share_grants_into_agent(agent_id: AgentId, grants_toml_text: str, mngr_caller: MngrCaller) -> None:
    """Write (or replace) the grants document. Takes effect on the gateway's next request."""
    _write_file_via_exec(agent_id, _SHARE_GRANTS_FILE, grants_toml_text, mngr_caller)


def inject_share_materials_into_agent(agent_id: AgentId, share_env_text: str, mngr_caller: MngrCaller) -> None:
    """Write (or replace) share.env; the workspace's share-gateway brings the stack up."""
    _write_file_via_exec(agent_id, _SHARE_ENV_FILE, share_env_text, mngr_caller)


def has_share_materials_in_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> bool:
    """Whether share.env is present inside the agent (the share stack's on-switch).

    Distinguishes an actively-shared workspace from one whose earlier enable
    failed between the connector-side create and the injection. Conservative on
    exec failure: reported as absent, so the caller re-provisions -- which is
    safe, because the connector reuses the share row and the injection
    overwrites in place.
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"test -f {_SHARE_ENV_FILE}", "--no-start"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    return result.returncode == 0


def clear_share_materials_from_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> None:
    """Remove share.env + the grants file; the share-gateway tears the stack down.

    Best-effort: a failure leaves stale materials (the connector-side relay
    token is already deleted, so the tunnel's next reconnect is rejected
    anyway), which is logged but not fatal. ``--no-start``: clearing materials
    from a stopped container must not cold-boot anything.
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"rm -f {_SHARE_ENV_FILE} {_SHARE_GRANTS_FILE}", "--no-start"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        logger.warning("Failed to clear share materials from agent {}: {}", agent_id, result.stderr.strip())


def read_share_grants_from_agent(agent_id: AgentId, mngr_caller: MngrCaller) -> str | None:
    """Read the grants document back from the agent; None when absent.

    A failed exec raises :class:`ShareInjectionError` rather than returning
    None: "no document exists" and "the read never landed" must stay
    distinguishable, or a caller could mistake an unreadable policy for an
    empty one (the ``|| true`` folds the absent-file case into rc 0).
    """
    result = mngr_caller.call(
        ["exec", str(agent_id), f"cat {_SHARE_GRANTS_FILE} 2>/dev/null || true", "--no-start"],
        timeout=_SHARE_EXEC_TIMEOUT_SECONDS,
    )
    if result.returncode != 0:
        raise ShareInjectionError(
            f"Could not read share grants from agent {agent_id}: {result.stderr.strip() or 'exec failed'}"
        )
    return result.stdout if result.stdout.strip() else None
