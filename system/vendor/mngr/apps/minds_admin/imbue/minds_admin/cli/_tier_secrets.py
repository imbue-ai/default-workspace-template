"""Env-aware resolution of the secrets the operator commands need.

Every ``minds-admin`` command resolves its per-tier inputs (host_pool DSN, pool
SSH private key, connector URL, admin API key, OVH supplier credentials, the
box observability ingest credential) from the activated minds env -- Vault for
the shared tiers (staging / production), the per-env local state
(``secrets.toml`` / ``client.toml``) for dev / ci envs -- so operators never
hand-export them. Explicit flags and env vars (``--database-url`` /
``MINDS_HOST_POOL_DSN``, ``POOL_SSH_PRIVATE_KEY``, ``--connector-url`` /
``MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL``, ``--api-key`` /
``MINDS_ADMIN_KEY``, ``OVH_*``) always win, which keeps non-activated one-off
use working. (One deliberate asymmetry: the pool SSH key's Vault value wins
over its env var -- see :func:`resolve_pool_private_key_pem`.)
"""

import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Final

import click
from loguru import logger
from pydantic import AnyHttpUrl
from pydantic import AnyUrl
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.pure import pure
from imbue.minds.config.loader import load_client_config
from imbue.minds.config.loader import load_deploy_config
from imbue.minds.envs.paths import active_env_name_or_none
from imbue.minds.envs.primitives import VaultReadError
from imbue.minds.envs.primitives import VaultSecretNotFoundError
from imbue.minds.envs.vault_reader import VaultPath
from imbue.minds.envs.vault_reader import admin_key_from_supertokens_secret
from imbue.minds.envs.vault_reader import read_vault_kv
from imbue.minds_admin.cli._activated_env import PRODUCTION_ENV_NAME
from imbue.minds_admin.cli._activated_env import STAGING_ENV_NAME
from imbue.minds_admin.cli._activated_env import tier_for_env_name
from imbue.mngr_imbue_cloud.config import CONNECTOR_URL_ENV_VAR
from imbue.mngr_imbue_cloud.connector.client import ImbueCloudConnectorClient
from imbue.mngr_ovh.config import OvhProviderConfig
from imbue.observability.data_types import CollectorInstallConfig
from imbue.observability.primitives import CollectorRole
from imbue.observability.primitives import ObservabilityTierName

# Vault key the pool management SSH private key lives under (per
# host-pool-setup.md step 2). The connector deploys with this private key pushed
# to a Modal Secret; the slice bake authorizes the matching public key on the VM.
_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD: Final[str] = "POOL_SSH_PRIVATE_KEY"
# Env var carrying the pool management private key for non-activated one-off
# use. The activated tier's Vault value wins over it: the operator's mental
# model when running an activated ``minds-admin pool create`` is "this
# provisions hosts for the active tier", so the active tier's secrets are the
# source of truth, not a stale export from a different tier's session.
POOL_PRIVATE_KEY_ENV_VAR: Final[str] = "POOL_SSH_PRIVATE_KEY"
# Vault field (under ``<vault_prefix>/neon``) holding the pooled host_pool DSN.
_POOL_DSN_VAULT_FIELD: Final[str] = "DATABASE_URL"
# Env var name a minds-activated shell (or a non-activated operator) can use to
# supply the pool host DSN directly. Mirrors the field written into
# ``~/.minds-<env>/secrets.toml`` by ``minds-admin env deploy``.
_MINDS_HOST_POOL_DSN_ENV_VAR: Final[str] = "MINDS_HOST_POOL_DSN"
# Env vars the activation exports; used to locate the per-env local state.
_MINDS_ROOT_NAME_ENV_VAR: Final[str] = "MINDS_ROOT_NAME"
_MINDS_CLIENT_CONFIG_PATH_ENV_VAR: Final[str] = "MINDS_CLIENT_CONFIG_PATH"
_MINDS_PREFIX: Final[str] = "minds"

