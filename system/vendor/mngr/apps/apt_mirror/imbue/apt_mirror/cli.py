"""Operator CLI for the snapshot-pinned apt mirror: cut, warm, and verify.

Authentication is the R2 credential set itself (``APT_MIRROR_R2_*`` env vars,
populated from the ``secrets/minds/production/apt-mirror`` Vault entry); there
is no service and no admin key. See the README for the bring-up runbook.
"""

from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TypeVar

import click
import httpx
from pydantic import Field

from imbue.apt_mirror.data_types import AptMirrorCompletenessResult
from imbue.apt_mirror.data_types import AptMirrorCutRequest
from imbue.apt_mirror.data_types import DEFAULT_ARCHITECTURES
from imbue.apt_mirror.data_types import DEFAULT_SUITES_BY_ARCHIVE
from imbue.apt_mirror.data_types import PackageListResolution
from imbue.apt_mirror.errors import AptMirrorError
from imbue.apt_mirror.errors import AptMirrorTimestampFileError
from imbue.apt_mirror.fetcher import HttpUpstreamFetcher
from imbue.apt_mirror.package_lists import read_package_lists
from imbue.apt_mirror.parsing import validate_snapshot_timestamp
from imbue.apt_mirror.service import AptMirrorService
from imbue.apt_mirror.storage import build_r2_storage_from_env
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from imbue.mngr.cli.output_helpers import write_human_line
from imbue.mngr.cli.output_helpers import write_stderr_line

# Repo-relative locations of the committed cut record and package lists; this
# app is internal-only (never published), so resolving through __file__ is safe.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
CURRENT_TIMESTAMP_PATH = _PROJECT_ROOT / "current-timestamp"
PACKAGE_LISTS_DIR = _PROJECT_ROOT / "package_lists"

_UPSTREAM_TIMEOUT_SECONDS = 60.0
_DEFAULT_MAX_WORKERS = 16

TCommand = TypeVar("TCommand", bound=Callable[..., None])


def _add_warm_verify_options(command: TCommand) -> TCommand:
    """Add the --timestamp/--list/--timestamp-file/--max-workers options warm and verify share."""
    # Applied in reversed source order so --help lists the options in the same
    # order they appear here (click reverses the applied decorators).
    for decorator in reversed(
        (
            click.option(
                "--timestamp",
                default=None,
                help="Cut timestamp to operate on (default: the committed current-timestamp)",
            ),
            click.option(
                "--list",
                "list_paths",
                type=click.Path(path_type=Path),
                multiple=True,
                help="Package list file(s) (default: every package_lists/*.txt)",
            ),
            click.option(
                "--timestamp-file",
                type=click.Path(path_type=Path),
                default=CURRENT_TIMESTAMP_PATH,
                help="Committed current-timestamp file read when --timestamp is not given",
            ),
            click.option(
                "--max-workers",
                type=click.IntRange(min=1),
                default=_DEFAULT_MAX_WORKERS,
                help="Parallel R2/upstream workers",
            ),
        )
    ):
        command = decorator(command)
    return command


class WarmInvocation(FrozenModel):
    """Parsed arguments shared by the warm and verify commands."""

    timestamp: str = Field(description="The snapshot timestamp to operate on")
    package_names: tuple[str, ...] = Field(description="Deduplicated package names from the lists")
    max_workers: int = Field(description="Parallel R2/upstream operations")


def build_apt_mirror_service() -> AptMirrorService:
    """Build the service against R2 from env config. Raises AptMirrorNotConfiguredError when unset."""
    return AptMirrorService(
        storage=build_r2_storage_from_env(),
        fetcher=HttpUpstreamFetcher(client=httpx.Client(timeout=_UPSTREAM_TIMEOUT_SECONDS)),
    )


def _get_service(ctx: click.Context) -> AptMirrorService:
    """The service from ctx.obj when a test injected one, else the env-configured R2 service."""
    if isinstance(ctx.obj, AptMirrorService):
        return ctx.obj
    else:
        return build_apt_mirror_service()


def read_current_timestamp(timestamp_file: Path) -> str:
    """Read and validate the cut timestamp from the committed current-timestamp file.

    Raises AptMirrorTimestampFileError when the file is missing or unreadable.
    """
    try:
        text = timestamp_file.read_text()
    except OSError as e:
        raise AptMirrorTimestampFileError(f"Cannot read current-timestamp file: {timestamp_file}") from e
    return validate_snapshot_timestamp(text.strip())


