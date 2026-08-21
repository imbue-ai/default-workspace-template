"""Credentials for an R2 account, and a boto3 client pointed at it.

Everything published to R2 goes through the S3-compatible API rather than
Cloudflare's REST object API, and every publisher reaches it the same way: the
same three environment variables, minted by ``setup_tier.py``, and the same
per-account endpoint. That is what lives here; what each publisher puts in its
bucket lives with that publisher.

Cloudflare's REST object API is deliberately not an option. It is governed by
the global api.cloudflare.com rate limit of 1200 requests per 5 minutes, which a
single pre-baked Lima image (~65,000 chunks) cannot fit inside even uploading
flat out. The S3 API has no such limit. If you only hold an account API token,
derive S3 credentials from it rather than reaching for the REST API -- for an
account-owned R2 token, the access key id is the token's id and the secret is
the SHA-256 of the token value.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from typing import Final

import boto3
from botocore.client import Config

R2_ENV_VARS: Final[tuple[str, ...]] = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")


class R2CredentialsError(Exception):
    """A credential the S3 API needs did not arrive."""


@dataclass(frozen=True)
class R2Credentials:
    """The three values the S3 API needs to reach one R2 account."""

    account_id: str
    access_key_id: str
    secret_access_key: str

    @property
    def endpoint_url(self) -> str:
        return f"https://{self.account_id}.r2.cloudflarestorage.com"


def read_r2_credentials(env: Mapping[str, str]) -> R2Credentials:
    """Name any credential that did not arrive, before boto3 makes it look like a network fault.

    Empty counts as missing: a publish workflow exports all three names
    unconditionally, so a secret Vault did not supply arrives as an empty string
    rather than as an absent variable.
    """
    missing = [name for name in R2_ENV_VARS if not env.get(name)]
    if missing:
        raise R2CredentialsError(f"Missing or empty environment variable(s): {', '.join(missing)}.")
    return R2Credentials(
        account_id=env["R2_ACCOUNT_ID"],
        access_key_id=env["R2_ACCESS_KEY_ID"],
        secret_access_key=env["R2_SECRET_ACCESS_KEY"],
    )


def has_r2_credentials(env: Mapping[str, str]) -> bool:
    """Whether the bucket is reachable, for choosing between a credentialed path and a public one."""
    return all(env.get(name) for name in R2_ENV_VARS)


def s3_client(credentials: R2Credentials, config: Config | None = None) -> Any:
    """A boto3 S3 client bound to this account's R2 endpoint.

    ``region_name`` is R2's fixed ``auto``; botocore requires one and refuses to
    sign without it.
    """
    return boto3.client(
        "s3",
        endpoint_url=credentials.endpoint_url,
        aws_access_key_id=credentials.access_key_id,
        aws_secret_access_key=credentials.secret_access_key,
        region_name="auto",
        config=config,
    )