# Shared ``--database-url`` help text for every env-aware admin command.
# Hoisted to one constant so the subcommands' ``--help`` output can't drift.
DATABASE_URL_HELP: Final[str] = (
    "Neon PostgreSQL connection string for the pool DB. Optional: for "
    "staging/production it is read from Vault (secrets/minds/<tier>/neon); "
    "for dev/ci it auto-resolves from the activated env's secrets.toml. "
    "Pass explicitly (or export MINDS_HOST_POOL_DSN) only when overriding."
)


def _read_activated_minds_host_pool_dsn() -> str | None:
    """Return the activated dev/ci env's NEON_HOST_POOL_DSN from its secrets.toml, or None.

    Walks the same on-disk layout ``minds-admin env deploy`` writes:

        $HOME/.<MINDS_ROOT_NAME>/secrets.toml -> [secrets].NEON_HOST_POOL_DSN

    Returns None when ``MINDS_ROOT_NAME`` is unset, when the env root is
    production (``MINDS_ROOT_NAME=minds``, no per-env secrets.toml), when the
    file doesn't exist, or when the field is missing / empty.
    """
    root_name = os.environ.get(_MINDS_ROOT_NAME_ENV_VAR)
    if not root_name or root_name == _MINDS_PREFIX:
        return None
    secrets_path = Path.home() / f".{root_name}" / "secrets.toml"
    if not secrets_path.is_file():
        return None
    try:
        raw = tomllib.loads(secrets_path.read_text())
    except OSError as exc:
        logger.warning("Could not read {} for pool DSN resolution: {}", secrets_path, exc)
        return None
    except tomllib.TOMLDecodeError as exc:
        logger.warning(
            "Could not parse {} for pool DSN resolution ({}); pass --database-url explicitly.",
            secrets_path,
            exc,
        )
        return None
    secrets_block = raw.get("secrets")
    if not isinstance(secrets_block, dict):
        return None
    dsn = secrets_block.get("NEON_HOST_POOL_DSN")
    if not isinstance(dsn, str) or not dsn:
        return None
    return dsn


