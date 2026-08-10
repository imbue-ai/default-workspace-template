import os
from typing import Any

import boto3
from botocore.exceptions import ClientError
from pydantic import ConfigDict
from pydantic import Field

from imbue.apt_mirror.errors import AptMirrorNotConfiguredError
from imbue.apt_mirror.interfaces import AptMirrorStorageInterface

R2_ENDPOINT_ENV = "APT_MIRROR_R2_ENDPOINT"
R2_BUCKET_ENV = "APT_MIRROR_R2_BUCKET"
R2_ACCESS_KEY_ID_ENV = "APT_MIRROR_R2_ACCESS_KEY_ID"
R2_SECRET_ACCESS_KEY_ENV = "APT_MIRROR_R2_SECRET_ACCESS_KEY"


class R2AptMirrorStorage(AptMirrorStorageInterface):
    """R2-backed storage via the S3 API. boto3 clients are thread-safe, so one instance serves the warm pool."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: Any = Field(frozen=True, description="boto3 S3 client configured for the R2 endpoint")
    bucket: str = Field(frozen=True, description="R2 bucket name")

    def get_object(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        return response["Body"].read()

    def put_object(self, key: str, data: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data)

    def has_object(self, key: str) -> bool:
        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return False
            raise
        return True


def build_r2_storage_from_env() -> R2AptMirrorStorage:
    """Build R2 storage from the APT_MIRROR_R2_* env vars. Raises AptMirrorNotConfiguredError when any is unset."""
    endpoint = os.environ.get(R2_ENDPOINT_ENV, "")
    bucket = os.environ.get(R2_BUCKET_ENV, "")
    access_key_id = os.environ.get(R2_ACCESS_KEY_ID_ENV, "")
    secret_access_key = os.environ.get(R2_SECRET_ACCESS_KEY_ENV, "")
    for env_var, value in (
        (R2_ENDPOINT_ENV, endpoint),
        (R2_BUCKET_ENV, bucket),
        (R2_ACCESS_KEY_ID_ENV, access_key_id),
        (R2_SECRET_ACCESS_KEY_ENV, secret_access_key),
    ):
        if not value:
            raise AptMirrorNotConfiguredError(env_var)
    s3_client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
    )
    return R2AptMirrorStorage(client=s3_client, bucket=bucket)
