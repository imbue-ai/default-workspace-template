from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

import pytest

from imbue.remote_service_connector.app import run_backup_retention_reap
from imbue.remote_service_connector.auth import derive_user_id_prefix
from imbue.remote_service_connector.r2.naming import DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS
from imbue.remote_service_connector.testing import ALL_RECORD_FIELDS_SENT
from imbue.remote_service_connector.testing import _make_bucket_test_client
from imbue.remote_service_connector.testing import _store_record
from imbue.remote_service_connector.testing import make_fake_cloudflare_ctx
from imbue.remote_service_connector.testing import make_fake_key_store
from imbue.remote_service_connector.testing import make_fake_orphan_bucket_store
from imbue.remote_service_connector.testing import make_fake_sync_store


def _make_reap_deps() -> tuple[Any, Any, Any, Any]:
    """(ops, sync_store, key_store, orphan_store) fakes for direct reaper runs."""
    fake_ctx = make_fake_cloudflare_ctx()
    return fake_ctx.fake, make_fake_sync_store(), make_fake_key_store(), make_fake_orphan_bucket_store()


def _seed_destroyed_record_with_bucket(ops: Any, sync_store: Any, user_id: str, host_id: str) -> str:
    """Tombstone a record for (user_id, host_id) and create its (non-empty) backup bucket."""
    sync_store.put_record(
        user_id, _store_record(host_id=host_id, agent_id=f"agent-{host_id}", state="destroyed"), ALL_RECORD_FIELDS_SENT
    )
    bucket_name = f"{derive_user_id_prefix(user_id)}--{host_id}"
    ops.create_bucket(bucket_name)
    ops.bucket_objects[bucket_name] = ["data/a", "data/b", "config"]
    return bucket_name


def test_backup_retention_reap_deletes_bucket_then_record() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    bucket_name = _seed_destroyed_record_with_bucket(ops, sync_store, "user-1", "host-aaa111")

    counters = run_backup_retention_reap(ops, sync_store, key_store, orphan_store, window_seconds=0.0)

    assert counters["records_reaped"] == 1
    assert counters["buckets_deleted"] == 1
    assert counters["objects_deleted"] == 3
    assert bucket_name not in ops.buckets
    assert sync_store.list_records("user-1") == []


def test_backup_retention_reap_leaves_records_inside_the_window() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    bucket_name = _seed_destroyed_record_with_bucket(ops, sync_store, "user-1", "host-aaa111")

    counters = run_backup_retention_reap(ops, sync_store, key_store, orphan_store)

    assert counters["records_reaped"] == 0
    assert bucket_name in ops.buckets
    assert len(sync_store.list_records("user-1")) == 1


def test_backup_retention_reap_reaps_record_without_bucket() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    sync_store.put_record("user-1", _store_record(host_id="host-aaa111", state="destroyed"), ALL_RECORD_FIELDS_SENT)

    counters = run_backup_retention_reap(ops, sync_store, key_store, orphan_store, window_seconds=0.0)

    assert counters["records_reaped"] == 1
    assert counters["buckets_deleted"] == 0
    assert sync_store.list_records("user-1") == []


def test_backup_retention_reap_never_touches_non_backup_named_buckets() -> None:
    # A destroyed record whose host_id is not in the reserved `host-<hex>`
    # shape must be reaped without bucket work: its name could collide with a
    # generic user bucket, which is never the reaper's to delete.
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    sync_store.put_record(
        "user-1", _store_record(host_id="my-data", agent_id="agent-x", state="destroyed"), ALL_RECORD_FIELDS_SENT
    )
    generic_bucket = f"{derive_user_id_prefix('user-1')}--my-data"
    ops.create_bucket(generic_bucket)
    ops.bucket_objects[generic_bucket] = ["keep-me"]

    counters = run_backup_retention_reap(ops, sync_store, key_store, orphan_store, window_seconds=0.0)

    assert counters["records_reaped"] == 1
    assert counters["buckets_deleted"] == 0
    assert generic_bucket in ops.buckets
    assert sync_store.list_records("user-1") == []


def test_backup_retention_reap_stamps_orphans_then_reaps_after_the_window() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    orphan_bucket = "someuser12345678--host-bbb222"
    ops.create_bucket(orphan_bucket)
    ops.bucket_objects[orphan_bucket] = ["x"]

    # First sighting stamps the orphan clock but deletes nothing.
    first = run_backup_retention_reap(ops, sync_store, key_store, orphan_store)
    assert first["orphan_buckets_reaped"] == 0
    assert orphan_bucket in ops.buckets
    assert orphan_store.get_first_seen(orphan_bucket) is not None

    # Backdate the stamp past the window: the next pass reaps the bucket.
    orphan_store.stamps_by_bucket[orphan_bucket] = datetime.now(timezone.utc) - timedelta(days=31)
    second = run_backup_retention_reap(ops, sync_store, key_store, orphan_store)
    assert second["orphan_buckets_reaped"] == 1
    assert orphan_bucket not in ops.buckets
    assert orphan_store.get_first_seen(orphan_bucket) is None


def test_backup_retention_reap_clears_stamps_for_referenced_buckets() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    bucket_name = f"{derive_user_id_prefix('user-1')}--host-ccc333"
    ops.create_bucket(bucket_name)
    sync_store.put_record("user-1", _store_record(host_id="host-ccc333", state="active"), ALL_RECORD_FIELDS_SENT)
    orphan_store.stamps_by_bucket[bucket_name] = datetime.now(timezone.utc) - timedelta(days=40)

    counters = run_backup_retention_reap(ops, sync_store, key_store, orphan_store)

    # The record protects the bucket even with an (obsolete) orphan stamp.
    assert counters["orphan_buckets_reaped"] == 0
    assert bucket_name in ops.buckets
    assert orphan_store.get_first_seen(bucket_name) is None


def test_backup_retention_reap_dry_run_reports_without_deleting() -> None:
    ops, sync_store, key_store, orphan_store = _make_reap_deps()
    bucket_name = _seed_destroyed_record_with_bucket(ops, sync_store, "user-1", "host-aaa111")
    orphan_bucket = "someuser12345678--host-bbb222"
    ops.create_bucket(orphan_bucket)

    result = run_backup_retention_reap(ops, sync_store, key_store, orphan_store, window_seconds=0.0, dry_run=True)

    assert result["dry_run"] is True
    kinds = sorted(candidate["kind"] for candidate in result["candidates"])
    assert kinds == ["orphan", "record"]
    assert bucket_name in ops.buckets
    assert orphan_bucket in ops.buckets
    assert len(sync_store.list_records("user-1")) == 1
    # Dry-run must not write orphan stamps either.
    assert orphan_store.get_first_seen(orphan_bucket) is None


def test_destroyed_workspace_backup_policy_endpoint_is_public(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _fake, _store = _make_bucket_test_client(monkeypatch)
    resp = client.get("/policies/destroyed-workspace-backups")
    assert resp.status_code == 200
    assert resp.json() == {"retention_seconds": DESTROYED_WORKSPACE_BACKUP_RETENTION_SECONDS}
