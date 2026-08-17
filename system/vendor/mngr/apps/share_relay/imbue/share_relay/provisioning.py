"""OVH Public Cloud provisioning for relay hosts.

A relay is one small Public Cloud instance per env+region. The OVH client
credentials come from the same ``OVH_*`` environment variables the rest of the
monorepo uses (application key/secret + consumer key against the ``ovh-us``
endpoint); the Public Cloud project id rides in ``OVH_CLOUD_PROJECT_ID``.
"""

import os
from collections.abc import Sequence
from typing import Any
from typing import Final

import ovh
from pydantic import Field
from tenacity import retry
from tenacity import retry_if_exception_type
from tenacity import stop_after_delay
from tenacity import wait_fixed

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from imbue.share_relay.errors import ShareRelayError
from imbue.share_relay.primitives import RegionCode

# Default instance shape for a relay: the smallest general-purpose discovery
# flavor with unmetered bandwidth. Overridable per provision call.
DEFAULT_RELAY_FLAVOR_NAME: Final[str] = "d2-4"
DEFAULT_RELAY_IMAGE_NAME: Final[str] = "Debian 13"

_INSTANCE_ACTIVE_TIMEOUT_SECONDS: Final[float] = 600.0
_INSTANCE_POLL_INTERVAL_SECONDS: Final[float] = 10.0


class RelayProvisioningError(ShareRelayError):
    """Raised when an OVH Public Cloud provisioning step fails."""


class _InstanceNotActiveYetError(ShareRelayError):
    """Internal: the instance is still building; tenacity retries the poll."""


@pure
def build_relay_instance_name(env_name: str, region: RegionCode, ordinal: int) -> str:
    """The canonical instance name for one relay: env + region + ordinal (regions run several relays)."""
    return f"share-relay-{env_name}-{region}-{ordinal}"


@pure
def pick_image_id(images: Sequence[dict[str, Any]], image_name: str) -> str:
    """Pick the image id whose name matches exactly. Raises when absent."""
    for image in images:
        if image.get("name") == image_name:
            return str(image["id"])
    available = sorted({str(image.get("name")) for image in images})
    raise RelayProvisioningError(f"No image named {image_name!r} in this region; available: {available[:20]}")


@pure
def pick_flavor_id(flavors: Sequence[dict[str, Any]], flavor_name: str) -> str:
    """Pick the flavor id whose name matches exactly (linux flavors only). Raises when absent."""
    for flavor in flavors:
        if flavor.get("name") == flavor_name and flavor.get("osType") == "linux":
            return str(flavor["id"])
    available = sorted({str(flavor.get("name")) for flavor in flavors if flavor.get("osType") == "linux"})
    raise RelayProvisioningError(f"No linux flavor named {flavor_name!r} in this region; available: {available[:20]}")


@pure
def pick_public_ipv4(instance: dict[str, Any]) -> str:
    """The instance's public IPv4 address. Raises when it has none yet."""
    for address in instance.get("ipAddresses", []):
        if address.get("type") == "public" and address.get("version") == 4:
            return str(address["ip"])
    raise RelayProvisioningError(f"Instance {instance.get('name')} has no public IPv4 address yet")


def make_ovh_client_from_env() -> ovh.Client:
    """Build an OVH API client from the standard ``OVH_*`` environment variables."""
    return ovh.Client(
        endpoint=os.environ.get("OVH_ENDPOINT", "ovh-us"),
        application_key=os.environ["OVH_APPLICATION_KEY"],
        application_secret=os.environ["OVH_APPLICATION_SECRET"],
        consumer_key=os.environ["OVH_CONSUMER_KEY"],
    )


def cloud_project_id_from_env() -> str:
    project_id = os.environ.get("OVH_CLOUD_PROJECT_ID", "")
    if not project_id:
        raise RelayProvisioningError("OVH_CLOUD_PROJECT_ID is not set (the OVH Public Cloud project to provision in)")
    return project_id


class OvhPublicCloudRelayProvisioner(MutableModel):
    """Provisions relay instances through the OVH Public Cloud (`/cloud/project`) API."""

    model_config = {"arbitrary_types_allowed": True}

    client: ovh.Client = Field(frozen=True, description="Authenticated OVH API client")
    project_id: str = Field(frozen=True, description="OVH Public Cloud project (service name)")

    def _project_url(self, suffix: str) -> str:
        return f"/cloud/project/{self.project_id}{suffix}"

    def ensure_ssh_key(self, key_name: str, public_key: str, ovh_region: str) -> str:
        """Register (or reuse) an SSH key in the project; returns its id.

        Reuse requires the stored key material to match: after a local key
        rotation, silently reusing the stale project key by name would produce
        an instance the operator cannot SSH into.
        """
        existing_keys = self.client.get(self._project_url("/sshkey"))
        for key in existing_keys:
            if key.get("name") != key_name:
                continue
            if str(key.get("publicKey", "")).strip() != public_key.strip():
                raise RelayProvisioningError(
                    f"SSH key {key_name!r} already exists in the project with different key material; "
                    "delete it from the project (or provision with a different key name) and retry."
                )
            return str(key["id"])
        created = self.client.post(
            self._project_url("/sshkey"),
            name=key_name,
            publicKey=public_key,
            region=ovh_region,
        )
        return str(created["id"])

    def create_relay_instance(
        self,
        instance_name: str,
        ovh_region: str,
        flavor_name: str,
        image_name: str,
        cloud_init_user_data: str,
        ssh_key_id: str | None,
    ) -> dict[str, Any]:
        """Create the instance and return the (not-yet-active) instance document."""
        images = self.client.get(self._project_url("/image"), region=ovh_region, osType="linux")
        flavors = self.client.get(self._project_url("/flavor"), region=ovh_region)
        body: dict[str, Any] = {
            "name": instance_name,
            "region": ovh_region,
            "flavorId": pick_flavor_id(flavors, flavor_name),
            "imageId": pick_image_id(images, image_name),
            "userData": cloud_init_user_data,
        }
        if ssh_key_id is not None:
            body["sshKeyId"] = ssh_key_id
        return self.client.post(self._project_url("/instance"), **body)

    @retry(
        retry=retry_if_exception_type(_InstanceNotActiveYetError),
        stop=stop_after_delay(_INSTANCE_ACTIVE_TIMEOUT_SECONDS),
        wait=wait_fixed(_INSTANCE_POLL_INTERVAL_SECONDS),
        reraise=True,
    )
    def wait_for_instance_active(self, instance_id: str) -> dict[str, Any]:
        """Poll until the instance reaches ACTIVE (or raise on a terminal status / timeout)."""
        instance = self.client.get(self._project_url(f"/instance/{instance_id}"))
        status = str(instance.get("status", ""))
        if status == "ACTIVE":
            return instance
        if status in ("ERROR", "STOPPED", "DELETED"):
            raise RelayProvisioningError(f"Instance {instance_id} entered status {status} while provisioning")
        raise _InstanceNotActiveYetError(f"Instance {instance_id} is {status or 'pending'}")

    def list_relay_instances(self, name_prefix: str) -> list[dict[str, Any]]:
        instances = self.client.get(self._project_url("/instance"))
        return [instance for instance in instances if str(instance.get("name", "")).startswith(name_prefix)]

    def delete_instance(self, instance_id: str) -> None:
        self.client.delete(self._project_url(f"/instance/{instance_id}"))
