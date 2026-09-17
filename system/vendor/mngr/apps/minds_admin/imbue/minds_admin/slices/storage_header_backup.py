"""Uploading a gen-2 box's LUKS header backup to the tier's workspace-storage bucket.

The gen-2 prep stages a fresh header backup on the box's tmpfs after every
run (see ``storage_encryption``); the CLI fetches it over management SSH and
parks it here, next to the workspace stop/start artifacts, keyed by the box
and the volume's LUKS UUID. A corrupt header loses every slice on the box at
once, and the RAID mirror does not protect against a bad write, so the backup
is the only path back to the volume.
"""

from typing import Any

from botocore.exceptions import BotoCoreError
from botocore.exceptions import ClientError
from loguru import logger

from imbue.mngr_imbue_cloud.errors import BareMetalProvisioningError


def upload_storage_header_backup(client: Any, bucket: str, object_key: str, header_bytes: bytes) -> None:
    """Put the header backup at ``object_key`` in the tier bucket, raising ``BareMetalProvisioningError`` on failure."""
    if not header_bytes:
        raise BareMetalProvisioningError(f"refusing to upload an empty LUKS header backup to {bucket}/{object_key}")
    try:
        client.put_object(Bucket=bucket, Key=object_key, Body=header_bytes)
    except (ClientError, BotoCoreError) as e:
        raise BareMetalProvisioningError(
            f"failed to upload the LUKS header backup to {bucket}/{object_key}: {e}"
        ) from e
    logger.info(
        "Uploaded the storage volume's LUKS header backup ({} bytes) to {}/{}", len(header_bytes), bucket, object_key
    )
