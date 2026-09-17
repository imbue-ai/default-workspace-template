"""The tier's standing box registry: the one pool DB whose ``bare_metal_servers`` rows are the fleet.

Boxes are physical and belong to a tier, but a dynamic env (dev / ci) has its
own per-env ``host_pool`` database, so each env's connector needs a copy of the
tier's box rows to lease and release slices on them. The tier keeps the
canonical rows in a standing "infra" database whose pooled DSN is the tier's
``secrets/minds/<tier>/neon/DATABASE_URL`` Vault leaf (the same leaf the
shared tiers use for their single fleet database), and a dynamic env imports
those rows -- id-preserving, idempotent -- at deploy time and whenever the CI
release flow bakes (specs/remote-workspaces-in-ci.md).
"""

from collections.abc import Sequence
from typing import Any

import psycopg2
from loguru import logger
from pydantic import Field
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds.errors import MindError
from imbue.minds_admin.slices.bare_metal_db import fetch_servers
from imbue.minds_admin.slices.bare_metal_db import upsert_bare_metal_server
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.primitives import OVH_US_DATACENTER_CODES
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY


class BoxRegistryImportError(MindError):
    """Raised when the tier box registry cannot be read or its boxes cannot be imported into a pool DB."""


class SkippedRegistryBox(FrozenModel):
    """A registry row the target already registers under another row id, so it was left alone."""

    server: BareMetalServer = Field(description="The registry row that could not be imported")
    reason: str = Field(description="The database's own account of the collision")


class BoxRegistryImportReport(FrozenModel):
    """What one import of the tier registry's ready boxes into an env's pool DB did."""

    imported: tuple[BareMetalServer, ...] = Field(description="Rows upserted into the target, in registry order")
    skipped: tuple[SkippedRegistryBox, ...] = Field(description="Rows the target already held under another id")


@pure
def assert_registry_servers_importable(ready_servers: Sequence[BareMetalServer]) -> None:
    """Refuse a registry whose boxes sit in a datacenter the lease-region map does not know.

    Such a box could never be matched by any lease label, so the whole import
    is refused before any upsert rather than landing an unusable row.
    """
    unknown_datacenter_servers = [server for server in ready_servers if server.region not in OVH_US_DATACENTER_CODES]
    if unknown_datacenter_servers:
        raise BoxRegistryImportError(
            "refusing to import boxes whose datacenter is not in the region map "
            f"{sorted(OVH_US_DATACENTER_CODES)}: "
            + ", ".join(f"{server.id} ({server.region!r})" for server in unknown_datacenter_servers)
        )


def _connect_pool_database(dsn: SecretStr, role: str) -> Any:
    """Open a psycopg2 connection to a pool DB; a connection failure is a ``BoxRegistryImportError`` naming ``role``."""
    try:
        return psycopg2.connect(dsn.get_secret_value())
    except psycopg2.Error as exc:
        raise BoxRegistryImportError(f"could not connect to the {role}: {exc}") from exc


def fetch_ready_registry_servers(registry_dsn: SecretStr) -> list[BareMetalServer]:
    """The registry's ``ready`` boxes, checked to be importable."""
    registry_conn = _connect_pool_database(registry_dsn, "tier box registry")
    try:
        servers = fetch_servers(registry_conn)
    except psycopg2.Error as exc:
        raise BoxRegistryImportError(f"could not read bare_metal_servers from the tier box registry: {exc}") from exc
    finally:
        registry_conn.close()
    ready_servers = [server for server in servers if str(server.status) == SERVER_STATUS_READY]
    assert_registry_servers_importable(ready_servers)
    return ready_servers


def import_ready_servers(target_conn: Any, ready_servers: Sequence[BareMetalServer]) -> BoxRegistryImportReport:
    """Upsert each box into the target; a box the target already registers under another row id is skipped, not fatal."""
    imported: list[BareMetalServer] = []
    skipped: list[SkippedRegistryBox] = []
    for server in ready_servers:
        try:
            upsert_bare_metal_server(target_conn, server)
        except psycopg2.errors.UniqueViolation as exc:
            target_conn.rollback()
            reason = (exc.diag.message_detail or str(exc)).strip()
            logger.warning(
                "Skipping box {} ({}): the target already registers it under another row ({})",
                server.id,
                server.public_address,
                reason,
            )
            skipped.append(SkippedRegistryBox(server=server, reason=reason))
            continue
        except psycopg2.Error as exc:
            target_conn.rollback()
            raise BoxRegistryImportError(
                f"importing box {server.id} ({server.public_address}) into the pool DB failed: {exc}"
            ) from exc
        imported.append(server)
    return BoxRegistryImportReport(imported=tuple(imported), skipped=tuple(skipped))


def import_servers_into_pool_database(
    target_dsn: SecretStr, ready_servers: Sequence[BareMetalServer]
) -> BoxRegistryImportReport:
    """Upsert ``ready_servers`` into the pool DB at ``target_dsn`` (id-preserving, idempotent)."""
    target_conn = _connect_pool_database(target_dsn, "target pool DB")
    try:
        return import_ready_servers(target_conn, ready_servers)
    finally:
        target_conn.close()


def import_registry_boxes(registry_dsn: SecretStr, target_dsn: SecretStr) -> BoxRegistryImportReport:
    """Copy the tier registry's ready boxes into the pool DB at ``target_dsn`` (a no-op for an empty registry)."""
    return import_servers_into_pool_database(target_dsn, fetch_ready_registry_servers(registry_dsn))
