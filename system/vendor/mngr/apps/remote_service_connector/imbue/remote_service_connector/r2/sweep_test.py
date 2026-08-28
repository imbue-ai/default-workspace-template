from collections.abc import Callable

import pytest

import imbue.remote_service_connector.app as app_mod
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import InMemoryEntitlementsStore
from imbue.remote_service_connector.testing import InMemoryGrantStore
from imbue.remote_service_connector.testing import InMemoryKeyStore
from imbue.remote_service_connector.testing import InMemoryLeaseStore
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import make_fake_entitlements_store
from imbue.remote_service_connector.testing import make_fake_grant_store
from imbue.remote_service_connector.testing import make_fake_key_store
from imbue.remote_service_connector.testing import make_fake_lease_store
from imbue.remote_service_connector.testing import make_fake_pool_backend


def _sweep_fixtures() -> tuple[FakeCloudflareOps, InMemoryKeyStore, InMemoryEntitlementsStore]:
    return FakeCloudflareOps(), make_fake_key_store(), make_fake_entitlements_store()


def _run_sweep(
    ops: FakeCloudflareOps,
    store: InMemoryKeyStore,
    entitlements_store: InMemoryEntitlementsStore,
    grant_store: InMemoryGrantStore | None = None,
    email_getter: Callable[[str], str | None] = lambda uid: None,
    only_user_id: str | None = None,
) -> dict[str, int]:
    """Call run_r2_quota_sweep with test defaults (fresh grant and lease stores)."""
    return app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        grant_store if grant_store is not None else make_fake_grant_store(),
        email_getter=email_getter,
        lease_store=make_fake_lease_store(),
        only_user_id=only_user_id,
    )


def _add_bucket_with_key(
    ops: FakeCloudflareOps,
    store: InMemoryKeyStore,
    owner_user_id: str,
    bucket_name: str,
    access: str = "readwrite",
    alias: str = "default",
) -> str:
    ops.buckets.setdefault(bucket_name, {"name": bucket_name})
    token = ops.create_bucket_token(bucket_name, access, f"mngr-r2:{bucket_name}:{alias}")
    store.add_key(str(token["id"]), owner_user_id, bucket_name, access, alias)
    return str(token["id"])


def _seed_sweep_row(
    entitlements_store: InMemoryEntitlementsStore, user_id: str, prefix: str, max_total_bucket_bytes: int
) -> None:
    _seed_entitlements_row(
        entitlements_store,
        user_id=user_id,
        user_id_prefix=prefix,
        max_total_bucket_bytes=max_total_bucket_bytes,
    )


def test_sweep_enforces_single_key_per_bucket() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    first = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    second = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", alias="extra")
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["extra_keys_revoked"] == 1
    remaining = store.list_keys("user-1", "u1prefix--data")
    # The newest key survives; the older one is revoked and dropped.
    assert [r["access_key_id"] for r in remaining] == [second]
    assert first not in ops.account_tokens


