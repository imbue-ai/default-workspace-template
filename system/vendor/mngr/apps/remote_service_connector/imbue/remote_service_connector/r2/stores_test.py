import threading
from pathlib import Path
from typing import Any

import pytest

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.errors import R2EnforcementLeaseLostError
from imbue.remote_service_connector.errors import R2EnforcementLeaseUnavailableError
from imbue.remote_service_connector.r2.stores import PostgresKeyStore
from imbue.remote_service_connector.r2.stores import PostgresLeaseStore
from imbue.remote_service_connector.r2.stores import r2_enforcement_lease
from imbue.remote_service_connector.r2.sweep import enforce_owner_key_access
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import InMemoryLeaseStore
from imbue.remote_service_connector.testing import make_fake_lease_store
from imbue.remote_service_connector.testing import make_fake_pool_backend


def test_enforcement_leases_migration_matches_lease_store_and_pending_markers() -> None:
    """Guard against the migration and the lease store / marker values drifting apart."""
    migrations_dir = Path(__file__).parent.parent.parent.parent / "migrations"
    migration_sql = (migrations_dir / "033_r2_enforcement_leases.sql").read_text().lower()
    assert "create table r2_enforcement_leases" in migration_sql
    for column in ("owner_user_id", "claim_id", "expires_at"):
        assert column in migration_sql, f"lease column {column!r} missing from the migration"
    # The widened CHECK constraints must admit every marker value the code writes.
    assert "'pending'" in migration_sql
    assert "'pending_read'" in migration_sql
    assert "'pending_disabled'" in migration_sql


def test_enforcement_lease_is_released_on_exit() -> None:
    store = make_fake_lease_store()
    with r2_enforcement_lease("owner-1", wait_timeout_seconds=0.0, store=store):
        assert "owner-1" in store.claim_by_owner
    assert store.claim_by_owner == {}


def test_enforcement_lease_raises_unavailable_when_held_past_the_wait_window() -> None:
    store = make_fake_lease_store()
    store.claim_by_owner["owner-1"] = "someone-else"
    with pytest.raises(R2EnforcementLeaseUnavailableError):
        with r2_enforcement_lease("owner-1", wait_timeout_seconds=0.0, store=store):
            raise AssertionError("the body must not run without the lease")


def test_enforcement_lease_renewal_raises_after_takeover_and_release_spares_the_thief() -> None:
    store = make_fake_lease_store()
    with pytest.raises(R2EnforcementLeaseLostError):
        with r2_enforcement_lease("owner-1", wait_timeout_seconds=0.0, store=store) as lease:
            lease.renew_or_raise()
            # A takeover (only possible after expiry) replaces the claim.
            store.claim_by_owner["owner-1"] = "thief-claim"
            lease.renew_or_raise()
    # The superseded holder's release must not evict the new holder's claim.
    assert store.claim_by_owner == {"owner-1": "thief-claim"}


class _SignalingLeaseStore(InMemoryLeaseStore):
    """Lease store that signals the first time an acquire attempt is refused."""

    def __init__(self) -> None:
        super().__init__()
        self.acquire_blocked = threading.Event()

    def try_acquire(self, owner_user_id: str, claim_id: str, duration_seconds: float) -> bool:
        is_acquired = super().try_acquire(owner_user_id, claim_id, duration_seconds)
        if not is_acquired:
            self.acquire_blocked.set()
        return is_acquired


def test_enforcement_lease_strictly_serializes_concurrent_holders() -> None:
    """A second holder blocks (polling) until the first releases; the sections never interleave."""
    store = _SignalingLeaseStore()
    events: list[str] = []
    events_lock = threading.Lock()
    first_inside = threading.Event()
    release_first = threading.Event()

    def first_holder() -> None:
        with r2_enforcement_lease("owner-1", wait_timeout_seconds=30.0, store=store):
            with events_lock:
                events.append("first-enter")
            first_inside.set()
            release_first.wait(timeout=30.0)
            with events_lock:
                events.append("first-exit")

    def second_holder() -> None:
        with r2_enforcement_lease("owner-1", wait_timeout_seconds=30.0, store=store):
            with events_lock:
                events.append("second-enter")

    first_thread = threading.Thread(target=first_holder)
    first_thread.start()
    assert first_inside.wait(timeout=10.0)
    second_thread = threading.Thread(target=second_holder)
    second_thread.start()
    # Only release the first holder once the second has actually been refused
    # the lease, so the test proves blocking rather than lucky ordering.
    assert store.acquire_blocked.wait(timeout=10.0)
    release_first.set()
    first_thread.join(timeout=30.0)
    second_thread.join(timeout=30.0)
    assert events == ["first-enter", "first-exit", "second-enter"]


