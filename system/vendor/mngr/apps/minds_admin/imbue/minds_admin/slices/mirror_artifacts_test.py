from imbue.apt_mirror.cli import PACKAGE_LISTS_DIR
from imbue.apt_mirror.package_lists import read_package_lists
from imbue.minds_admin.slices.mirror_artifacts import DESKTOP_LIMA_GUEST_IMAGE_ARM64
from imbue.minds_admin.slices.mirror_artifacts import DigestAlgorithm
from imbue.minds_admin.slices.mirror_artifacts import GEN2_SLICE_GUEST_IMAGE
from imbue.minds_admin.slices.mirror_artifacts import GVISOR_MIRROR_RELEASE_URL
from imbue.minds_admin.slices.mirror_artifacts import MIRROR_ARTIFACTS
from imbue.mngr_vps.host_setup import PINNED_CONTAINERD_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import PINNED_DOCKER_APT_VERSION_CORE
from imbue.mngr_vps.host_setup import PINNED_GVISOR_RELEASE
from imbue.observability.collector_install import otelcol_contrib_deb_mirror_url

_DIGEST_LENGTH_BY_ALGORITHM = {DigestAlgorithm.SHA256: 64, DigestAlgorithm.SHA512: 128}


def test_manifest_entries_have_unique_keys_and_well_formed_digests() -> None:
    keys = [artifact.object_key for artifact in MIRROR_ARTIFACTS]
    assert len(keys) == len(set(keys))
    for artifact in MIRROR_ARTIFACTS:
        assert len(artifact.digest) == _DIGEST_LENGTH_BY_ALGORITHM[artifact.digest_algorithm], artifact.object_key
        assert artifact.digest == artifact.digest.lower()
        assert artifact.upstream_url.startswith("https://"), artifact.object_key
        assert artifact.mirror_url == f"https://apt.imbuepackages.com/{artifact.object_key}"


def test_manifest_covers_every_pinned_download_the_fleet_makes() -> None:
    names = {artifact.name for artifact in MIRROR_ARTIFACTS}
    assert names == {"debian-cloud-image", "gvisor", "age", "s5cmd", "uv", "onetun", "otelcol-contrib"}
    # Both guest images (cloud slices and desktop Lima) come from one release.
    assert GEN2_SLICE_GUEST_IMAGE.version == DESKTOP_LIMA_GUEST_IMAGE_ARM64.version
    assert GEN2_SLICE_GUEST_IMAGE.subpath.endswith("-amd64-" + GEN2_SLICE_GUEST_IMAGE.version + ".qcow2")
    # gVisor keeps the upstream per-arch layout under the release directory the
    # shared install script appends ${ARCH}/ to.
    assert GVISOR_MIRROR_RELEASE_URL == f"https://apt.imbuepackages.com/artifacts/gvisor/{PINNED_GVISOR_RELEASE}"
    gvisor_subpaths = {artifact.subpath for artifact in MIRROR_ARTIFACTS if artifact.name == "gvisor"}
    assert gvisor_subpaths == {
        "x86_64/runsc",
        "x86_64/runsc.sha512",
        "x86_64/containerd-shim-runsc-v1",
        "x86_64/containerd-shim-runsc-v1.sha512",
    }


def test_manifest_otelcol_entries_match_the_urls_the_collector_install_downloads() -> None:
    for goarch in ("amd64", "arm64"):
        matching = [a for a in MIRROR_ARTIFACTS if a.name == "otelcol-contrib" and a.subpath.endswith(f"{goarch}.deb")]
        assert len(matching) == 1
        assert matching[0].mirror_url == otelcol_contrib_deb_mirror_url(goarch)


def test_docker_package_list_pins_the_engine_versions_the_guest_image_installs() -> None:
    # The apt mirror's docker.txt is what warms the frozen docker archive; its
    # exact pins must be the ones the gen-2 guest customization installs.
    specs = read_package_lists([PACKAGE_LISTS_DIR / "docker.txt"])
    version_by_name = {spec.name: spec.version for spec in specs}
    assert version_by_name["docker-ce"] == f"{PINNED_DOCKER_APT_VERSION_CORE}~debian.13~trixie"
    assert version_by_name["docker-ce-cli"] == f"{PINNED_DOCKER_APT_VERSION_CORE}~debian.13~trixie"
    assert version_by_name["containerd.io"] == f"{PINNED_CONTAINERD_APT_VERSION_CORE}~debian.13~trixie"
    assert version_by_name["docker-buildx-plugin"] is None
    assert version_by_name["docker-compose-plugin"] is None
    # docker-ce Recommends this one (installed by default) with no version tie,
    # so the newest in the frozen index is what apt fetches from the mirror.
    assert version_by_name["docker-ce-rootless-extras"] is None
