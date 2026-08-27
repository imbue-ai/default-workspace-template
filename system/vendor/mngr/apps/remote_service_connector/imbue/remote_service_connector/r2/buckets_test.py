import threading
from pathlib import Path

import pytest

import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.r2.buckets as r2_buckets_mod
from imbue.remote_service_connector.errors import CloudflareApiError
from imbue.remote_service_connector.r2.naming import derive_s3_secret_access_key
from imbue.remote_service_connector.testing import FakeCloudflareOps
from imbue.remote_service_connector.testing import InMemoryKeyStore
from imbue.remote_service_connector.testing import _USER_STUB_EMAIL
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID
from imbue.remote_service_connector.testing import _USER_STUB_USER_ID_PREFIX
from imbue.remote_service_connector.testing import _make_bucket_quota_test_client
from imbue.remote_service_connector.testing import _make_bucket_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_fake_pool_backend


def test_create_bucket_returns_bucket_and_default_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "my-data"}, headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"]["bucket_name"] == f"{_USER_STUB_USER_ID_PREFIX}--my-data"
    assert body["bucket"]["s3_endpoint"] == "https://test-account.r2.cloudflarestorage.com"
    assert body["key"]["access"] == "readwrite"
    assert body["key"]["bucket_name"] == f"{_USER_STUB_USER_ID_PREFIX}--my-data"
    access_key_id = body["key"]["access_key_id"]
    assert access_key_id
    # Secret is the sha256 of the fake token value, returned once.
    assert body["key"]["secret_access_key"] == derive_s3_secret_access_key(f"token-value-{access_key_id}")
    # Bucket actually created in the fake.
    assert f"{_USER_STUB_USER_ID_PREFIX}--my-data" in fake.buckets
    # Key metadata recorded; the secret/token value is NOT persisted.
    rows = store.list_keys(_USER_STUB_USER_ID, None)
    assert len(rows) == 1
    assert rows[0]["access_key_id"] == access_key_id
    assert "secret_access_key" not in rows[0]
    assert "value" not in rows[0]
    assert rows[0]["owner_user_id"] == _USER_STUB_USER_ID


def test_create_bucket_with_read_access(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "ro", "access": "read"}, headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["key"]["access"] == "read"


def test_create_bucket_invalid_access_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "x", "access": "write"}, headers=_user_headers())
    assert resp.status_code == 422


def test_create_bucket_invalid_name_returns_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets", json={"name": "!!!"}, headers=_user_headers())
    assert resp.status_code == 400


def test_create_bucket_duplicate_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "dup"}, headers=_user_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "dup"}, headers=_user_headers())
    assert resp.status_code == 409


def test_create_bucket_at_quota_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bucket creation past the account's max_buckets entitlement is refused."""
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_buckets=1)
    assert client.post("/buckets", json={"name": "first"}, headers=_user_headers()).status_code == 200
    resp = client.post("/buckets", json={"name": "one-more"}, headers=_user_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_buckets"
    assert f"{_USER_STUB_USER_ID_PREFIX}--one-more" not in fake.buckets


def test_list_buckets_returns_only_owned(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "a"}, headers=_user_headers())
    client.post("/buckets", json={"name": "b"}, headers=_user_headers())
    # A bucket owned by someone else, plus a crafted name that merely *contains*
    # the prefix -- the in-code startswith re-check must exclude it.
    fake.buckets["otheruser--secret"] = {"name": "otheruser--secret"}
    fake.buckets[f"evil-{_USER_STUB_USER_ID_PREFIX}--x"] = {"name": f"evil-{_USER_STUB_USER_ID_PREFIX}--x"}
    resp = client.get("/buckets", headers=_user_headers())
    assert resp.status_code == 200
    names = sorted(b["bucket_name"] for b in resp.json())
    assert names == [f"{_USER_STUB_USER_ID_PREFIX}--a", f"{_USER_STUB_USER_ID_PREFIX}--b"]