def _installed_backend(monkeypatch: pytest.MonkeyPatch) -> FakePoolBackend:
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return backend


def test_postgres_lease_store_acquire_renew_release_semantics(monkeypatch: pytest.MonkeyPatch) -> None:
    """The SQL-backed store over the fake DB: acquire excludes, takeover needs expiry, renew proves continuity."""
    backend = _installed_backend(monkeypatch)
    store = PostgresLeaseStore()
    assert store.try_acquire("owner-1", "claim-a", 180.0) is True
    # A live claim excludes everyone else (and even a re-acquire of the same claim).
    assert store.try_acquire("owner-1", "claim-b", 180.0) is False
    assert store.renew("owner-1", "claim-a", 180.0) is True
    assert store.renew("owner-1", "claim-b", 180.0) is False
    # Expiry (a test knob on the fake) lets a contender take over, after
    # which the original claim can no longer renew or release the lease.
    backend.enforcement_lease_expired_owners.add("owner-1")
    assert store.try_acquire("owner-1", "claim-b", 180.0) is True
    assert store.renew("owner-1", "claim-a", 180.0) is False
    store.release("owner-1", "claim-a")
    assert backend.enforcement_lease_claim_by_owner == {"owner-1": "claim-b"}
    store.release("owner-1", "claim-b")
    assert backend.enforcement_lease_claim_by_owner == {}


class _ConnectionObservingCloudflareOps(FakeCloudflareOps):
    """Cloudflare fake recording how many fake DB connections are open at each policy write."""

    def __init__(self, backend: FakePoolBackend) -> None:
        super().__init__()
        self._backend = backend
        self.open_connections_at_update: list[int] = []

    def update_bucket_token_access(self, token_id: str, bucket_name: str, access: str, token_name: str) -> None:
        self.open_connections_at_update.append(self._backend.open_connection_count)
        super().update_bucket_token_access(token_id, bucket_name, access, token_name)


def test_no_db_connection_is_held_across_cloudflare_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """The enforcement critical section must not pin a DB connection while Cloudflare is called.

    This is the regression pin for replacing the advisory-lock context
    manager (which parked one pooled connection in an open transaction for
    the whole body) with the lease: with the real Postgres-backed lease and
    key stores in the loop, every Cloudflare policy write must observe zero
    open DB connections.
    """
    backend = _installed_backend(monkeypatch)
    ops = _ConnectionObservingCloudflareOps(backend)
    rows: list[dict[str, Any]] = []
    for idx in range(3):
        bucket_name = f"u1prefix--data-{idx}"
        token = ops.create_bucket_token(bucket_name, "readwrite", f"mngr-r2:{bucket_name}:default")
        rows.append(
            {
                "access_key_id": str(token["id"]),
                "owner_user_id": "user-1",
                "bucket_name": bucket_name,
                "access": "readwrite",
                "alias": "default",
                "created_at": f"2026-01-01T00:00:0{idx}+00:00",
                "enforced_access": None,
                "suspension_access": None,
            }
        )
    counters = {"keys_downgraded": 0, "keys_restored": 0, "key_update_failures": 0}
    with r2_enforcement_lease("user-1", wait_timeout_seconds=1.0, store=PostgresLeaseStore()) as lease:
        enforce_owner_key_access(ops, PostgresKeyStore(), rows, True, counters, lease)
    assert counters["keys_downgraded"] == 3
    assert ops.open_connections_at_update == [0, 0, 0]
    assert backend.open_connection_count == 0