def _resolve_warm_invocation(
    timestamp: str | None,
    list_paths: tuple[Path, ...],
    timestamp_file: Path,
    max_workers: int,
) -> WarmInvocation:
    resolved_timestamp = timestamp if timestamp is not None else read_current_timestamp(timestamp_file)
    resolved_list_paths = list_paths if list_paths else tuple(sorted(PACKAGE_LISTS_DIR.glob("*.txt")))
    package_names = read_package_lists(resolved_list_paths)
    return WarmInvocation(
        timestamp=validate_snapshot_timestamp(resolved_timestamp),
        package_names=tuple(package_names),
        max_workers=max_workers,
    )


@contextmanager
def _fail_cleanly_on_mirror_errors(ctx: click.Context) -> Iterator[None]:
    """Convert any AptMirrorError into a one-line stderr message and exit code 2."""
    try:
        yield
    except AptMirrorError as e:
        write_stderr_line(f"error: {e}")
        ctx.exit(2)


def _report_completeness_gaps(result: AptMirrorCompletenessResult) -> None:
    for name in result.unknown_package_names:
        write_human_line(f"UNKNOWN PACKAGE (in no index): {name}")
    for pool_path in result.missing_pool_paths:
        write_human_line(f"MISSING: {pool_path}")


@click.group()
def main() -> None:
    """Manage the snapshot-pinned apt mirror (cut/warm/verify against R2)."""
    setup_logging(level="INFO")


@main.command()
@click.option("--timestamp", required=True, help="snapshot.debian.org timestamp to freeze (YYYYMMDDTHHMMSSZ)")
@click.option(
    "--timestamp-file",
    type=click.Path(path_type=Path),
    default=CURRENT_TIMESTAMP_PATH,
    help="Committed current-timestamp file updated on success",
)
@click.option(
    "--skip-timestamp-file",
    is_flag=True,
    default=False,
    help="Do not update the committed current-timestamp file",
)
@click.pass_context
def cut(ctx: click.Context, timestamp: str, timestamp_file: Path, skip_timestamp_file: bool) -> None:
    """Freeze the index set for a new timestamp into the bucket (idempotent, minutes)."""
    with _fail_cleanly_on_mirror_errors(ctx):
        service = _get_service(ctx)
        result = service.cut(AptMirrorCutRequest(timestamp=timestamp))
        write_human_line(
            f"Cut {result.timestamp}: stored {result.stored_index_count} indexes, "
            f"{result.already_present_count} already present, "
            f"{result.missing_upstream_count} missing upstream"
        )
        if not skip_timestamp_file:
            timestamp_file.write_text(f"{result.timestamp}\n")
            write_human_line(f"Updated {timestamp_file} -- commit it, then mirror the same value into dwt")


@main.command()
@_add_warm_verify_options
@click.pass_context
def warm(
    ctx: click.Context,
    timestamp: str | None,
    list_paths: tuple[Path, ...],
    timestamp_file: Path,
    max_workers: int,
) -> None:
    """Fetch every listed package's pool files into the cache; exits nonzero on any gap."""
    with _fail_cleanly_on_mirror_errors(ctx):
        invocation = _resolve_warm_invocation(timestamp, list_paths, timestamp_file, max_workers)
        service = _get_service(ctx)
        resolution = _resolve_packages(service, invocation)
        result = service.warm(invocation.timestamp, resolution, invocation.max_workers)
        write_human_line(
            f"Warmed {result.timestamp}: examined {result.examined_count} pool files, "
            f"fetched {result.fetched_count}, {result.already_cached_count} already cached"
        )
        _report_completeness_gaps(result)
        if not result.is_complete:
            ctx.exit(1)


@main.command()
@_add_warm_verify_options
@click.pass_context
def verify(
    ctx: click.Context,
    timestamp: str | None,
    list_paths: tuple[Path, ...],
    timestamp_file: Path,
    max_workers: int,
) -> None:
    """Read-only check that a cut timestamp's listed pool files are all cached; exits nonzero on any gap."""
    with _fail_cleanly_on_mirror_errors(ctx):
        invocation = _resolve_warm_invocation(timestamp, list_paths, timestamp_file, max_workers)
        service = _get_service(ctx)
        resolution = _resolve_packages(service, invocation)
        result = service.verify(invocation.timestamp, resolution, invocation.max_workers)
        write_human_line(f"Verified {result.timestamp}: {result.cached_count} listed pool files cached")
        _report_completeness_gaps(result)
        if not result.is_complete:
            ctx.exit(1)


def _resolve_packages(service: AptMirrorService, invocation: WarmInvocation) -> PackageListResolution:
    """Resolve the invocation's package names against the cut indexes for the default suites/arches."""
    return service.resolve_package_names(
        timestamp=invocation.timestamp,
        package_names=invocation.package_names,
        architectures=DEFAULT_ARCHITECTURES,
        suites_by_archive=dict(DEFAULT_SUITES_BY_ARCHIVE),
    )


if __name__ == "__main__":
    main()