def _read_shared_tier_host_pool_dsn_from_vault(env_name: str) -> str:
    """Read a shared tier's host_pool DSN from ``<vault_prefix>/neon/DATABASE_URL``.

    The shared tiers (staging / production) keep no local ``secrets.toml``, so
    their DSN comes from the same Vault entry the connector and the deploy use.

    Raises ``click.ClickException`` if the Vault read fails or the entry lacks
    a non-empty ``DATABASE_URL``.
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/neon"))
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the host_pool DSN from Vault ({vault_prefix}/neon) for env '{env_name}': {exc}"
        ) from exc
    dsn = secret.get(_POOL_DSN_VAULT_FIELD, "")
    if not dsn:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/neon is missing {_POOL_DSN_VAULT_FIELD!r}; "
            "see apps/minds/docs/deploy/host-pool-setup.md step 3 for the schema."
        )
    return dsn


def resolve_pool_database_url(explicit: str | None) -> str:
    """Resolve the pool DSN for a pool/server command.

    Precedence (highest first): explicit ``--database-url``, then
    ``$MINDS_HOST_POOL_DSN``, then the activated minds env -- Vault
    (``<vault_prefix>/neon/DATABASE_URL``) for the shared tiers, the per-env
    ``secrets.toml`` ``NEON_HOST_POOL_DSN`` for dev/ci envs -- else a useful
    error. ``$DATABASE_URL`` is intentionally NOT consulted (a generic env var
    that might point at an unrelated DB); ``MINDS_HOST_POOL_DSN`` is the
    explicit opt-in for non-activated operators.
    """
    if explicit:
        return explicit
    env_value = os.environ.get(_MINDS_HOST_POOL_DSN_ENV_VAR)
    if env_value:
        return env_value
    env_name = active_env_name_or_none()
    if env_name is not None:
        if tier_for_env_name(env_name) in (PRODUCTION_ENV_NAME, STAGING_ENV_NAME):
            return _read_shared_tier_host_pool_dsn_from_vault(env_name)
        activated_dsn = _read_activated_minds_host_pool_dsn()
        if activated_dsn:
            return activated_dsn
    raise click.ClickException(
        "No pool DSN available. Either pass --database-url explicitly, export "
        f"{_MINDS_HOST_POOL_DSN_ENV_VAR}=<dsn>, or `minds-admin env activate <env>` "
        "first (dev deploys write the DSN into the per-env secrets.toml; "
        "staging/production resolve it from Vault)."
    )


def read_pool_private_key_from_vault(
    env_name: str,
    *,
    parent_cg: ConcurrencyGroup | None = None,
) -> str:
    """Read the activated tier's pool management private key PEM from Vault.

    Reads ``<vault_path_prefix>/pool-ssh/POOL_SSH_PRIVATE_KEY`` -- the same entry
    ``minds-admin env deploy`` pushes into the ``pool-ssh-<tier>`` Modal Secret the
    connector loads, so the key the slice bake authorizes on the VM matches the one
    the connector SSHes with at lease/release time. The slice backend needs the
    private key itself to SSH the box and carve the lima VM.

    Raises ``click.ClickException`` if the entry lacks the private-key field.
    Raises ``VaultReadError`` for any underlying Vault read failure.
    """
    tier = tier_for_env_name(env_name)
    deploy_config = load_deploy_config(tier)
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    secret = read_vault_kv(VaultPath(f"{vault_prefix}/pool-ssh"), parent_concurrency_group=parent_cg)
    private_key = secret.get(_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD, "")
    if not private_key:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/pool-ssh is missing {_POOL_MGMT_PRIVATE_KEY_VAULT_FIELD!r}; "
            "see apps/minds/docs/deploy/host-pool-setup.md step 2 for the schema."
        )
    return private_key


def read_pool_private_key_from_vault_or_fail(env_name: str) -> str:
    """Like :func:`read_pool_private_key_from_vault`, but any Vault read failure becomes a ``ClickException``.

    The CLI-facing form shared by every command that SSHes a box with the
    tier's pool key.
    """
    try:
        return read_pool_private_key_from_vault(env_name)
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the pool SSH private key from Vault for env '{env_name}': {exc}"
        ) from exc


def resolve_pool_private_key_pem() -> str:
    """Resolve the pool management private key: activated tier's Vault entry, else ``$POOL_SSH_PRIVATE_KEY``.

    The activated tier's Vault value deliberately wins over the env var (see
    :data:`POOL_PRIVATE_KEY_ENV_VAR`); the env var is the non-activated
    one-off escape hatch.
    """
    env_name = active_env_name_or_none()
    if env_name is not None:
        return read_pool_private_key_from_vault_or_fail(env_name)
    pem = os.environ.get(POOL_PRIVATE_KEY_ENV_VAR)
    if pem:
        return pem
    raise click.ClickException(
        f"No pool management key available: `minds-admin env activate <env>` first (the key is "
        f"read from the tier's Vault entry), or export {POOL_PRIVATE_KEY_ENV_VAR}=<pem> for "
        "non-activated one-off use."
    )


def resolve_admin_connector_url(explicit: str | None) -> str:
    """Resolve the connector base URL for a connector-admin command.

    Precedence: explicit ``--connector-url``, then
    ``$MNGR__PROVIDERS__IMBUE_CLOUD__CONNECTOR_URL``, then the activated env's
    ``client.toml`` (via ``$MINDS_CLIENT_CONFIG_PATH``, exported by activation).
    """
    if explicit:
        return explicit.rstrip("/")
    env_value = os.environ.get(CONNECTOR_URL_ENV_VAR)
    if env_value:
        return env_value.rstrip("/")
    config_path = os.environ.get(_MINDS_CLIENT_CONFIG_PATH_ENV_VAR)
    if config_path:
        return str(load_client_config(Path(config_path)).connector_url).rstrip("/")
    raise click.ClickException(
        "No connector URL configured: `minds-admin env activate <env>` first, pass "
        f"--connector-url <url>, or set ${CONNECTOR_URL_ENV_VAR}."
    )


def resolve_admin_api_key_value(explicit: str | None) -> str:
    """Resolve the connector admin API key (``MINDS_ADMIN_KEY``) for an admin command.

    Precedence: explicit ``--api-key``, then ``$MINDS_ADMIN_KEY`` (with the
    deprecated ``$MINDS_PAID_ADMIN_KEY`` spelling still accepted, warned), then
    the activated tier's ``<vault_prefix>/supertokens`` Vault entry -- the same
    value the connector loads as a Modal Secret.
    """
    if explicit:
        return explicit
    env_value = os.environ.get("MINDS_ADMIN_KEY")
    if env_value:
        return env_value
    legacy_value = os.environ.get("MINDS_PAID_ADMIN_KEY")
    if legacy_value:
        logger.warning(
            "Admin API key found under deprecated env var $MINDS_PAID_ADMIN_KEY; rename it to $MINDS_ADMIN_KEY"
        )
        return legacy_value
    env_name = active_env_name_or_none()
    if env_name is None:
        raise click.ClickException(
            "No admin API key: `minds-admin env activate <env>` first (the key is read from the "
            "tier's supertokens Vault entry), pass --api-key, or set $MINDS_ADMIN_KEY."
        )
    tier = tier_for_env_name(env_name)
    vault_prefix = str(load_deploy_config(tier).vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/supertokens"))
        return admin_key_from_supertokens_secret(secret, vault_prefix)
    except VaultReadError as exc:
        raise click.ClickException(f"Could not read the admin API key from Vault for env '{env_name}': {exc}") from exc


def make_admin_connector_client(connector_url: str | None) -> ImbueCloudConnectorClient:
    return ImbueCloudConnectorClient(base_url=AnyUrl(resolve_admin_connector_url(connector_url)))


# The Vault fields of a tier's ``<vault_prefix>/ovh`` entry that the bare-metal
# ordering flows require (schema: .minds/template/ovh.sh; the entry's
# OVH_CLOUD_PROJECT_ID is relay-only and not needed here).
_REQUIRED_OVH_VAULT_FIELDS: Final[tuple[str, ...]] = (
    "OVH_APPLICATION_KEY",
    "OVH_APPLICATION_SECRET",
    "OVH_CONSUMER_KEY",
)


@pure
def ovh_config_from_vault_secret(secret: Mapping[str, str], vault_prefix: str) -> OvhProviderConfig:
    """Build an :class:`OvhProviderConfig` from a tier's ``<vault_prefix>/ovh`` Vault entry.

    Raises ``click.ClickException`` naming the missing field(s) when the entry
    does not carry the full AK/AS/CK trio, so a half-populated entry fails with
    the schema pointer instead of a confusing OVH auth error later.
    """
    missing_fields = [field for field in _REQUIRED_OVH_VAULT_FIELDS if not secret.get(field)]
    if missing_fields:
        raise click.ClickException(
            f"Vault entry {vault_prefix}/ovh is missing {', '.join(missing_fields)}; "
            "see .minds/template/ovh.sh for the schema and "
            "apps/minds/docs/deploy/host-pool-setup.md step 3 for how to populate it."
        )
    return OvhProviderConfig(
        application_key=SecretStr(secret["OVH_APPLICATION_KEY"]),
        application_secret=SecretStr(secret["OVH_APPLICATION_SECRET"]),
        consumer_key=SecretStr(secret["OVH_CONSUMER_KEY"]),
    )


def resolve_ovh_config() -> OvhProviderConfig:
    """Resolve the OVH supplier credentials for a ``minds-admin server`` command.

    Precedence (highest first): the existing ``OVH_APPLICATION_KEY`` /
    ``OVH_APPLICATION_SECRET`` / ``OVH_CONSUMER_KEY`` env vars (the
    non-activated one-off escape hatch, same as every other env-var override
    here), then the activated tier's ``<vault_prefix>/ovh`` Vault entry, else a
    useful error.
    """
    env_config = OvhProviderConfig()
    if env_config.has_explicit_credentials():
        return env_config
    env_name = active_env_name_or_none()
    if env_name is None:
        raise click.ClickException(
            "No OVH credentials found. `minds-admin env activate <env>` first (the credentials are "
            "read from the tier's ovh Vault entry), or export OVH_APPLICATION_KEY / "
            "OVH_APPLICATION_SECRET / OVH_CONSUMER_KEY for non-activated one-off use."
        )
    tier = tier_for_env_name(env_name)
    vault_prefix = str(load_deploy_config(tier).vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/ovh"))
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the OVH credentials from Vault ({vault_prefix}/ovh) for env '{env_name}': {exc}. "
            "Populate the entry (see host-pool-setup.md step 3) or export the OVH_* env vars."
        ) from exc
    return ovh_config_from_vault_secret(secret, vault_prefix)


# Vault field (under ``secrets/minds/<observability tier>/observability``)
# holding the boxes sender class's complete Authorization header value. Empty or
# absent means the tier has no box observability yet: the collector install is
# a clean skip, never a failure.
_BOXES_INGEST_CREDENTIAL_VAULT_FIELD: Final[str] = "INGEST_CREDENTIAL_BOXES"


@pure
def observability_tier_for_env_name(env_name: str) -> ObservabilityTierName:
    """Map an env to the tier whose observability instance its boxes report to.

    There is exactly one instance per observability tier: production and
    staging have their own; every dev-* and ci-* env shares the single ``dev``
    instance (mirrors ``_derive_observability_tier`` in the relay recipes).
    """
    tier = tier_for_env_name(env_name)
    if tier in (PRODUCTION_ENV_NAME, STAGING_ENV_NAME):
        return ObservabilityTierName(tier)
    return ObservabilityTierName("dev")


@pure
def boxes_collector_install_config_from_secret(
    secret: Mapping[str, str],
    observability_tier: ObservabilityTierName,
    telemetry_domain: str,
    # None when the tier's boxes ingest credential is still empty (clean skip)
) -> CollectorInstallConfig | None:
    """Build the box collector-install config from a tier's observability Vault entry."""
    credential = secret.get(_BOXES_INGEST_CREDENTIAL_VAULT_FIELD, "")
    if not credential:
        return None
    return CollectorInstallConfig(
        role=CollectorRole.BOX,
        tier=observability_tier,
        ingest_url=AnyHttpUrl(f"https://telemetry.{telemetry_domain}"),
        ingest_authorization_header_value=SecretStr(credential),
    )


