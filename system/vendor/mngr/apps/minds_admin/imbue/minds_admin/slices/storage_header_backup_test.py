import pytest
from botocore.exceptions import ClientError

from imbue.minds_admin.slices.storage_header_backup import upload_storage_header_backup
from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError


class _RecordingS3Client:
    """Records put_object calls; fails them when told to (the boto3 surface the uploader touches)."""

    def __init__(self, failure: ClientError | None = None) -> None:
        self.failure = failure
        self.puts: list[tuple[str, str, bytes]] = []

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> None:
        if self.failure is not None:
            raise self.failure
        self.puts.append((Bucket, Key, Body))


def test_upload_puts_the_header_bytes_at_the_object_key() -> None:
    client = _RecordingS3Client()
    upload_storage_header_backup(client, "mngr-workspaces-dev", "dev-a/boxes/ns1/luks-header-u.img", b"LUKS")
    assert client.puts == [("mngr-workspaces-dev", "dev-a/boxes/ns1/luks-header-u.img", b"LUKS")]


def test_upload_refuses_an_empty_header_without_touching_the_bucket() -> None:
    client = _RecordingS3Client()
    with pytest.raises(BareMetalProvisioningError, match="empty LUKS header backup"):
        upload_storage_header_backup(client, "bucket", "key", b"")
    assert client.puts == []


def test_upload_wraps_an_s3_failure_so_the_prep_reports_it() -> None:
    failure = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "PutObject")
    with pytest.raises(BareMetalProvisioningError, match="AccessDenied"):
        upload_storage_header_backup(_RecordingS3Client(failure), "bucket", "key", b"LUKS")
