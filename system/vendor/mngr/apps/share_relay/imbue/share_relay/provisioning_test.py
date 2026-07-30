import pytest

from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.provisioning import RelayProvisioningError
from imbue.share_relay.provisioning import build_relay_instance_name
from imbue.share_relay.provisioning import pick_flavor_id
from imbue.share_relay.provisioning import pick_image_id
from imbue.share_relay.provisioning import pick_public_ipv4


def test_build_relay_instance_name_is_env_and_region_scoped() -> None:
    assert build_relay_instance_name("dev-josh-1", RegionCode("dev-josh-1")) == "share-relay-dev-josh-1-dev-josh-1"
    assert build_relay_instance_name("production", RegionCode("us1")) == "share-relay-production-us1"


def test_pick_image_id_matches_exact_name() -> None:
    images = [
        {"id": "img-1", "name": "Debian 11"},
        {"id": "img-2", "name": "Debian 12"},
    ]
    assert pick_image_id(images, "Debian 12") == "img-2"
    with pytest.raises(RelayProvisioningError):
        pick_image_id(images, "Debian 13")


def test_pick_flavor_id_matches_linux_flavors_only() -> None:
    flavors = [
        {"id": "fl-win", "name": "d2-4", "osType": "windows"},
        {"id": "fl-lin", "name": "d2-4", "osType": "linux"},
        {"id": "fl-big", "name": "b2-15", "osType": "linux"},
    ]
    assert pick_flavor_id(flavors, "d2-4") == "fl-lin"
    with pytest.raises(RelayProvisioningError):
        pick_flavor_id(flavors, "d2-8")


def test_pick_public_ipv4_prefers_public_v4() -> None:
    instance = {
        "name": "share-relay-x",
        "ipAddresses": [
            {"type": "private", "version": 4, "ip": "10.0.0.5"},
            {"type": "public", "version": 6, "ip": "2001:db8::1"},
            {"type": "public", "version": 4, "ip": "203.0.113.7"},
        ],
    }
    assert pick_public_ipv4(instance) == "203.0.113.7"
    with pytest.raises(RelayProvisioningError):
        pick_public_ipv4({"name": "empty", "ipAddresses": []})