def test_get_bucket_info(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_user_headers())
    resp = client.get("/buckets/data", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["bucket_name"] == f"{_USER_STUB_USER_ID_PREFIX}--data"


def test_get_bucket_info_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.get("/buckets/missing", headers=_user_headers())
    assert resp.status_code == 404


def test_destroy_bucket_non_empty_returns_409(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_user_headers())
    fake.bucket_objects[f"{_USER_STUB_USER_ID_PREFIX}--data"].append("obj1")
    resp = client.delete("/buckets/data", headers=_user_headers())
    assert resp.status_code == 409
    assert f"{_USER_STUB_USER_ID_PREFIX}--data" in fake.buckets


def test_destroy_bucket_empty_cascades_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    client.post("/buckets", json={"name": "data"}, headers=_user_headers())
    # A legacy second key (pre-single-key model) must be cascaded too.
    extra = fake.create_bucket_token(
        f"{_USER_STUB_USER_ID_PREFIX}--data", "read", f"mngr-r2:{_USER_STUB_USER_ID_PREFIX}--data:extra"
    )
    store.add_key(str(extra["id"]), _USER_STUB_USER_ID, f"{_USER_STUB_USER_ID_PREFIX}--data", "read", "extra")
    assert len(store.list_keys(_USER_STUB_USER_ID, None)) == 2
    assert len(fake.account_tokens) == 2
    resp = client.delete("/buckets/data", headers=_user_headers())
    assert resp.status_code == 200
    assert f"{_USER_STUB_USER_ID_PREFIX}--data" not in fake.buckets
    assert store.list_keys(_USER_STUB_USER_ID, None) == []
    assert fake.account_tokens == {}


def test_roll_key_returns_same_access_key_id_with_fresh_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling keeps the Access Key ID (and token policies) while re-deriving the secret."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_user_headers()).json()
    original_key = created["key"]
    resp = client.post("/buckets/data/roll-key", headers=_user_headers())
    assert resp.status_code == 200
    rolled = resp.json()
    assert rolled["access_key_id"] == original_key["access_key_id"]
    assert rolled["secret_access_key"] != original_key["secret_access_key"]
    # Still exactly one recorded key for the bucket.
    assert len(store.list_keys(_USER_STUB_USER_ID, f"{_USER_STUB_USER_ID_PREFIX}--data")) == 1


def test_roll_key_reports_enforced_downgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    """A key downgraded by the storage sweep reports read access through a roll (no bypass)."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_user_headers()).json()
    store.set_enforced_access(created["key"]["access_key_id"], "read")
    resp = client.post("/buckets/data/roll-key", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "read"


def test_roll_key_mints_fresh_key_when_none_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rolling a bucket with no recorded key (e.g. after a revoke) mints one."""
    client, _fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    created = client.post("/buckets", json={"name": "data"}, headers=_user_headers()).json()
    client.delete(f"/bucket-keys/{created['key']['access_key_id']}", headers=_user_headers())
    assert store.list_keys(_USER_STUB_USER_ID, f"{_USER_STUB_USER_ID_PREFIX}--data") == []
    resp = client.post("/buckets/data/roll-key", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["access"] == "readwrite"
    assert len(store.list_keys(_USER_STUB_USER_ID, f"{_USER_STUB_USER_ID_PREFIX}--data")) == 1


def test_roll_key_for_missing_bucket_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.post("/buckets/nope/roll-key", headers=_user_headers())
    assert resp.status_code == 404


def test_destroy_key(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store = _make_bucket_test_client(monkeypatch)
    create = client.post("/buckets", json={"name": "data"}, headers=_user_headers()).json()
    access_key_id = create["key"]["access_key_id"]
    resp = client.delete(f"/bucket-keys/{access_key_id}", headers=_user_headers())
    assert resp.status_code == 200
    assert access_key_id not in fake.account_tokens
    assert store.get_key(access_key_id) is None


def test_destroy_key_unknown_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.delete("/bucket-keys/does-not-exist", headers=_user_headers())
    assert resp.status_code == 404


def test_destroy_key_not_owned_returns_404(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, store = _make_bucket_test_client(monkeypatch)
    store.add_key("akid-other", "some-other-user", "other--bucket", "readwrite", "x")
    resp = client.delete("/bucket-keys/akid-other", headers=_user_headers())
    assert resp.status_code == 404
    # The other user's row is untouched.
    assert store.get_key("akid-other") is not None


def test_create_bucket_works_for_unpaid_explorer_account(monkeypatch: pytest.MonkeyPatch) -> None:
    """The old paid gate is gone: an unpaid (explorer) account can create buckets within quota."""
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    # Install a paid-list backend where the stub user email is NOT paid.
    backend = make_fake_pool_backend()
    backend.add_paid_email(_USER_STUB_EMAIL, is_paid=False)
    backend.install_on_app_module(app_mod, monkeypatch)
    resp = client.post("/buckets", json={"name": "x"}, headers=_user_headers())
    assert resp.status_code == 200
    assert f"{_USER_STUB_USER_ID_PREFIX}--x" in fake.buckets


def test_r2_keys_migration_declares_all_persisted_columns() -> None:
    """Guard against the r2_keys schema and the PostgresKeyStore INSERT drifting apart."""
    migration_path = Path(__file__).parent.parent.parent.parent / "migrations" / "004_r2_keys.sql"
    migration_sql = migration_path.read_text()
    for column in ("access_key_id", "owner_user_id", "bucket_name", "access", "alias", "created_at"):
        assert column in migration_sql, f"r2_keys migration is missing column {column!r}"


class _FailForNamedBucketOps(FakeCloudflareOps):
    """FakeCloudflareOps whose usage read fails only for one named bucket."""

    def __init__(self, failing_bucket_name: str) -> None:
        super().__init__()
        self.failing_bucket_name = failing_bucket_name

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        if bucket_name == self.failing_bucket_name:
            raise CloudflareApiError(status_code=500, errors=[{"message": "simulated per-bucket failure"}])
        return super().get_bucket_usage_bytes(bucket_name)


def test_read_bucket_usage_bytes_concurrently_aligns_results_and_errors_positionally() -> None:
    ops = _FailForNamedBucketOps("u1prefix--broken")
    ops.usage_bytes_by_bucket["u1prefix--a"] = 111
    ops.usage_bytes_by_bucket["u1prefix--b"] = 222
    results = r2_buckets_mod.read_bucket_usage_bytes_concurrently(
        ops, ["u1prefix--a", "u1prefix--broken", "u1prefix--b"]
    )
    assert results[0] == 111
    assert isinstance(results[1], CloudflareApiError)
    assert results[2] == 222


def test_read_bucket_usage_bytes_concurrently_returns_empty_for_no_buckets() -> None:
    assert r2_buckets_mod.read_bucket_usage_bytes_concurrently(FakeCloudflareOps(), []) == []


class _BarrierUsageOps(FakeCloudflareOps):
    """FakeCloudflareOps whose usage reads block until all expected readers arrive.

    Proves the reads overlap: sequential reads would deadlock on the barrier
    (surfacing as a BrokenBarrierError after the wait timeout) instead of all
    arriving together.
    """

    def __init__(self, expected_reader_count: int) -> None:
        super().__init__()
        self.reader_barrier = threading.Barrier(expected_reader_count)

    def get_bucket_usage_bytes(self, bucket_name: str) -> int:
        self.reader_barrier.wait(timeout=10)
        return super().get_bucket_usage_bytes(bucket_name)


def test_read_bucket_usage_bytes_concurrently_overlaps_reads() -> None:
    bucket_count = r2_buckets_mod._BUCKET_USAGE_MAX_PARALLEL_READS
    ops = _BarrierUsageOps(expected_reader_count=bucket_count)
    bucket_names = [f"u1prefix--bucket{i}" for i in range(bucket_count)]
    for i, name in enumerate(bucket_names):
        ops.usage_bytes_by_bucket[name] = i + 1
    results = r2_buckets_mod.read_bucket_usage_bytes_concurrently(ops, bucket_names)
    assert results == [i + 1 for i in range(bucket_count)]


def test_measure_live_owner_usage_bytes_raises_when_any_read_fails() -> None:
    ops = _FailForNamedBucketOps("u1prefix--broken")
    ops.buckets["u1prefix--ok"] = {"name": "u1prefix--ok"}
    ops.buckets["u1prefix--broken"] = {"name": "u1prefix--broken"}
    ops.usage_bytes_by_bucket["u1prefix--ok"] = 10
    with pytest.raises(CloudflareApiError):
        r2_buckets_mod.measure_live_owner_usage_bytes(ops, "u1prefix")


def _downgrade_key(fake: FakeCloudflareOps, store: InMemoryKeyStore, access_key_id: str) -> None:
    """Put a key into the sweep's downgraded state (read-only token policy + enforced marker)."""
    fake.account_tokens[access_key_id]["access"] = "read"
    store.set_enforced_access(access_key_id, "read")


def test_create_bucket_over_storage_quota_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    assert client.post("/buckets", json={"name": "a"}, headers=_user_headers()).status_code == 200
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 1000
    resp = client.post("/buckets", json={"name": "b"}, headers=_user_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "quota_exceeded"
    assert detail["entitlement"] == "max_total_bucket_bytes"


def test_create_bucket_storage_check_fails_open_on_usage_read_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreadable usage number never blocks creation (missing data never denies)."""
    client, fake, _store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    assert client.post("/buckets", json={"name": "a"}, headers=_user_headers()).status_code == 200
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 1000
    fake.fail_bucket_usage_reads = True
    resp = client.post("/buckets", json={"name": "b"}, headers=_user_headers())
    assert resp.status_code == 200


def test_create_bucket_while_enforced_mints_read_only_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh mint must not hand a writable key to an owner the sweep already downgraded."""
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    first = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    _downgrade_key(fake, store, first["key"]["access_key_id"])
    resp = client.post("/buckets", json={"name": "b"}, headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"]["access"] == "read"
    assert fake.account_tokens[body["key"]["access_key_id"]]["access"] == "read"
    new_row = store.get_key(body["key"]["access_key_id"])
    assert new_row is not None
    # Intended access stays readwrite so the sweep restores it once under quota.
    assert new_row["access"] == "readwrite"
    assert new_row["enforced_access"] == "read"


def test_roll_key_fresh_mint_respects_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, _entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    first = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    second = client.post("/buckets", json={"name": "b"}, headers=_user_headers()).json()
    _downgrade_key(fake, store, first["key"]["access_key_id"])
    # Revoke b's key so roll-key has to mint a fresh one.
    revoke = client.delete(f"/bucket-keys/{second['key']['access_key_id']}", headers=_user_headers())
    assert revoke.status_code == 200
    rolled = client.post("/buckets/b/roll-key", headers=_user_headers())
    assert rolled.status_code == 200
    assert rolled.json()["access"] == "read"


def test_cleanup_grant_not_needed_when_nothing_downgraded(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store, _entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    assert client.post("/buckets", json={"name": "a"}, headers=_user_headers()).status_code == 200
    resp = client.post("/account/storage-cleanup-grant", headers=_user_headers())
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_needed"
    assert grant_store.grants_by_id == {}


def test_cleanup_grant_restores_keys_and_records_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 1000
    _downgrade_key(fake, store, key_id)

    resp = client.post("/account/storage-cleanup-grant", headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "granted"
    assert body["baseline_bytes"] == 1000
    # The downgraded key is writable again; its intended access is unchanged.
    assert fake.account_tokens[key_id]["access"] == "readwrite"
    restored_row = store.get_key(key_id)
    assert restored_row is not None
    assert restored_row["enforced_access"] is None
    assert len(grant_store.grants_by_id) == 1

    # Idempotent while active: no second grant row is minted.
    again = client.post("/account/storage-cleanup-grant", headers=_user_headers())
    assert again.status_code == 200
    assert again.json()["status"] == "granted"
    assert len(grant_store.grants_by_id) == 1


def test_cleanup_grant_budget_exhausted_returns_403(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    _downgrade_key(fake, store, created["key"]["access_key_id"])
    # Burn the failed-grant budget: five grants settled without any decrease.
    for _ in range(5):
        burned = grant_store.create_grant(_USER_STUB_USER_ID, _USER_STUB_USER_ID_PREFIX, 1000, 60)
        grant_store.settle_grant(int(burned["grant_id"]), 1000, False)
    resp = client.post("/account/storage-cleanup-grant", headers=_user_headers())
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["code"] == "cleanup_grant_budget_exhausted"
    assert detail["limit"] == 5
    assert detail["current"] == 5


def test_storage_recheck_settles_grant_success_and_keeps_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 1000
    _downgrade_key(fake, store, key_id)
    assert client.post("/account/storage-cleanup-grant", headers=_user_headers()).status_code == 200
    # The client prunes: usage drops under both the baseline and the limit.
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 40

    resp = client.post("/account/storage-recheck", headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["usage_bytes"] == 40
    assert body["is_over_quota"] is False
    assert body["is_grant_settled"] is True
    assert fake.account_tokens[key_id]["access"] == "readwrite"
    settled = list(grant_store.grants_by_id.values())[0]
    assert settled["is_decreased"] is True
    assert grant_store.count_failed_grants_in_window(_USER_STUB_USER_ID, 24) == 0


def test_storage_recheck_redowngrades_and_burns_budget_when_usage_did_not_drop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, fake, store, entitlements_store, grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    key_id = created["key"]["access_key_id"]
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 1000
    _downgrade_key(fake, store, key_id)
    assert client.post("/account/storage-cleanup-grant", headers=_user_headers()).status_code == 200

    resp = client.post("/account/storage-recheck", headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_over_quota"] is True
    assert body["is_grant_settled"] is True
    assert fake.account_tokens[key_id]["access"] == "read"
    assert grant_store.count_failed_grants_in_window(_USER_STUB_USER_ID, 24) == 1


def test_storage_recheck_standalone_restores_without_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    """A user who freed space any other way gets restored immediately, no grant involved."""
    client, fake, store, entitlements_store, _grant_store = _make_bucket_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, max_total_bucket_bytes=100)
    created = client.post("/buckets", json={"name": "a"}, headers=_user_headers()).json()
    key_id = created["key"]["access_key_id"]
    _downgrade_key(fake, store, key_id)
    fake.usage_bytes_by_bucket[f"{_USER_STUB_USER_ID_PREFIX}--a"] = 40

    resp = client.post("/account/storage-recheck", headers=_user_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_grant_settled"] is False
    assert body["is_over_quota"] is False
    assert fake.account_tokens[key_id]["access"] == "readwrite"


def test_create_bucket_rejects_reserved_host_prefix_without_record(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)

    resp = client.post("/buckets", json={"name": "host-abc123"}, headers=_user_headers())

    assert resp.status_code == 403
    assert "reserved" in resp.json()["detail"]


def test_create_bucket_rejects_reserved_host_prefix_in_slugified_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bucket is created under the slugified short name, so a raw name that
    # only slugifies into the reserved `host-` shape must be refused too.
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)

    resp = client.post("/buckets", json={"name": "HOST-abc123"}, headers=_user_headers())

    assert resp.status_code == 403
    assert "reserved" in resp.json()["detail"]


def test_create_bucket_allows_host_prefix_with_workspace_record(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    backend.sync_record_rows.append(
        {"user_id": _USER_STUB_USER_ID, "host_id": "host-abc123", "agent_id": "agent-1", "state": "active"}
    )

    resp = client.post("/buckets", json={"name": "host-abc123"}, headers=_user_headers())

    assert resp.status_code == 200
    assert resp.json()["bucket"]["bucket_name"] == f"{_USER_STUB_USER_ID_PREFIX}--host-abc123"


def test_delete_bucket_refuses_while_workspace_record_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    backend.sync_record_rows.append(
        {"user_id": _USER_STUB_USER_ID, "host_id": "host-abc123", "agent_id": "agent-1", "state": "active"}
    )
    fake.create_bucket(f"{_USER_STUB_USER_ID_PREFIX}--host-abc123")

    resp = client.delete("/buckets/host-abc123", headers=_user_headers())

    assert resp.status_code == 409
    assert "still active" in resp.json()["detail"]

    # Tombstoning the record unlocks the destroy.
    backend.sync_record_rows[0]["state"] = "destroyed"
    resp_after = client.delete("/buckets/host-abc123", headers=_user_headers())
    assert resp_after.status_code == 200


def test_delete_bucket_interlock_applies_to_slugified_name(monkeypatch: pytest.MonkeyPatch) -> None:
    # The bucket is deleted under the slugified short name, so a case variant
    # of the path parameter must hit the same ACTIVE-record interlock.
    client, fake, _store = _make_bucket_test_client(monkeypatch)
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    backend.sync_record_rows.append(
        {"user_id": _USER_STUB_USER_ID, "host_id": "host-abc123", "agent_id": "agent-1", "state": "active"}
    )
    fake.create_bucket(f"{_USER_STUB_USER_ID_PREFIX}--host-abc123")

    resp = client.delete("/buckets/HOST-abc123", headers=_user_headers())

    assert resp.status_code == 409
    assert "still active" in resp.json()["detail"]
