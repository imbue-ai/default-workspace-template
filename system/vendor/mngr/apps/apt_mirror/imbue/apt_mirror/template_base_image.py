"""Reading the default-workspace-template's committed image-base and apt-snapshot pins.

The template's Dockerfile pins its base image by digest and every apt
operation in that image resolves against the archive frozen at the committed
snapshot timestamp. apt never downgrades, so the base's own package versions
must exist in that snapshot's index; the two pins are bumped together (the
release runbook, apps/minds/docs/deploy/ops/app-release.md step 0) and the
release test in test_apt_mirror_release.py proves they still agree.

Tags cut before the digest pin landed float their base on the bare tag, so a
fresh build of one resolves whatever Docker Hub currently serves under it. The
table below names, per snapshot timestamp, the digest whose packages that
snapshot covers, and the pool bake's seed of such a tag builds it against that
digest instead (imbue-ai/mngr-internal#1143).
"""

from collections.abc import Mapping
from pathlib import Path
from typing import Final

from imbue.apt_mirror.data_types import DefaultWorkspaceTemplatePin
from imbue.apt_mirror.data_types import TemplateCheckoutPins
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.fetcher import open_http_upstream_fetcher
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.parsing import parse_dockerfile_base_image
from imbue.apt_mirror.parsing import parse_dockerfile_from_image
from imbue.apt_mirror.parsing import parse_image_reference
from imbue.apt_mirror.parsing import validate_snapshot_timestamp
from imbue.imbue_common.pure import pure

# The template repo is public, so its committed files are readable without a token.
_DEFAULT_WORKSPACE_TEMPLATE_RAW_BASE_URL: Final[str] = (
    "https://raw.githubusercontent.com/imbue-ai/default-workspace-template"
)
_DEFAULT_WORKSPACE_TEMPLATE_DEFAULT_REF: Final[str] = "main"
_DEFAULT_WORKSPACE_TEMPLATE_REF_ENV_VAR: Final[str] = "DEFAULT_WORKSPACE_TEMPLATE_REF"
_DOCKERFILE_PATH: Final[str] = "system/Dockerfile"
_APT_SNAPSHOT_TIMESTAMP_PATH: Final[str] = ".mngr/apt-snapshot-timestamp"
_GITHUB_TIMEOUT_SECONDS: Final[float] = 60.0

# The digest-pinned base a floating ``FROM`` resolves to when the template
# pins the given apt snapshot: a build whose own packages that snapshot's
# index lists, so apt can install the template's toolchain on top of it. The
# image name must equal the floating ``FROM`` it stands in for; a tag whose
# base has another name is refused rather than silently built against this one.
# CLEANUP: drop this table and the floating-FROM override once every tag that
# still floats its base (minds-v0.6.2 and older) is retired from the pool and
# the gen-1 -> gen-2 migration has finished (phase 6 of
# blueprint/slice-fleet-cutover).
PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP: Final[Mapping[str, str]] = {
    "20260725T000000Z": (
        "python:3.12-slim-trixie@sha256:57cd7c3a7a273101a6485ba99423ee568157882804b1124b4dd04266317710de"
    ),
}


@pure
def resolve_default_workspace_template_ref(environ: Mapping[str, str]) -> str:
    """The template ref to read pins from: ``DEFAULT_WORKSPACE_TEMPLATE_REF`` when set and non-empty, else main."""
    return environ.get(_DEFAULT_WORKSPACE_TEMPLATE_REF_ENV_VAR) or _DEFAULT_WORKSPACE_TEMPLATE_DEFAULT_REF


@pure
def default_workspace_template_file_url(ref: str, repo_relative_path: str) -> str:
    """The raw-content URL of one committed file at a template ref (branch, tag, or SHA)."""
    return f"{_DEFAULT_WORKSPACE_TEMPLATE_RAW_BASE_URL}/{ref}/{repo_relative_path}"


def fetch_default_workspace_template_pin(fetcher: UpstreamFetcherInterface, ref: str) -> DefaultWorkspaceTemplatePin:
    """Read the template's Dockerfile base-image pin and apt snapshot timestamp at ``ref``.

    Raises AptMirrorTemplateBaseImageError when either file is missing at the
    ref or the base image floats (no digest), and AptMirrorInvalidTimestampError
    when the committed timestamp is malformed.
    """
    dockerfile_url = default_workspace_template_file_url(ref, _DOCKERFILE_PATH)
    dockerfile_data = fetcher.fetch(dockerfile_url)
    if dockerfile_data is None:
        raise AptMirrorTemplateBaseImageError(f"No Dockerfile at {dockerfile_url}")
    base_image_ref = parse_dockerfile_base_image(dockerfile_data.decode("utf-8", errors="replace"))

    timestamp_url = default_workspace_template_file_url(ref, _APT_SNAPSHOT_TIMESTAMP_PATH)
    timestamp_data = fetcher.fetch(timestamp_url)
    if timestamp_data is None:
        raise AptMirrorTemplateBaseImageError(f"No apt snapshot timestamp at {timestamp_url}")
    apt_snapshot_timestamp = validate_snapshot_timestamp(timestamp_data.decode("utf-8", errors="replace").strip())
    return DefaultWorkspaceTemplatePin(base_image_ref=base_image_ref, apt_snapshot_timestamp=apt_snapshot_timestamp)


