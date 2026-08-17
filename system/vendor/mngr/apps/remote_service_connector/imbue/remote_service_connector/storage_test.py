import boto3
import pytest
from botocore.stub import Stubber

from imbue.remote_service_connector.errors import MissingStorageConfigError
from imbue.remote_service_connector.errors import StorageDeletionError
from imbue.remote_service_connector.storage import _delete_prefix_with_client
from imbue.remote_service_connector.storage import is_storage_configured
from imbue.remote_service_connector.storage import read_storage_config
from imbue.remote_service_connector.storage import unwrap_dek
from imbue.remote_service_connector.storage import wrap_dek
from imbue.remote_service_connector.testing import make_storage_config


def _make_stubbed_s3_client() -> tuple[object, Stubber]:
    client = boto3.client(
        "s3",
        region_name="us-east-va",
        aws_access_key_id="fake-access-key",
        aws_secret_access_key="fake-secret-key",
    )
    return client, Stubber(client)


def test_wrap_and_unwrap_dek_round_trips() -> None:
    config = make_storage_config()
    identity = "AGE-SECRET-KEY-1ROUNDTRIP"

    wrapped = wrap_dek(config, identity)

    assert wrapped != identity
    assert unwrap_dek(config, wrapped) == identity
    # Wrapping is nonce-randomized: two wraps of the same identity differ.
    assert wrap_dek(config, identity) != wrapped


def test_read_storage_config_raises_naming_the_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_ENDPOINT", "https://s3.example")
    monkeypatch.delenv("WORKSPACE_STORAGE_S3_REGION", raising=False)

    with pytest.raises(MissingStorageConfigError) as excinfo:
        read_storage_config()

    assert "WORKSPACE_STORAGE_S3_REGION" in str(excinfo.value)


def test_read_storage_config_reads_all_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_REGION", "us-east-va")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_SECRET_KEY", "sk")
    monkeypatch.setenv("WORKSPACE_STORAGE_BUCKET", "mngr-workspaces-test")
    monkeypatch.setenv("WORKSPACE_STORAGE_KEK", "a2Vr")
    monkeypatch.setenv("WORKSPACE_STOP_RETENTION_SECONDS", "120")

    config = read_storage_config()

    assert config.bucket == "mngr-workspaces-test"
    assert config.retention_seconds == 120
    assert is_storage_configured()


def test_read_storage_config_defaults_retention_when_left_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Vault template ships the optional retention line as an empty export;
    an empty value must fall back to the default, not crash on int('')."""
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_ENDPOINT", "https://s3.example")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_REGION", "us-east-va")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_ACCESS_KEY", "ak")
    monkeypatch.setenv("WORKSPACE_STORAGE_S3_SECRET_KEY", "sk")
    monkeypatch.setenv("WORKSPACE_STORAGE_BUCKET", "mngr-workspaces-test")
    monkeypatch.setenv("WORKSPACE_STORAGE_KEK", "a2Vr")
    monkeypatch.setenv("WORKSPACE_STOP_RETENTION_SECONDS", "")

    config = read_storage_config()

    assert config.retention_seconds == 3600


def test_is_storage_configured_false_without_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WORKSPACE_STORAGE_BUCKET", raising=False)
    assert not is_storage_configured()


def test_delete_prefix_deletes_every_listed_object_across_pages() -> None:
    client, stubber = _make_stubbed_s3_client()
    stubber.add_response(
        "list_objects_v2",
        {
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
            "Contents": [{"Key": "host-1/gen-1/disk.zst.age"}, {"Key": "host-1/gen-1/meta.tar.zst.age"}],
        },
        {"Bucket": "mngr-workspaces-test", "Prefix": "host-1/"},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {
                "Objects": [{"Key": "host-1/gen-1/disk.zst.age"}, {"Key": "host-1/gen-1/meta.tar.zst.age"}],
                "Quiet": True,
            },
        },
    )
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": "host-1/gen-2/datadisk.zst.age"}]},
        {"Bucket": "mngr-workspaces-test", "Prefix": "host-1/", "ContinuationToken": "token-1"},
    )
    stubber.add_response(
        "delete_objects",
        {},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {"Objects": [{"Key": "host-1/gen-2/datadisk.zst.age"}], "Quiet": True},
        },
    )

    with stubber:
        deleted_count = _delete_prefix_with_client(client, "mngr-workspaces-test", "host-1/")

    assert deleted_count == 3
    stubber.assert_no_pending_responses()


def test_delete_prefix_raises_on_per_key_delete_failures() -> None:
    # Quiet-mode delete_objects reports per-key failures in the response's
    # ``Errors`` list without raising, so the helper must surface them itself:
    # a partially-deleted prefix has to fail the caller, not orphan objects.
    client, stubber = _make_stubbed_s3_client()
    stubber.add_response(
        "list_objects_v2",
        {"IsTruncated": False, "Contents": [{"Key": "host-1/gen-1/disk.zst.age"}]},
        {"Bucket": "mngr-workspaces-test", "Prefix": "host-1/"},
    )
    stubber.add_response(
        "delete_objects",
        {"Errors": [{"Key": "host-1/gen-1/disk.zst.age", "Code": "AccessDenied", "Message": "denied"}]},
        {
            "Bucket": "mngr-workspaces-test",
            "Delete": {"Objects": [{"Key": "host-1/gen-1/disk.zst.age"}], "Quiet": True},
        },
    )

    with stubber:
        with pytest.raises(StorageDeletionError, match="host-1/gen-1/disk.zst.age"):
            _delete_prefix_with_client(client, "mngr-workspaces-test", "host-1/")


def test_delete_prefix_wraps_s3_errors_in_storage_deletion_error() -> None:
    client, stubber = _make_stubbed_s3_client()
    stubber.add_client_error("list_objects_v2", service_error_code="AccessDenied", http_status_code=403)

    with stubber:
        with pytest.raises(StorageDeletionError, match="mngr-workspaces-test/host-1/"):
            _delete_prefix_with_client(client, "mngr-workspaces-test", "host-1/")