def test_sweep_keeps_extra_key_row_when_revoke_fails() -> None:
    """A failed Cloudflare revoke keeps the r2_keys row so the next sweep retries.

    Dropping the row of a still-live token would orphan a credential no later
    sweep could revoke (or downgrade for storage-quota enforcement).
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    first = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    second = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", alias="extra")
    ops.fail_next_delete_bucket_token = True
    failed = _run_sweep(ops, store, entitlements_store)
    assert failed["extra_keys_revoked"] == 0
    assert failed["key_update_failures"] == 1
    # Both the row and the live token survive the failed revoke.
    assert {r["access_key_id"] for r in store.list_keys("user-1", "u1prefix--data")} == {first, second}
    assert first in ops.account_tokens
    # The next (healthy) sweep completes the revoke.
    retried = _run_sweep(ops, store, entitlements_store)
    assert retried["extra_keys_revoked"] == 1
    assert [r["access_key_id"] for r in store.list_keys("user-1", "u1prefix--data")] == [second]
    assert first not in ops.account_tokens


def test_sweep_downgrades_and_restores_keys_around_quota() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000

    over = _run_sweep(ops, store, entitlements_store)
    assert over["users_over_quota"] == 1
    assert over["keys_downgraded"] == 1
    assert ops.account_tokens[key_id]["access"] == "read"
    downgraded_row = store.get_key(key_id)
    assert downgraded_row is not None
    assert downgraded_row["enforced_access"] == "read"

    # Repeated over-quota sweeps are no-ops (already downgraded).
    again = _run_sweep(ops, store, entitlements_store)
    assert again["keys_downgraded"] == 0

    # Back under quota: the key's intended access is restored.
    ops.usage_bytes_by_bucket["u1prefix--data"] = 50
    restored = _run_sweep(ops, store, entitlements_store)
    assert restored["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"
    restored_row = store.get_key(key_id)
    assert restored_row is not None
    assert restored_row["enforced_access"] is None


def test_sweep_never_downgrades_intentionally_read_only_keys() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data", access="read")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["keys_downgraded"] == 0
    untouched_row = store.get_key(key_id)
    assert untouched_row is not None
    assert untouched_row["enforced_access"] is None


def test_sweep_sums_usage_across_all_owner_buckets() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 150)
    key_a = _add_bucket_with_key(ops, store, "user-1", "u1prefix--a")
    key_b = _add_bucket_with_key(ops, store, "user-1", "u1prefix--b")
    ops.usage_bytes_by_bucket["u1prefix--a"] = 100
    ops.usage_bytes_by_bucket["u1prefix--b"] = 100
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["users_over_quota"] == 1
    assert counters["keys_downgraded"] == 2
    assert ops.account_tokens[key_a]["access"] == "read"
    assert ops.account_tokens[key_b]["access"] == "read"


def test_sweep_skips_unknown_owner_without_downgrading() -> None:
    """No entitlements row + no resolvable email means skip, never guess a limit."""
    ops, store, entitlements_store = _sweep_fixtures()
    key_id = _add_bucket_with_key(ops, store, "user-unknown", "uxprefix--data")
    ops.usage_bytes_by_bucket["uxprefix--data"] = 10**15
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["users_skipped"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_lazily_creates_row_for_resolvable_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """An owner with no row gets one created from their email (unpaid -> free here)."""
    monkeypatch.setenv("MINDS_PAID_LIST_CACHE_TTL_SECONDS", "0")
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    # The real time_joined getter degrades to 0 (pre-cutoff) when SuperTokens
    # is unavailable, which is exactly the branch this test wants.
    ops, store, entitlements_store = _sweep_fixtures()
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 10
    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        make_fake_grant_store(),
        email_getter=lambda uid: "nobody@gmail.com",
        lease_store=make_fake_lease_store(),
    )
    assert counters["users_skipped"] == 0
    row = entitlements_store.get_entitlements("user-1")
    assert row is not None
    assert row["plan_name"] == "free"
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_confirms_downgrade_against_live_usage() -> None:
    """A stale analytics window peak alone never downgrades: live REST usage is re-checked first.

    This is the anti-flap guarantee: a user who just pruned under quota (peak
    still over, live under) must not have their restored keys re-broken.
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 50
    ops.graphql_usage_bytes_by_bucket = {"u1prefix--data": 1000}
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["downgrades_cancelled_by_live_usage"] == 1
    assert counters["keys_downgraded"] == 0
    assert counters["users_over_quota"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_restores_downgraded_key_when_live_usage_dropped() -> None:
    """A downgraded key is restored as soon as live usage is under quota, even while the peak lags."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    over = _run_sweep(ops, store, entitlements_store)
    assert over["keys_downgraded"] == 1
    # The user cleans up: live usage drops but the window peak still shows the old high-water mark.
    ops.usage_bytes_by_bucket["u1prefix--data"] = 40
    ops.graphql_usage_bytes_by_bucket = {"u1prefix--data": 1000}
    restored = _run_sweep(ops, store, entitlements_store)
    assert restored["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_fails_open_when_live_usage_read_fails() -> None:
    """A failed REST confirmation skips the owner (no downgrade), never enforces on the peak alone."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    ops.fail_bucket_usage_reads = True
    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["live_usage_read_failures"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_skips_owner_with_active_grant() -> None:
    """An owner mid-cleanup (active grant) is left alone even when measurably over quota."""
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    counters = _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    assert counters["users_skipped_for_grant"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


class _GrantCreatingLeaseStore(InMemoryLeaseStore):
    """Lease store that creates a cleanup grant as part of granting the lease.

    Simulates a grant request winning the lease first: by the time the
    sweep's acquire succeeds, the grant exists (a real grant request holds
    the lease while it creates the grant and restores the keys).
    """

    def __init__(self, grant_store: InMemoryGrantStore, user_id_prefix: str) -> None:
        super().__init__()
        self._grant_store = grant_store
        self._user_id_prefix = user_id_prefix

    def try_acquire(self, owner_user_id: str, claim_id: str, duration_seconds: float) -> bool:
        self._grant_store.create_grant(owner_user_id, self._user_id_prefix, 1000, 60)
        return super().try_acquire(owner_user_id, claim_id, duration_seconds)


def test_sweep_skips_downgrade_when_grant_appears_before_lease_acquisition() -> None:
    """A grant created between the loop-top check and the lease must still block the downgrade."""
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000

    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        grant_store,
        email_getter=lambda uid: None,
        lease_store=_GrantCreatingLeaseStore(grant_store, "u1prefix"),
    )
    assert counters["users_skipped_for_grant"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_settles_expired_grants() -> None:
    """A grant whose expiry passed is settled from live usage; decreased usage marks it successful."""
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    grant = grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    ops.usage_bytes_by_bucket["u1prefix--data"] = 400
    grant_store.now_minutes = 61
    counters = _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    assert counters["grants_settled"] == 1
    settled = grant_store.grants_by_id[int(grant["grant_id"])]
    assert settled["settled_bytes"] == 400
    assert settled["is_decreased"] is True
    # Once settled, the owner is enforced normally again (400 > 100 -> downgraded).
    assert counters["keys_downgraded"] == 1


def test_sweep_settles_expired_grant_as_failed_when_usage_did_not_drop() -> None:
    ops, store, entitlements_store = _sweep_fixtures()
    grant_store = make_fake_grant_store()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    grant = grant_store.create_grant("user-1", "u1prefix", 1000, 60)
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    grant_store.now_minutes = 61
    _run_sweep(ops, store, entitlements_store, grant_store=grant_store)
    settled = grant_store.grants_by_id[int(grant["grant_id"])]
    assert settled["is_decreased"] is False
    assert grant_store.count_failed_grants_in_window("user-1", 24) == 1


def test_sweep_scoped_to_one_user_leaves_others_untouched() -> None:
    """The email-scoped admin sweep only enforces (and revokes extras) for the named owner."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    _seed_sweep_row(entitlements_store, "user-2", "u2prefix", 100)
    key_one = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    key_two = _add_bucket_with_key(ops, store, "user-2", "u2prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    ops.usage_bytes_by_bucket["u2prefix--data"] = 1000
    counters = _run_sweep(ops, store, entitlements_store, only_user_id="user-1")
    assert counters["keys_downgraded"] == 1
    assert ops.account_tokens[key_one]["access"] == "read"
    assert ops.account_tokens[key_two]["access"] == "readwrite"


def test_sweep_skips_suspended_owners_entirely() -> None:
    """Suspension owns a suspended account's keys: the sweep neither downgrades nor restores them."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    entitlements_store.update_entitlements(
        "user-1", {"suspended_at": "2026-08-22T00:00:00+00:00", "suspended_reason": "abuse"}
    )
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 10_000

    counters = _run_sweep(ops, store, entitlements_store)

    assert counters["users_skipped_suspended"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_never_touches_a_key_under_suspension_enforcement() -> None:
    """The per-key guard: a suspension-marked key is left alone even mid-race."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    # Model the suspend fan-out having flipped the key while the row itself
    # reads unsuspended (state changed mid-pass).
    ops.update_bucket_token_access(key_id, "u1prefix--data", "read", "mngr-r2:u1prefix--data:default")
    store.set_suspension_access(key_id, "read")
    store.set_enforced_access(key_id, "read")

    counters = _run_sweep(ops, store, entitlements_store)

    # Under quota, an enforced_access key would normally be restored; the
    # suspension marker blocks it.
    assert counters["keys_restored"] == 0
    assert ops.account_tokens[key_id]["access"] == "read"


def test_sweep_failed_downgrade_leaves_pending_marker_and_next_sweep_settles_it() -> None:
    """The write-ahead marker: a downgrade whose Cloudflare call fails leaves the key recorded as in-flight.

    'pending' (rather than an untouched None) means the token's live policy
    is untrusted, so the next pass re-asserts and settles it instead of
    trusting a possibly-half-applied write.
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    ops.fail_next_update_token_access = True

    failed = _run_sweep(ops, store, entitlements_store)
    assert failed["key_update_failures"] == 1
    assert failed["keys_downgraded"] == 0
    pending_row = store.get_key(key_id)
    assert pending_row is not None
    assert pending_row["enforced_access"] == "pending"

    retried = _run_sweep(ops, store, entitlements_store)
    assert retried["keys_downgraded"] == 1
    assert ops.account_tokens[key_id]["access"] == "read"
    settled_row = store.get_key(key_id)
    assert settled_row is not None
    assert settled_row["enforced_access"] == "read"


def test_sweep_failed_restore_leaves_pending_marker_and_next_sweep_settles_it() -> None:
    """Restores are write-ahead too: an interrupted restore leaves 'pending', never its settled 'read' marker.

    A stale 'read' marker on a key whose restore actually landed would be
    trusted by a later over-quota pass as already-downgraded, leaving the
    key silently writable; 'pending' is always re-asserted instead.
    """
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    # A settled downgrade from an earlier over-quota pass; the owner is now
    # far under quota, so this pass restores.
    ops.update_bucket_token_access(key_id, "u1prefix--data", "read", "mngr-r2:u1prefix--data:default")
    store.set_enforced_access(key_id, "read")
    ops.fail_next_update_token_access = True

    failed = _run_sweep(ops, store, entitlements_store)
    assert failed["key_update_failures"] == 1
    assert failed["keys_restored"] == 0
    pending_row = store.get_key(key_id)
    assert pending_row is not None
    assert pending_row["enforced_access"] == "pending"

    retried = _run_sweep(ops, store, entitlements_store)
    assert retried["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"
    settled_row = store.get_key(key_id)
    assert settled_row is not None
    assert settled_row["enforced_access"] is None


def test_sweep_restores_and_clears_a_stale_pending_marker_when_under_quota() -> None:
    """A crash-orphaned 'pending' key is re-asserted to its intended access once the owner is under quota."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 10**12)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    # Model a crashed downgrade: the Cloudflare write landed (token read) but
    # the settling DB write never did (marker still pending).
    ops.update_bucket_token_access(key_id, "u1prefix--data", "read", "mngr-r2:u1prefix--data:default")
    store.set_enforced_access(key_id, "pending")

    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["keys_restored"] == 1
    assert ops.account_tokens[key_id]["access"] == "readwrite"
    restored_row = store.get_key(key_id)
    assert restored_row is not None
    assert restored_row["enforced_access"] is None


def test_sweep_reasserts_a_stale_pending_marker_when_over_quota() -> None:
    """A 'pending' key of an over-quota owner is re-driven to read-only and settled as 'read'."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    # Model a crashed downgrade where the Cloudflare write never landed.
    store.set_enforced_access(key_id, "pending")

    counters = _run_sweep(ops, store, entitlements_store)
    assert counters["keys_downgraded"] == 1
    assert ops.account_tokens[key_id]["access"] == "read"
    settled_row = store.get_key(key_id)
    assert settled_row is not None
    assert settled_row["enforced_access"] == "read"


class _RenewalFailingLeaseStore(InMemoryLeaseStore):
    """Lease store whose renewals always fail, modeling a takeover mid-enforcement."""

    def renew(self, owner_user_id: str, claim_id: str, duration_seconds: float) -> bool:
        del owner_user_id, claim_id, duration_seconds
        return False


def test_sweep_aborts_an_owner_whose_lease_is_taken_over() -> None:
    """A lost lease aborts that owner's pass before any Cloudflare write and is counted."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000

    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        make_fake_grant_store(),
        email_getter=lambda uid: None,
        lease_store=_RenewalFailingLeaseStore(),
    )
    assert counters["users_aborted_lease_lost"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"


def test_sweep_skips_an_owner_whose_lease_is_contended() -> None:
    """An owner whose lease another holder keeps for the whole wait window is skipped and counted."""
    ops, store, entitlements_store = _sweep_fixtures()
    _seed_sweep_row(entitlements_store, "user-1", "u1prefix", 100)
    key_id = _add_bucket_with_key(ops, store, "user-1", "u1prefix--data")
    ops.usage_bytes_by_bucket["u1prefix--data"] = 1000
    lease_store = make_fake_lease_store()
    lease_store.claim_by_owner["user-1"] = "someone-else"

    counters = app_mod.run_r2_quota_sweep(
        ops,
        store,
        entitlements_store,
        make_fake_grant_store(),
        email_getter=lambda uid: None,
        lease_store=lease_store,
        lease_wait_seconds=0.0,
    )
    assert counters["users_skipped_lease_contended"] == 1
    assert counters["keys_downgraded"] == 0
    assert ops.account_tokens[key_id]["access"] == "readwrite"
