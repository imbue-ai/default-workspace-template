import base64

import pytest
from starlette.testclient import TestClient

import imbue.remote_service_connector.app as app_mod
import imbue.remote_service_connector.sync as sync_mod
from imbue.remote_service_connector.sync import PostgresSyncStore
from imbue.remote_service_connector.sync import SyncActiveAgentConflictError
from imbue.remote_service_connector.sync import SyncRecordFormatTooNewError
from imbue.remote_service_connector.sync import SyncRevisionConflictError
from imbue.remote_service_connector.sync import SyncStoreConsistencyError
from imbue.remote_service_connector.sync import _MAX_ENCRYPTED_SECRETS_BYTES
from imbue.remote_service_connector.sync import get_sync_store
from imbue.remote_service_connector.testing import ALL_RECORD_FIELDS_SENT
from imbue.remote_service_connector.testing import FakePoolBackend
from imbue.remote_service_connector.testing import InMemoryEntitlementsStore
from imbue.remote_service_connector.testing import InMemorySyncStore
from imbue.remote_service_connector.testing import _make_quota_test_client
from imbue.remote_service_connector.testing import _make_sync_test_client
from imbue.remote_service_connector.testing import _seed_entitlements_row
from imbue.remote_service_connector.testing import _store_record
from imbue.remote_service_connector.testing import _user_headers
from imbue.remote_service_connector.testing import make_fake_pool_backend
from imbue.remote_service_connector.testing import make_fake_sync_store


def _sync_record_body(
    host_id: str = "host-aaa111",
    agent_id: str = "agent-bbb222",
    revision: int = 1,
    state: str = "active",
    encrypted_secrets: str | None = None,
) -> dict[str, object]:
    return {
        "host_id": host_id,
        "agent_id": agent_id,
        "display_name": "my workspace",
        "color": "#aabbcc",
        "provider_kind": "lima",
        "hosting_device_id": "device-123",
        "device_label": "joshs-laptop",
        "state": state,
        "restored_from_host_id": None,
        "encrypted_secrets": encrypted_secrets,
        "revision": revision,
    }


def test_put_and_list_workspace_records_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    secrets_b64 = base64.b64encode(b"opaque-encrypted-payload").decode("ascii")

    put_resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=secrets_b64),
        headers=_user_headers(),
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["revision"] == 1

    list_resp = client.get("/sync/records", headers=_user_headers())
    assert list_resp.status_code == 200
    records = list_resp.json()["records"]
    assert len(records) == 1
    assert records[0]["host_id"] == "host-aaa111"
    assert records[0]["agent_id"] == "agent-bbb222"
    assert records[0]["display_name"] == "my workspace"
    assert records[0]["encrypted_secrets"] == secrets_b64
    assert records[0]["created_at"]


def test_workspace_record_endpoints_serve_the_destroyed_at_stamp(monkeypatch: pytest.MonkeyPatch) -> None:
    # The server-side stamp must reach clients through the wire model: minds'
    # backup reaper and the destroyed-workspaces countdown age against it.
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    active = client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers())
    assert active.status_code == 200
    assert active.json()["destroyed_at"] is None

    tombstone = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(revision=2, state="destroyed"),
        headers=_user_headers(),
    )
    assert tombstone.status_code == 200
    assert tombstone.json()["destroyed_at"]

    listed = client.get("/sync/records", headers=_user_headers()).json()["records"]
    assert listed[0]["destroyed_at"] == tombstone.json()["destroyed_at"]


def test_put_workspace_record_rejects_mismatched_path_host_id(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put("/sync/records/host-other", json=_sync_record_body(), headers=_user_headers())
    assert resp.status_code == 400


def test_put_workspace_record_cas_conflict_returns_409_with_stored_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert (
        client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers()).status_code == 200
    )

    stale = client.put("/sync/records/host-aaa111", json=_sync_record_body(revision=1), headers=_user_headers())
    assert stale.status_code == 409
    assert stale.json()["detail"]["stored"]["revision"] == 1

    fresh = client.put("/sync/records/host-aaa111", json=_sync_record_body(revision=2), headers=_user_headers())
    assert fresh.status_code == 200
    assert fresh.json()["revision"] == 2


