from typing import Any

import boto3
import pytest


@pytest.fixture
def stub_s3_client() -> Any:
    """An S3 client a `Stubber` can drive, standing in for the feed bucket.

    Every credential here is a placeholder: a stubbed client is never signed and
    never leaves the process.
    """
    return boto3.client(
        "s3",
        endpoint_url="https://account.r2.cloudflarestorage.com",
        aws_access_key_id="key",
        aws_secret_access_key="secret",
        region_name="auto",
    )
