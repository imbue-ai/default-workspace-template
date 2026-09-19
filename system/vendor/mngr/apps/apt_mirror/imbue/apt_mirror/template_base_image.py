"""Reading the default-workspace-template's committed image-base and apt-snapshot pins.

The template's Dockerfile pins its base image by digest and every apt
operation in that image resolves against the archive frozen at the committed
snapshot timestamp. apt never downgrades, so the base's own package versions
must exist in that snapshot's index; the two pins are bumped together (the
release runbook, apps/minds/docs/deploy/ops/app-release.md step 0) and the
release test in test_apt_mirror_release.py proves they still agree.
"""

from collections.abc import Mapping
from typing import Final

from imbue.apt_mirror.data_types import DefaultWorkspaceTemplatePin
from imbue.apt_mirror.errors import AptMirrorTemplateBaseImageError
from imbue.apt_mirror.fetcher import open_http_upstream_fetcher
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.parsing import parse_dockerfile_base_image
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