def test_second_active_record_for_same_agent_id_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert (
        client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers()).status_code == 200
    )

    conflicting = client.put(
        "/sync/records/host-ccc333",
        json=_sync_record_body(host_id="host-ccc333"),
        headers=_user_headers(),
    )
    assert conflicting.status_code == 409

    # Tombstoning the first row frees the agent_id for a restored workspace.
    tombstone = _sync_record_body(revision=2, state="destroyed")
    assert client.put("/sync/records/host-aaa111", json=tombstone, headers=_user_headers()).status_code == 200
    restored = client.put(
        "/sync/records/host-ccc333",
        json=_sync_record_body(host_id="host-ccc333"),
        headers=_user_headers(),
    )
    assert restored.status_code == 200


def test_scrub_secrets_strips_blobs_but_keeps_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    secrets_b64 = base64.b64encode(b"payload").decode("ascii")
    client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=secrets_b64),
        headers=_user_headers(),
    )

    scrub = client.post("/sync/scrub-secrets", headers=_user_headers())
    assert scrub.status_code == 200
    assert scrub.json()["scrubbed"] == 1

    records = client.get("/sync/records", headers=_user_headers()).json()["records"]
    assert records[0]["encrypted_secrets"] is None
    assert records[0]["display_name"] == "my workspace"


def test_put_workspace_record_rejects_invalid_base64_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets="not-base64!!!"),
        headers=_user_headers(),
    )
    assert resp.status_code == 400


def test_put_workspace_record_rejects_oversized_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    oversized = base64.b64encode(b"x" * (_MAX_ENCRYPTED_SECRETS_BYTES + 1)).decode("ascii")
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(encrypted_secrets=oversized),
        headers=_user_headers(),
    )
    assert resp.status_code == 400


def test_put_workspace_record_accepts_empty_provider_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    # minds' create-path seeds a record before discovery knows the provider,
    # so an empty provider_kind must be accepted (enriched by a later push).
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    body = _sync_record_body()
    body["provider_kind"] = ""

    resp = client.put("/sync/records/host-aaa111", json=body, headers=_user_headers())

    assert resp.status_code == 200
    records = client.get("/sync/records", headers=_user_headers()).json()["records"]
    assert records[0]["provider_kind"] == ""


