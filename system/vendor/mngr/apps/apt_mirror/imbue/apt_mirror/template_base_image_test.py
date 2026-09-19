import pytest

from imbue.apt_mirror.errors import AptMirrorError
from imbue.apt_mirror.errors import AptMirrorInvalidTimestampError
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.mock_apt_mirror_test import MappingUpstreamFetcher
from imbue.apt_mirror.template_base_image import default_workspace_template_file_url
from imbue.apt_mirror.template_base_image import fetch_default_workspace_template_pin
from imbue.apt_mirror.template_base_image import resolve_default_workspace_template_ref

_PINNED_DOCKERFILE = (
    b"# comment\n"
    b"FROM python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de\n"
    b"RUN true\n"
)


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

    assert pin.base_image_ref == (
        "python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    )
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
