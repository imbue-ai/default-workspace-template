"""``minds-admin artifacts``: populate and audit the pinned-artifact set on imbue's mirror.

The mirror (apps/apt_mirror) serves ``/artifacts/<name>/<version>/<subpath>``
straight from its R2 bucket with no read-through, so every artifact the fleet
pins (slices/mirror_artifacts.py) has to be uploaded here before the pin that
references it lands. Authentication is the mirror's R2 credential set
(``APT_MIRROR_R2_*``, from the ``secrets/minds/production/apt-mirror`` Vault
entry), exactly as for ``uv run apt-mirror``.
"""

from collections.abc import Sequence

import click
import httpx

from imbue.apt_mirror.errors import AptMirrorError
from imbue.apt_mirror.fetcher import HttpUpstreamFetcher
from imbue.apt_mirror.interfaces import UpstreamFetcherInterface
from imbue.apt_mirror.storage import build_r2_storage_from_env
from imbue.minds_admin.slices.mirror_artifact_upload import upload_mirror_artifacts
from imbue.minds_admin.slices.mirror_artifact_upload import verify_mirror_artifacts
from imbue.minds_admin.slices.mirror_artifacts import MIRROR_ARTIFACTS
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifact
from imbue.minds_admin.slices.mirror_artifacts import MirrorArtifactError
from imbue.mngr.cli.output_helpers import write_human_line

# Cloud images are hundreds of MB; give the download a generous per-read bound.
_UPSTREAM_TIMEOUT_SECONDS = 300.0


def _select_artifacts(names: Sequence[str]) -> tuple[MirrorArtifact, ...]:
    """The manifest entries with the given names (every entry when none are given). Raises on an unknown name."""
    if not names:
        return MIRROR_ARTIFACTS
    known_names = {artifact.name for artifact in MIRROR_ARTIFACTS}
    unknown = sorted(set(names) - known_names)
    if unknown:
        raise click.UsageError(f"unknown artifact name(s) {unknown}; known: {sorted(known_names)}")
    return tuple(artifact for artifact in MIRROR_ARTIFACTS if artifact.name in names)


def _build_upstream_fetcher() -> UpstreamFetcherInterface:
    return HttpUpstreamFetcher(client=httpx.Client(timeout=_UPSTREAM_TIMEOUT_SECONDS))


@click.group(name="artifacts")
def artifacts_admin() -> None:
    """Populate and audit the pinned upstream artifacts on imbue's mirror (apt.imbuepackages.com)."""


@artifacts_admin.command(name="list")
def list_artifacts() -> None:
    """Print every manifest entry: mirror URL, upstream URL, and pinned digest."""
    for artifact in MIRROR_ARTIFACTS:
        write_human_line(f"{artifact.mirror_url}")
        write_human_line(f"    upstream: {artifact.upstream_url}")
        write_human_line(f"    {artifact.digest_algorithm.value.lower()}: {artifact.digest}")


@artifacts_admin.command(name="upload")
@click.option(
    "--name",
    "names",
    multiple=True,
    help="Only upload the artifacts with this name (repeatable; default: every manifest entry).",
)
@click.option(
    "--force", is_flag=True, default=False, help="Re-fetch and overwrite artifacts the bucket already holds."
)
@click.pass_context
def upload_artifacts(ctx: click.Context, names: tuple[str, ...], force: bool) -> None:
    """Download each pinned artifact from upstream, verify its digest, and store it on the mirror.

    Idempotent: artifacts already in the bucket are skipped unless --force.
    Every download is verified against the digest recorded in the manifest
    and, where upstream publishes one, its checksum file; a mismatch aborts
    before anything is stored. Run this BEFORE landing a pin bump.
    """
    artifacts = _select_artifacts(names)
    try:
        storage = build_r2_storage_from_env()
        report = upload_mirror_artifacts(storage, _build_upstream_fetcher(), artifacts, is_forced=force)
    except (AptMirrorError, MirrorArtifactError) as e:
        write_human_line(f"error: {e}")
        ctx.exit(2)
    for key in report.uploaded_keys:
        write_human_line(f"UPLOADED: {key}")
    write_human_line(
        f"Uploaded {len(report.uploaded_keys)} artifact(s), {len(report.already_present_keys)} already present"
    )


@artifacts_admin.command(name="verify")
@click.option(
    "--name",
    "names",
    multiple=True,
    help="Only check the artifacts with this name (repeatable; default: every manifest entry).",
)
@click.pass_context
def verify_artifacts(ctx: click.Context, names: tuple[str, ...]) -> None:
    """Read-only check that the mirror holds and serves every pinned artifact; exits nonzero on any gap."""
    artifacts = _select_artifacts(names)
    try:
        report = verify_mirror_artifacts(build_r2_storage_from_env(), _build_upstream_fetcher(), artifacts)
    except AptMirrorError as e:
        write_human_line(f"error: {e}")
        ctx.exit(2)
    for key in report.missing_keys:
        write_human_line(f"MISSING: {key}")
    for url in report.unserved_urls:
        write_human_line(
            f"UNSERVED: {url} (in the bucket, but the live Worker does not serve it; `just deploy-apt-mirror`)"
        )
    write_human_line(
        f"Verified: {len(report.present_keys)} present, {len(report.missing_keys)} missing, "
        f"{len(report.unserved_urls)} unserved"
    )
    if not report.is_complete:
        ctx.exit(1)