def resolve_boxes_collector_install_config_or_none() -> CollectorInstallConfig | None:
    """Resolve the activated tier's box observability collector install, or None to skip cleanly.

    The in-process port of ``provision_observability_config.py collector-env
    <tier> boxes``: reads the observability tier's
    ``<vault_prefix>/observability`` Vault entry and returns the collector
    install config when the tier carries a boxes ingest credential. Returns
    None -- a clean skip, logged -- when no env is activated, the tier has no
    observability Vault entry, or its boxes credential is still empty. Any
    other Vault failure raises: with observability configured the collector is
    fail-closed, so a transient Vault error must not silently skip it.
    """
    env_name = active_env_name_or_none()
    if env_name is None:
        logger.info(
            "Skipping the observability collector install: no minds env is activated, so there is no tier to "
            "resolve the ingest credential from (activate the tier, or pass a self-rendered install script "
            "via --extra-prep-script)."
        )
        return None
    observability_tier = observability_tier_for_env_name(env_name)
    deploy_config = load_deploy_config(str(observability_tier))
    vault_prefix = str(deploy_config.vault_path_prefix).rstrip("/")
    try:
        secret = read_vault_kv(VaultPath(f"{vault_prefix}/observability"))
    except VaultSecretNotFoundError:
        logger.info(
            "Skipping the observability collector install: tier '{}' has no observability Vault entry yet",
            observability_tier,
        )
        return None
    except VaultReadError as exc:
        raise click.ClickException(
            f"Could not read the observability Vault entry ({vault_prefix}/observability) for tier "
            f"'{observability_tier}': {exc}. The collector is fail-closed once configured, so this is "
            "not skippable -- fix the Vault read (or deactivate observability for the tier) and re-run."
        ) from exc
    config = boxes_collector_install_config_from_secret(
        secret, observability_tier, str(deploy_config.cloudflare_domain)
    )
    if config is None:
        logger.info(
            "Skipping the observability collector install: tier '{}' has no {} yet",
            observability_tier,
            _BOXES_INGEST_CREDENTIAL_VAULT_FIELD,
        )
    return config