def read_default_workspace_template_pin(environ: Mapping[str, str]) -> DefaultWorkspaceTemplatePin:
    """The pins at the template ref ``environ`` names (see ``resolve_default_workspace_template_ref``), read from GitHub.

    Opens and closes its own HTTP client; the release tests that only need the
    pin call this rather than wiring a fetcher.
    """
    with open_http_upstream_fetcher(_GITHUB_TIMEOUT_SECONDS) as fetcher:
        return fetch_default_workspace_template_pin(fetcher, resolve_default_workspace_template_ref(environ))


def _read_checkout_file(workspace_dir: Path, repo_relative_path: str) -> str:
    file_path = workspace_dir / repo_relative_path
    try:
        return file_path.read_text()
    except OSError as e:
        raise AptMirrorTemplateBaseImageError(f"Cannot read {file_path} from the template checkout") from e


def read_template_checkout_pins(workspace_dir: Path) -> TemplateCheckoutPins:
    """The Dockerfile base (pinned or floating) and apt snapshot timestamp a template working tree commits.

    Raises AptMirrorTemplateBaseImageError when either file is unreadable or
    the ``FROM`` reference is malformed, and AptMirrorInvalidTimestampError
    when the committed timestamp is malformed.
    """
    base_image = parse_dockerfile_from_image(_read_checkout_file(workspace_dir, _DOCKERFILE_PATH))
    apt_snapshot_timestamp = validate_snapshot_timestamp(
        _read_checkout_file(workspace_dir, _APT_SNAPSHOT_TIMESTAMP_PATH).strip()
    )
    return TemplateCheckoutPins(base_image=base_image, apt_snapshot_timestamp=apt_snapshot_timestamp)


@pure
def build_base_image_build_context_arg(image_name: str, pinned_image_ref: str) -> str:
    """The ``docker build`` flag that resolves a Dockerfile's ``FROM <image_name>`` to ``pinned_image_ref``.

    BuildKit's named build contexts override an image by the exact name a
    ``FROM`` line uses, so the Dockerfile is built unmodified and the daemon
    never consults the registry for the floating tag (a ``--pull`` still
    resolves the override, not the tag).
    """
    return f"--build-context={image_name}=docker-image://{pinned_image_ref}"


@pure
def resolve_floating_base_image_build_context_arg(pins: TemplateCheckoutPins) -> str | None:
    """The build flag a template checkout with a floating ``FROM`` needs to build against its snapshot's base.

    None when the Dockerfile pins its own base (the pin is the source of truth
    from then on). Raises AptMirrorTemplateBaseImageError when the base floats
    and the table names no digest for the checkout's snapshot, or names a
    digest of a different image than the ``FROM`` line uses: building such a
    checkout would resolve whatever the registry serves today, which is the
    failure this override exists to prevent.
    """
    if pins.base_image.is_digest_pinned:
        return None
    pinned_image_ref = PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP.get(pins.apt_snapshot_timestamp)
    if pinned_image_ref is None:
        raise AptMirrorTemplateBaseImageError(
            f"the template's Dockerfile floats its base image {pins.base_image.image_ref!r} and no pinned base is "
            f"recorded for its apt snapshot {pins.apt_snapshot_timestamp} (PINNED_BASE_IMAGE_REF_BY_APT_SNAPSHOT_TIMESTAMP "
            "in imbue.apt_mirror.template_base_image); a fresh build would resolve whatever the registry serves today"
        )
    pinned_base_image = parse_image_reference(pinned_image_ref)
    if pinned_base_image.image_name != pins.base_image.image_name:
        raise AptMirrorTemplateBaseImageError(
            f"the template's Dockerfile floats its base image {pins.base_image.image_ref!r}, but the pinned base "
            f"recorded for its apt snapshot {pins.apt_snapshot_timestamp} is {pinned_image_ref!r}, a different image"
        )
    return build_base_image_build_context_arg(pins.base_image.image_name, pinned_image_ref)
