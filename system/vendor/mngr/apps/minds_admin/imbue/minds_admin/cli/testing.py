"""Shared non-fixture test utilities for the cli package."""

import base64
import os

from pydantic import SecretStr

from imbue.minds_admin.cli._tier_secrets import WorkspaceStorageConfig


def make_workspace_storage_config() -> WorkspaceStorageConfig:
    """A tier workspace-storage bucket config with throwaway credentials and a fresh KEK."""
    return WorkspaceStorageConfig(
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
        access_key_id=SecretStr("AKIA"),
        secret_access_key=SecretStr("secret"),
        bucket="bucket",
        kek_base64=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        key_prefix="dev-x/",
    )
