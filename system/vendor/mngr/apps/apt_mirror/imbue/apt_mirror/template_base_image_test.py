from pathlib import Path

import pytest

from imbue.apt_mirror.data_types import DockerfileBaseImage
from imbue.apt_mirror.data_types import TemplateCheckoutPins
from imbue.apt_mirror.errors import AptMirrorError
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.apt_mirror.parsing import parse_image_reference
from imbue.apt_mirror.parsing import validate_snapshot_timestamp
from imbue.apt_mirror.template_base_image import PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP
from imbue.apt_mirror.template_base_image import build_base_image_build_context_arg
from imbue.apt_mirror.template_base_image import default_workspace_template_file_url
from imbue.apt_mirror.template_base_image import fetch_default_workspace_template_pin
from imbue.apt_mirror.template_base_image import read_template_checkout_pins
from imbue.apt_mirror.template_base_image import resolve_default_workspace_template_ref
from imbue.apt_mirror.template_base_image import resolve_floating_base_image_build_context_arg
from imbue.apt_mirror.testing import write_template_checkout

_PINNED_BASE_IMAGE_REF = (
    "python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
)
_PINNED_DOCKERFILE = f"# comment\nFROM {_PINNED_BASE_IMAGE_REF}\nRUN true\n".encode()


def _fetcher_for_ref(ref: str, dockerfile: bytes | None, timestamp: bytes | None) -> MappingUpstreamFetcher:
    responses: dict[str, bytes] = {}
    if dockerfile is not None:
        responses[default_workspace_template_file_url(ref, "system/Dockerfile")] = dockerfile
    if timestamp is not None:
        responses[default_workspace_template_file_url(ref, ".mngr/apt-snapshot-timestamp")] = timestamp
    return MappingUpstreamFetcher(responses_by_url=responses)


def test_fetch_pin_reads_the_digest_pinned_base_and_the_snapshot_timestamp_at_the_ref() -> None:
    fetcher = _fetcher_for_ref("minds-v0.6.3", _PINNED_DOCKERFILE, b"20260725T000000Z\n")

    pin = fetch_default_workspace_template_pin(fetcher, "minds-v0.6.3")

    assert pin.base_image_ref == _PINNED_BASE_IMAGE_REF
    assert pin.apt_snapshot_timestamp == "20260725T000000Z"
    assert all("/minds-v0.6.3/" in url for url in fetcher.fetched_urls)


@pytest.mark.parametrize(
    ("dockerfile", "timestamp", "expected_error", "message"),
    [
        (
            b"FROM python:3.12-slim-trixie\n",
            b"20260725T000000Z\n",
            AptMirrorTemplateBaseImageError,
            "not digest-pinned",
        ),
        (None, b"20260725T000000Z\n", AptMirrorTemplateBaseImageError, "No Dockerfile"),
        (_PINNED_DOCKERFILE, None, AptMirrorTemplateBaseImageError, "No apt snapshot timestamp"),
        (_PINNED_DOCKERFILE, b"yesterday\n", AptMirrorInvalidTimestampError, "yesterday"),
    ],
    ids=["floating-base-tag", "absent-dockerfile", "absent-timestamp", "malformed-timestamp"],
)
def test_fetch_pin_rejects_a_floating_base_or_a_missing_or_malformed_pin(
    dockerfile: bytes | None, timestamp: bytes | None, expected_error: type[AptMirrorError], message: str
) -> None:
    fetcher = _fetcher_for_ref("main", dockerfile, timestamp)

    with pytest.raises(expected_error, match=message):
        fetch_default_workspace_template_pin(fetcher, "main")


@pytest.mark.parametrize(
    ("environ", "expected_ref"),
    [
        ({"DEFAULT_WORKSPACE_TEMPLATE_REF": "minds-v0.6.3"}, "minds-v0.6.3"),
        ({"DEFAULT_WORKSPACE_TEMPLATE_REF": ""}, "main"),
        ({}, "main"),
    ],
)
def test_resolve_template_ref_reads_the_env_var_and_falls_back_to_main(
    environ: dict[str, str], expected_ref: str
) -> None:
    assert resolve_default_workspace_template_ref(environ) == expected_ref


_FLOATING_DOCKERFILE = "FROM python:3.12-slim-trixie\nRUN true\n"
_SNAPSHOT_WITH_A_PINNED_BASE = "20260725T000000Z"


def test_the_pinned_base_for_the_snapshot_every_floating_tag_pins_is_the_debian_13_6_index() -> None:
    """Every default-workspace-template tag through minds-v0.6.2 floats on this snapshot; the seed builds them on this base."""
    assert PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP[_SNAPSHOT_WITH_A_PINNED_BASE] == _PINNED_BASE_IMAGE_REF