def test_put_workspace_record_rejects_unknown_state(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.put(
        "/sync/records/host-aaa111",
        json=_sync_record_body(state="bogus"),
        headers=_user_headers(),
    )
    assert resp.status_code == 422


def test_sync_records_require_user_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    resp = client.get("/sync/records", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401


def test_sync_records_are_isolated_per_user(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, caller = _make_sync_test_client(monkeypatch)
    client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers())

    caller["user_id"] = "other-user-id"
    other_list = client.get("/sync/records", headers=_user_headers())
    assert other_list.json()["records"] == []
    assert len(store.records_by_key) == 1


def test_delete_workspace_record_removes_row(monkeypatch: pytest.MonkeyPatch) -> None:
    client, store, _caller = _make_sync_test_client(monkeypatch)
    client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers())

    resp = client.delete("/sync/records/host-aaa111", headers=_user_headers())
    assert resp.status_code == 200
    assert client.get("/sync/records", headers=_user_headers()).json()["records"] == []
    # Idempotent: deleting again still succeeds.
    assert client.delete("/sync/records/host-aaa111", headers=_user_headers()).status_code == 200
    assert len(store.records_by_key) == 0


def _make_postgres_sync_store(monkeypatch: pytest.MonkeyPatch) -> tuple[PostgresSyncStore, FakePoolBackend]:
    """Build a PostgresSyncStore whose connections hit the in-memory pool backend."""
    backend = make_fake_pool_backend()
    backend.install_on_app_module(app_mod, monkeypatch)
    return PostgresSyncStore(), backend


def test_postgres_sync_store_round_trips_a_record(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)

    written = store.put_record("user-1", _store_record(encrypted_secrets=b"\x00opaque-blob"), ALL_RECORD_FIELDS_SENT)

    assert written["revision"] == 1
    assert written["encrypted_secrets"] == base64.b64encode(b"\x00opaque-blob").decode("ascii")
    listed = store.list_records("user-1")
    assert [record["host_id"] for record in listed] == ["host-aaa111"]
    assert listed[0]["created_at"] != ""
    assert store.list_records("user-2") == []

    updated = store.put_record("user-1", _store_record(display_name="renamed", revision=2), ALL_RECORD_FIELDS_SENT)
    assert updated["display_name"] == "renamed"
    assert updated["revision"] == 2
    # The metadata-only update carried no secrets, so the blob is now gone.
    assert updated["encrypted_secrets"] is None


def test_postgres_sync_store_raises_the_stored_row_on_a_stale_push(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(), ALL_RECORD_FIELDS_SENT)

    with pytest.raises(SyncRevisionConflictError) as conflict:
        store.put_record("user-1", _store_record(display_name="stale", revision=1), ALL_RECORD_FIELDS_SENT)

    assert conflict.value.stored_record["revision"] == 1
    assert conflict.value.stored_record["display_name"] == "my-workspace"


def test_postgres_sync_store_enforces_one_active_record_per_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(), ALL_RECORD_FIELDS_SENT)

    with pytest.raises(SyncActiveAgentConflictError):
        store.put_record("user-1", _store_record(host_id="host-bbb222"), ALL_RECORD_FIELDS_SENT)

    # A tombstone for the same agent on another host is allowed by the partial index.
    tombstone = store.put_record(
        "user-1", _store_record(host_id="host-bbb222", state="destroyed"), ALL_RECORD_FIELDS_SENT
    )
    assert tombstone["state"] == "destroyed"


def test_postgres_sync_store_reports_an_insert_race_as_a_cas_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    store, backend = _make_postgres_sync_store(monkeypatch)
    backend.sync_insert_race_winner = {"user_id": "user-1", **_store_record(display_name="winner")}

    # The loser's INSERT hits the primary key after the winner commits; the
    # retry then reports the race through the regular CAS path.
    with pytest.raises(SyncRevisionConflictError) as conflict:
        store.put_record("user-1", _store_record(display_name="loser"), ALL_RECORD_FIELDS_SENT)

    assert conflict.value.stored_record["display_name"] == "winner"


def test_postgres_sync_store_surfaces_a_rowless_update_as_a_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(), ALL_RECORD_FIELDS_SENT)
    backend.sync_update_returns_no_row = True

    with pytest.raises(SyncStoreConsistencyError):
        store.put_record("user-1", _store_record(revision=2), ALL_RECORD_FIELDS_SENT)


def test_postgres_sync_store_deletes_and_scrubs(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(encrypted_secrets=b"blob"), ALL_RECORD_FIELDS_SENT)
    store.put_record("user-1", _store_record(host_id="host-bbb222", agent_id="agent-2"), ALL_RECORD_FIELDS_SENT)

    assert store.scrub_secrets("user-1") == 1
    assert all(record["encrypted_secrets"] is None for record in store.list_records("user-1"))
    # A second scrub finds nothing left to strip.
    assert store.scrub_secrets("user-1") == 0

    store.delete_record("user-1", "host-aaa111")
    assert [record["host_id"] for record in store.list_records("user-1")] == ["host-bbb222"]


