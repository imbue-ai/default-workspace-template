import pytest

from imbue.share_relay.mock_ovh_client_test import FakeOvhClient
from imbue.share_relay.primitives import RegionCode
from imbue.share_relay.provisioning import OvhPublicCloudRelayProvisioner
from imbue.share_relay.provisioning import RelayProvisioningError
from imbue.share_relay.provisioning import build_relay_instance_name
from imbue.share_relay.provisioning import pick_flavor_id
from imbue.share_relay.provisioning import pick_image_id
from imbue.share_relay.provisioning import pick_public_ipv4


def test_build_relay_instance_name_is_env_region_and_ordinal_scoped() -> None:
    assert (
        build_relay_instance_name("dev-josh-1", RegionCode("dev-josh-1"), 1) == "share-relay-dev-josh-1-dev-josh-1-1"
    )
    assert build_relay_instance_name("production", RegionCode("us1"), 2) == "share-relay-production-us1-2"


def test_pick_image_id_matches_exact_name() -> None:
    images = [
        {"id": "img-1", "name": "Debian 12"},
        {"id": "img-2", "name": "Debian 13"},
    ]
    assert pick_image_id(images, "Debian 13") == "img-2"
    with pytest.raises(RelayProvisioningError):
        pick_image_id(images, "Debian 14")


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


def _provisioner(client: FakeOvhClient) -> OvhPublicCloudRelayProvisioner:
    return OvhPublicCloudRelayProvisioner(client=client, project_id="proj-1")


def test_ensure_ssh_key_reuses_a_matching_project_key() -> None:
    client = FakeOvhClient(
        {"/cloud/project/proj-1/sshkey": [{"id": "key-1", "name": "relay", "publicKey": "ssh-ed25519 AAAA relay\n"}]}
    )

    key_id = _provisioner(client).ensure_ssh_key("relay", "ssh-ed25519 AAAA relay", "US-EAST-VA-1")

    assert key_id == "key-1"
    assert client.post_calls == []


def test_ensure_ssh_key_creates_when_absent() -> None:
    client = FakeOvhClient({"/cloud/project/proj-1/sshkey": []})
    client.post_response = {"id": "key-new"}

    key_id = _provisioner(client).ensure_ssh_key("relay", "ssh-ed25519 AAAA relay", "US-EAST-VA-1")

    assert key_id == "key-new"
    assert client.post_calls == [
        (
            "/cloud/project/proj-1/sshkey",
            {"name": "relay", "publicKey": "ssh-ed25519 AAAA relay", "region": "US-EAST-VA-1"},
        )
    ]


def test_ensure_ssh_key_rejects_a_name_collision_with_different_key_material() -> None:
    # Silently reusing a stale key after rotation would produce an instance the
    # operator cannot SSH into; fail loudly instead.
    client = FakeOvhClient(
        {"/cloud/project/proj-1/sshkey": [{"id": "key-1", "name": "relay", "publicKey": "ssh-ed25519 OLD relay"}]}
    )

    with pytest.raises(RelayProvisioningError, match="different key material"):
        _provisioner(client).ensure_ssh_key("relay", "ssh-ed25519 NEW relay", "US-EAST-VA-1")


def test_wait_for_instance_active_returns_the_active_instance() -> None:
    client = FakeOvhClient({"/cloud/project/proj-1/instance/inst-1": {"id": "inst-1", "status": "ACTIVE"}})

    instance = _provisioner(client).wait_for_instance_active("inst-1")

    assert instance["status"] == "ACTIVE"


def test_wait_for_instance_active_raises_on_a_terminal_status() -> None:
    client = FakeOvhClient({"/cloud/project/proj-1/instance/inst-1": {"id": "inst-1", "status": "ERROR"}})

    with pytest.raises(RelayProvisioningError, match="entered status ERROR"):
        _provisioner(client).wait_for_instance_active("inst-1")


def test_list_relay_instances_filters_by_name_prefix() -> None:
    client = FakeOvhClient(
        {
            "/cloud/project/proj-1/instance": [
                {"id": "a", "name": "share-relay-staging-us1"},
                {"id": "b", "name": "share-relay-staging-us2"},
                {"id": "c", "name": "unrelated-vm"},
            ]
        }
    )

    instances = _provisioner(client).list_relay_instances("share-relay-staging-")

    assert [instance["id"] for instance in instances] == ["a", "b"]


def test_delete_instance_targets_the_project_scoped_url() -> None:
    client = FakeOvhClient()

    _provisioner(client).delete_instance("inst-9")

    assert client.deleted_urls == ["/cloud/project/proj-1/instance/inst-9"]