@pytest.mark.parametrize(
    ("timestamp", "pinned_image_ref"), sorted(PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP.items())
)
def test_every_recorded_base_pin_is_a_snapshot_timestamp_and_a_digest_pinned_reference(
    timestamp: str, pinned_image_ref: str
) -> None:
    """An entry the override cannot use (a bad timestamp, a floating value) must fail here, not at bake time."""
    assert validate_snapshot_timestamp(timestamp) == timestamp
    assert parse_image_reference(pinned_image_ref).is_digest_pinned


def test_read_template_checkout_pins_reads_a_floating_base_and_the_timestamp(tmp_path: Path) -> None:
    checkout = write_template_checkout(tmp_path, _FLOATING_DOCKERFILE, "20260725T000000Z\n")

    pins = read_template_checkout_pins(checkout)

    assert pins == TemplateCheckoutPins(
        base_image=DockerfileBaseImage(image_name="python:3.12-slim-trixie", digest=None),
        apt_snapshot_timestamp="20260725T000000Z",
    )


def test_read_template_checkout_pins_reads_a_pinned_base(tmp_path: Path) -> None:
    checkout = write_template_checkout(tmp_path, _PINNED_DOCKERFILE.decode(), "20260725T000000Z\n")

    pins = read_template_checkout_pins(checkout)

    assert pins.base_image.is_digest_pinned
    assert pins.base_image.image_ref == _PINNED_BASE_IMAGE_REF


@pytest.mark.parametrize(
    ("dockerfile", "timestamp", "expected_error", "message"),
    [
        (None, "20260725T000000Z\n", AptMirrorTemplateBaseImageError, "Cannot read"),
        (_FLOATING_DOCKERFILE, None, AptMirrorTemplateBaseImageError, "Cannot read"),
        ("RUN true\n", "20260725T000000Z\n", AptMirrorTemplateBaseImageError, "no FROM line"),
        (_FLOATING_DOCKERFILE, "yesterday\n", AptMirrorInvalidTimestampError, "yesterday"),
    ],
    ids=["absent-dockerfile", "absent-timestamp", "no-from", "malformed-timestamp"],
)
def test_read_template_checkout_pins_rejects_a_missing_or_malformed_pin(
    tmp_path: Path,
    dockerfile: str | None,
    timestamp: str | None,
    expected_error: type[AptMirrorError],
    message: str,
) -> None:
    checkout = write_template_checkout(tmp_path, dockerfile, timestamp)

    with pytest.raises(expected_error, match=message):
        read_template_checkout_pins(checkout)


def test_build_base_image_build_context_arg_overrides_the_from_name_with_the_pinned_ref() -> None:
    assert build_base_image_build_context_arg("python:3.12-slim-trixie", "python:3.12-slim-trixie@sha256:abc") == (
        "--build-context=python:3.12-slim-trixie=docker-image://python:3.12-slim-trixie@sha256:abc"
    )


def test_floating_base_override_names_the_snapshots_pinned_base() -> None:
    pins = TemplateCheckoutPins(
        base_image=DockerfileBaseImage(image_name="python:3.12-slim-trixie", digest=None),
        apt_snapshot_timestamp=_SNAPSHOT_WITH_A_PINNED_BASE,
    )

    assert resolve_floating_base_image_build_context_arg(pins) == (
        f"--build-context=python:3.12-slim-trixie=docker-image://{_PINNED_BASE_IMAGE_REF}"
    )


def test_floating_base_override_is_none_when_the_dockerfile_pins_its_own_base() -> None:
    pins = TemplateCheckoutPins(
        base_image=DockerfileBaseImage(image_name="python:3.12-slim-trixie", digest="sha256:" + "f" * 64),
        apt_snapshot_timestamp="20990101T000000Z",
    )

    assert resolve_floating_base_image_build_context_arg(pins) is None


def test_floating_base_override_refuses_a_snapshot_with_no_recorded_base() -> None:
    pins = TemplateCheckoutPins(
        base_image=DockerfileBaseImage(image_name="python:3.12-slim-trixie", digest=None),
        apt_snapshot_timestamp="20990101T000000Z",
    )

    with pytest.raises(
        AptMirrorTemplateBaseImageError, match="no pinned base is recorded for its apt snapshot 20990101T000000Z"
    ):
        resolve_floating_base_image_build_context_arg(pins)


def test_floating_base_override_refuses_a_base_of_another_name() -> None:
    pins = TemplateCheckoutPins(
        base_image=DockerfileBaseImage(image_name="python:3.13-slim-trixie", digest=None),
        apt_snapshot_timestamp=_SNAPSHOT_WITH_A_PINNED_BASE,
    )

    with pytest.raises(AptMirrorTemplateBaseImageError, match="a different image"):
        resolve_floating_base_image_build_context_arg(pins)
