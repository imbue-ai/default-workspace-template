"""Bugsink REST API provisioning: the team, the per-service projects, and their DSNs.

Runs after ``observability bugsink deploy``: mint an API auth token by running
``bugsink-manage create_auth_token`` over SSH (the operator escape hatch; the
canonical REST API is loopback-only behind the caddy gate, so all API calls go
through an SSH tunnel), then get-or-create the team and projects and read each
project's DSN off its detail endpoint. Idempotent: teams/projects are
get-or-create by name, so a re-run returns the same DSNs.

The resulting ``{vault_key: dsn}`` map is emitted by the CLI for
``scripts/provision_bugsink_config.py store-dsns`` to write into the tier's
``sentry`` Vault entry (and, for dev, the ci tier's twin entry) -- see
specs/minds-bugsink-error-tracking.md.
"""

import re
import shlex
from typing import Any
from typing import Final

import httpx

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.observability.bugsink_remote_install import BUGSINK_HOME_DIR
from imbue.observability.bugsink_render import BUGSINK_ENV_FILE_PATH
from imbue.observability.errors import ObservabilityError
from imbue.observability.primitives import ObservabilityTierName
from imbue.observability.remote_install import run_ssh_command_capturing_output

# The one Bugsink team every instance carries; all per-service projects live
# under it.
BUGSINK_TEAM_NAME: Final[str] = "server"

# Bugsink project name -> the Vault key (in secrets/minds/<tier>/sentry) its
# DSN is written to. ``oauth-redirector`` only exists on the dev instance
# (the redirector is only deployed on dev/ci tiers).
SENTRY_DSN_VAULT_KEY_BY_PROJECT: Final[dict[str, str]] = {
    "rsc": "RSC_SENTRY_DSN",
    "llm": "LITELLM_SENTRY_DSN",
    "oauth-redirector": "OAUTH_REDIRECTOR_SENTRY_DSN",
}

_BUGSINK_API_BASE_PATH: Final[str] = "/api/canonical/0"
_BUGSINK_API_TIMEOUT_SECONDS: Final[float] = 30.0

# ``bugsink-manage create_auth_token`` prints exactly one 40-hex token line.
_AUTH_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{40}$", re.MULTILINE)


class BugsinkProvisioningError(ObservabilityError):
    """Raised when a Bugsink provisioning step fails."""


@pure
def bugsink_project_names_for_tier(tier: ObservabilityTierName) -> tuple[str, ...]:
    """The per-service Bugsink projects a tier's instance carries.

    ``oauth-redirector`` reports only from dev/ci deploys, which share the
    dev instance -- so its project exists only there.
    """
    if str(tier) == "dev":
        return ("rsc", "llm", "oauth-redirector")
    return ("rsc", "llm")


@pure
def parse_manage_auth_token(manage_output: str) -> str:
    """Extract the token ``bugsink-manage create_auth_token`` printed.

    Raises :class:`BugsinkProvisioningError` when no (or more than one)
    token-shaped line is present -- provisioning must never write a
    half-parsed value into Vault.
    """
    matches = _AUTH_TOKEN_PATTERN.findall(manage_output)
    if len(matches) != 1:
        raise BugsinkProvisioningError(
            f"Expected exactly one 40-hex auth-token line in the manage output, found {len(matches)}. "
            f"Output tail: {manage_output[-500:]!r}"
        )
    return matches[0]


def mint_bugsink_api_token_over_ssh(concurrency_group: ConcurrencyGroup, host: str, ssh_user: str) -> str:
    """Mint a fresh REST API auth token by running ``bugsink-manage create_auth_token`` on the host.

    The manage command needs the instance's Django environment, which lives
    in the root-only EnvironmentFile systemd reads -- so the invocation
    sources it under sudo, from the working directory holding the vendored
    settings module (exactly how the unit's own processes resolve it).
    """
    manage_invocation = (
        f"set -a; . {BUGSINK_ENV_FILE_PATH}; set +a; "
        f"cd {BUGSINK_HOME_DIR} && venv/bin/bugsink-manage create_auth_token"
    )
    output = run_ssh_command_capturing_output(
        concurrency_group, host, ssh_user, f"sudo bash -c {shlex.quote(manage_invocation)}"
    )
    return parse_manage_auth_token(output)