def test_postgres_sync_store_bundle_crud(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    assert store.get_bundle("user-1") is None

    bundle = {
        "kdf_salt": b"salt-bytes",
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": b"wrapped-dek-bytes",
        "key_epoch": 1,
    }
    store.put_bundle("user-1", bundle)

    fetched = store.get_bundle("user-1")
    assert fetched is not None
    assert fetched["kdf_salt"] == base64.b64encode(b"salt-bytes").decode("ascii")
    assert fetched["wrapped_dek"] == base64.b64encode(b"wrapped-dek-bytes").decode("ascii")
    assert fetched["key_epoch"] == 1

    # The upsert path: a rewrapped bundle replaces the stored one in place.
    store.put_bundle("user-1", {**bundle, "wrapped_dek": b"rewrapped", "key_epoch": 2})
    refetched = store.get_bundle("user-1")
    assert refetched is not None
    assert refetched["key_epoch"] == 2

    store.delete_bundle("user-1")
    assert store.get_bundle("user-1") is None


def test_postgres_sync_store_put_bundle_if_absent_never_replaces(monkeypatch: pytest.MonkeyPatch) -> None:
    # Two clients racing to mint the account's first DEK: exactly one
    # create-only put wins, and the loser's bundle never lands.
    store, _backend = _make_postgres_sync_store(monkeypatch)
    bundle = {
        "kdf_salt": b"salt-bytes",
        "kdf_time_cost": 3,
        "kdf_memory_kib": 65536,
        "kdf_parallelism": 4,
        "wrapped_dek": b"first-tab-dek",
        "key_epoch": 1,
    }

    assert store.put_bundle_if_absent("user-1", bundle) is True
    assert store.put_bundle_if_absent("user-1", {**bundle, "wrapped_dek": b"second-tab-dek"}) is False

    fetched = store.get_bundle("user-1")
    assert fetched is not None
    assert fetched["wrapped_dek"] == base64.b64encode(b"first-tab-dek").decode("ascii")


def test_get_sync_store_returns_a_cached_postgres_store() -> None:
    assert isinstance(get_sync_store(), PostgresSyncStore)
    assert get_sync_store() is get_sync_store()


def _make_sync_quota_test_client(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, InMemorySyncStore, InMemoryEntitlementsStore]:
    client, entitlements_store, _litellm = _make_quota_test_client(monkeypatch)
    store = make_fake_sync_store()
    monkeypatch.setattr(sync_mod, "get_sync_store", lambda: store)
    return client, store, entitlements_store


def test_sync_put_active_record_refused_at_quota(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, entitlements_store = _make_sync_quota_test_client(monkeypatch)
    _seed_entitlements_row(entitlements_store, "explorer", max_active_synced_workspaces=1)
    first = client.put(
        "/sync/records/host-1", json=_sync_record_body(host_id="host-1", agent_id="agent-1"), headers=_user_headers()
    )
    assert first.status_code == 200
    second = client.put(
        "/sync/records/host-2", json=_sync_record_body(host_id="host-2", agent_id="agent-2"), headers=_user_headers()
    )
    assert second.status_code == 403
    assert second.json()["detail"]["entitlement"] == "max_active_synced_workspaces"
    # Updating the existing active record is always allowed at the cap.
    update = client.put(
        "/sync/records/host-1",
        json=_sync_record_body(host_id="host-1", agent_id="agent-1", revision=2),
        headers=_user_headers(),
    )
    assert update.status_code == 200
    # Tombstoning is always allowed, and frees quota for a new active record.
    tombstone = client.put(
        "/sync/records/host-1",
        json=_sync_record_body(host_id="host-1", agent_id="agent-1", revision=3, state="destroyed"),
        headers=_user_headers(),
    )
    assert tombstone.status_code == 200
    third = client.put(
        "/sync/records/host-2", json=_sync_record_body(host_id="host-2", agent_id="agent-2"), headers=_user_headers()
    )
    assert third.status_code == 200


def test_postgres_sync_store_stamps_destroyed_at_and_clears_on_resurrection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    active = store.put_record("user-1", _store_record(), ALL_RECORD_FIELDS_SENT)
    assert active["destroyed_at"] is None

    tombstoned = store.put_record("user-1", _store_record(state="destroyed", revision=2), ALL_RECORD_FIELDS_SENT)
    assert tombstoned["destroyed_at"] is not None

    # A further destroyed-state update keeps the original stamp.
    updated = store.put_record(
        "user-1", _store_record(state="destroyed", display_name="renamed", revision=3), ALL_RECORD_FIELDS_SENT
    )
    assert updated["destroyed_at"] == tombstoned["destroyed_at"]

    resurrected = store.put_record("user-1", _store_record(state="active", revision=4), ALL_RECORD_FIELDS_SENT)
    assert resurrected["destroyed_at"] is None


def test_put_record_preserves_fields_absent_from_the_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """An update names only the fields it sends; absent updatable fields keep their stored values."""
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record(
        "user-1", _store_record(display_name="original", encrypted_secrets=b"\x01blob"), ALL_RECORD_FIELDS_SENT
    )

    # An older client's push that never heard of display_name/encrypted_secrets:
    # they are absent from sent_fields, so the stored values must survive.
    partial_sent = ALL_RECORD_FIELDS_SENT - {"display_name", "encrypted_secrets"}
    updated = store.put_record("user-1", _store_record(display_name="", revision=2), partial_sent)

    assert updated["display_name"] == "original"
    assert updated["encrypted_secrets"] == base64.b64encode(b"\x01blob").decode("ascii")
    assert updated["revision"] == 2


def test_put_record_explicit_null_clears_a_field(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _backend = _make_postgres_sync_store(monkeypatch)
    store.put_record("user-1", _store_record(encrypted_secrets=b"\x01blob"), ALL_RECORD_FIELDS_SENT)

    updated = store.put_record("user-1", _store_record(encrypted_secrets=None, revision=2), ALL_RECORD_FIELDS_SENT)

    assert updated["encrypted_secrets"] is None


def test_put_record_rejects_a_push_below_the_stored_record_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """The record_format write-lock outranks the revision CAS."""
    store, _backend = _make_postgres_sync_store(monkeypatch)
    newer = _store_record()
    newer["record_format"] = 2
    store.put_record("user-1", newer, ALL_RECORD_FIELDS_SENT)

    stale_format = _store_record(display_name="from an old client", revision=2)
    with pytest.raises(SyncRecordFormatTooNewError) as exc_info:
        store.put_record("user-1", stale_format, ALL_RECORD_FIELDS_SENT)
    assert exc_info.value.stored_record["record_format"] == 2

    # A stale *revision* alongside the stale format still reports the format
    # refusal (terminal beats retryable).
    stale_both = _store_record(display_name="from an old client", revision=99)
    with pytest.raises(SyncRecordFormatTooNewError):
        store.put_record("user-1", stale_both, ALL_RECORD_FIELDS_SENT)


def test_put_record_endpoint_answers_409_record_format_too_new(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    newer_body = dict(_sync_record_body())
    newer_body["record_format"] = 2
    assert client.put("/sync/records/host-aaa111", json=newer_body, headers=_user_headers()).status_code == 200

    old_client_body = dict(_sync_record_body(revision=2))
    # A pre-format client sends no record_format at all (implicitly 1).
    resp = client.put("/sync/records/host-aaa111", json=old_client_body, headers=_user_headers())

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "record_format_too_new"
    assert detail["stored"]["record_format"] == 2
    assert "update the app" in detail["message"]


def test_put_record_endpoint_accepts_a_format_upgrade(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    assert (
        client.put("/sync/records/host-aaa111", json=_sync_record_body(), headers=_user_headers()).status_code == 200
    )

    upgraded = dict(_sync_record_body(revision=2))
    upgraded["record_format"] = 3
    resp = client.put("/sync/records/host-aaa111", json=upgraded, headers=_user_headers())

    assert resp.status_code == 200
    assert resp.json()["record_format"] == 3


def test_put_record_endpoint_ignores_unknown_body_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tolerant round-trip: clients may echo response fields (or newer fields) the model ignores."""
    client, _store, _caller = _make_sync_test_client(monkeypatch)
    body = dict(_sync_record_body())
    body["added_by_a_newer_client"] = "ignored"
    resp = client.put("/sync/records/host-aaa111", json=body, headers=_user_headers())
    assert resp.status_code == 200
