import hashlib

import pytest

from imbue.remote_service_connector.errors import InvalidR2BucketNameError
from imbue.remote_service_connector.errors import R2BucketOwnershipError
from imbue.remote_service_connector.r2.naming import derive_s3_secret_access_key
from imbue.remote_service_connector.r2.naming import make_bucket_name
from imbue.remote_service_connector.r2.naming import parse_workspace_backup_bucket_name
from imbue.remote_service_connector.r2.naming import slugify_r2_name
from imbue.remote_service_connector.r2.naming import verify_bucket_ownership


def test_make_bucket_name_slugifies() -> None:
    assert make_bucket_name("user", "My Cool Data") == "user--my-cool-data"


def test_make_bucket_name_collapses_separators() -> None:
    assert make_bucket_name("user", "foo__bar--baz") == "user--foo-bar-baz"


def test_make_bucket_name_rejects_invalid() -> None:
    with pytest.raises(InvalidR2BucketNameError):
        make_bucket_name("user", "!!!")


def test_slugify_r2_name_strips_edges() -> None:
    assert slugify_r2_name("  --Foo--  ") == "foo"


def test_verify_bucket_ownership_rejects_foreign_prefix() -> None:
    with pytest.raises(R2BucketOwnershipError):
        verify_bucket_ownership("evil-user--x", "user")


def test_verify_bucket_ownership_accepts_owned() -> None:
    verify_bucket_ownership("user--x", "user")


def test_derive_s3_secret_matches_sha256() -> None:
    assert derive_s3_secret_access_key("hello") == hashlib.sha256(b"hello").hexdigest()


def test_parse_workspace_backup_bucket_name_splits_prefix_and_host_id() -> None:
    assert parse_workspace_backup_bucket_name("abc123--host-9c35c0cf") == ("abc123", "host-9c35c0cf")


@pytest.mark.parametrize(
    "bad_name",
    ["no-separator", "abc123--my-data", "abc123--host-XYZ", "--host-abc", "abc123--host-"],
)
def test_parse_workspace_backup_bucket_name_rejects_non_backup_names(bad_name: str) -> None:
    assert parse_workspace_backup_bucket_name(bad_name) is None