def _rows_from_list_response(payload: Any) -> list[dict[str, Any]]:
    """Normalize a DRF list response (bare list, or ``{"results": [...]}``)."""
    if isinstance(payload, dict):
        results = payload.get("results")
        if isinstance(results, list):
            return [row for row in results if isinstance(row, dict)]
        raise BugsinkProvisioningError(f"Unexpected Bugsink list response shape: {payload!r}")
    elif isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    else:
        raise BugsinkProvisioningError(f"Unexpected Bugsink list response shape: {payload!r}")


def _api_request(
    client: httpx.Client,
    *,
    base_url: str,
    token: str,
    method: str,
    path: str,
    json_body: dict[str, str] | None = None,
) -> Any:
    """One authenticated Bugsink REST API call; raises on any non-2xx response.

    The return value is the raw decoded JSON -- inherently untyped at this
    seam; callers validate the shape at runtime.
    """
    url = f"{base_url.rstrip('/')}{_BUGSINK_API_BASE_PATH}{path}"
    response = client.request(
        method,
        url,
        json=json_body,
        headers={"Authorization": f"Bearer {token}"},
        timeout=_BUGSINK_API_TIMEOUT_SECONDS,
    )
    if not response.is_success:
        raise BugsinkProvisioningError(
            f"Bugsink API {method} {path} returned {response.status_code}: {response.text[:300]}"
        )
    return response.json()


def _find_row_id_by_name(rows: list[dict[str, Any]], name: str) -> str | None:
    """The id of the first row whose ``name`` matches, or None.

    Any non-None id counts, whatever its JSON type -- skipping a name match
    would make the caller silently create a duplicate team/project.
    """
    for row in rows:
        if row.get("name") == name and row.get("id") is not None:
            return str(row["id"])
    return None


def get_or_create_team(client: httpx.Client, *, base_url: str, token: str, team_name: str) -> str:
    """Return the id of the named team, creating it when absent."""
    rows = _rows_from_list_response(_api_request(client, base_url=base_url, token=token, method="GET", path="/teams/"))
    team_id = _find_row_id_by_name(rows, team_name)
    if team_id is not None:
        return team_id
    created = _api_request(
        client, base_url=base_url, token=token, method="POST", path="/teams/", json_body={"name": team_name}
    )
    if not isinstance(created, dict) or created.get("id") is None:
        raise BugsinkProvisioningError(f"Bugsink team-create returned no id: {created!r}")
    return str(created["id"])


def get_or_create_project_dsn(
    client: httpx.Client, *, base_url: str, token: str, team_id: str, project_name: str
) -> str:
    """Return the named project's DSN, creating the project when absent.

    The DSN only appears on the project *detail* response, so a freshly
    created project is re-fetched by id.
    """
    rows = _rows_from_list_response(
        _api_request(client, base_url=base_url, token=token, method="GET", path="/projects/")
    )
    project_id = _find_row_id_by_name(rows, project_name)
    if project_id is None:
        created = _api_request(
            client,
            base_url=base_url,
            token=token,
            method="POST",
            path="/projects/",
            json_body={"name": project_name, "team": team_id},
        )
        if not isinstance(created, dict) or created.get("id") is None:
            raise BugsinkProvisioningError(f"Bugsink project-create returned no id: {created!r}")
        project_id = str(created["id"])
    detail = _api_request(client, base_url=base_url, token=token, method="GET", path=f"/projects/{project_id}/")
    if not isinstance(detail, dict) or not isinstance(detail.get("dsn"), str) or not detail["dsn"]:
        raise BugsinkProvisioningError(f"Bugsink project {project_name!r} detail carries no dsn: {detail!r}")
    return str(detail["dsn"])


def provision_bugsink_projects(
    client: httpx.Client, *, base_url: str, token: str, tier: ObservabilityTierName
) -> dict[str, str]:
    """Get-or-create the team + per-service projects; return the ``{vault_key: dsn}`` map."""
    team_id = get_or_create_team(client, base_url=base_url, token=token, team_name=BUGSINK_TEAM_NAME)
    dsn_by_vault_key: dict[str, str] = {}
    for project_name in bugsink_project_names_for_tier(tier):
        dsn = get_or_create_project_dsn(
            client, base_url=base_url, token=token, team_id=team_id, project_name=project_name
        )
        dsn_by_vault_key[SENTRY_DSN_VAULT_KEY_BY_PROJECT[project_name]] = dsn
    return dsn_by_vault_key
