"""Unit tests for the mapper polling loop."""

import io
import tarfile
import time
from pathlib import Path

from imbue.mngr.api.providers import get_local_host
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.config.data_types import OutputOptions
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import OutputFormat
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr.providers.local.instance import LocalProviderInstance
from imbue.mngr.utils.plugin_testing import PLACEHOLDER_AGENT_TYPE
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr_mapreduce.agent_stopper import AgentStopper
from imbue.mngr_mapreduce.archive import ARCHIVE_SUBPATH
from imbue.mngr_mapreduce.data_types import AgentMetadata
from imbue.mngr_mapreduce.data_types import LaunchConfig
from imbue.mngr_mapreduce.data_types import MapReduceContext
from imbue.mngr_mapreduce.data_types import MapperInfo
from imbue.mngr_mapreduce.mock_recipe_test import RecordingRecipe
from imbue.mngr_mapreduce.orchestration import launch_and_poll_mappers

_ARCHIVE_EXTRACTION_FAILED_SUMMARY = "Mapper published an archive but it could not be extracted."

# Long enough that no mapper in these tests is ever judged timed out: the
# archive is already there when the loop starts, so the first pass finalizes it.
_NO_TIMEOUT_SECONDS = 3600.0


def _make_tarball_bytes(member_name: str, member_contents: bytes) -> bytes:
    """Build a valid gzipped tarball holding a single file."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        member = tarfile.TarInfo(name=member_name)
        member.size = len(member_contents)
        tar.addfile(member, io.BytesIO(member_contents))
    return buffer.getvalue()


def _poll_one_mapper_that_published(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
    archive_bytes: bytes,
) -> tuple[RecordingRecipe, MapperInfo, Path, list[AgentMetadata]]:
    """Poll a single pre-launched mapper whose volume already holds ``archive_bytes``.

    Publishes the archive to the agent's state volume before the loop starts,
    so the first polling pass sees the outputs as ready, finalizes the mapper
    and returns.
    """
    local_host = get_local_host(temp_mngr_ctx)
    agent_id = AgentId.generate()
    host_volume = local_provider.get_volume_for_host(local_host)
    assert host_volume is not None
    host_volume.get_agent_volume(agent_id).write_files({ARCHIVE_SUBPATH: archive_bytes})

    info = MapperInfo(
        task_id=f"libs/mngr/some_test.py::test_thing[{get_short_random_string()}]",
        agent_id=agent_id,
        agent_name=AgentName(f"mapper-{get_short_random_string()}"),
        branch_name=f"mock/mapper-{get_short_random_string()}",
        created_at=time.monotonic(),
    )

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    ctx = MapReduceContext(
        mngr_ctx=temp_mngr_ctx,
        source_dir=tmp_path,
        run_name="20260101000000",
        output_dir=output_dir,
        output_opts=OutputOptions(output_format=OutputFormat.HUMAN),
    )
    config = LaunchConfig(
        source_dir=tmp_path,
        source_host=local_host,
        base_commit="0" * 40,
        agent_type=AgentTypeName(PLACEHOLDER_AGENT_TYPE),
        provider_name=ProviderInstanceName("local"),
    )

    recipe = RecordingRecipe()
    with AgentStopper() as stopper:
        metadata = launch_and_poll_mappers(
            recipe=recipe,
            ctx=ctx,
            tasks=[],
            config=config,
            mngr_ctx=temp_mngr_ctx,
            max_agents=0,
            agent_timeout_seconds=_NO_TIMEOUT_SECONDS,
            poll_interval_seconds=0.01,
            all_agents=[info],
            all_hosts={str(agent_id): local_host},
            launch_failures=[],
            stopper=stopper,
        )
    return recipe, info, output_dir, metadata


def test_mapper_whose_archive_cannot_be_extracted_is_reported_as_failed(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """An archive that exists but is not a readable tarball is a mapper failure.

    Without an error summary the reducer gate counts this mapper as a success
    and launches the reducer on inputs that were never extracted.
    """
    recipe, info, _, metadata = _poll_one_mapper_that_published(
        temp_mngr_ctx, local_provider, tmp_path, archive_bytes=b"this is not a gzipped tarball"
    )

    assert [meta.error_summary for meta in metadata] == [_ARCHIVE_EXTRACTION_FAILED_SUMMARY]
    assert [meta.task_id for meta in metadata] == [info.task_id]
    assert recipe.finalized_mapper_dirs == []


def test_mid_poll_report_shows_a_mapper_whose_archive_cannot_be_extracted_as_failed(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """The renders during polling carry the same failure the final metadata does."""
    recipe, _, _, _ = _poll_one_mapper_that_published(
        temp_mngr_ctx, local_provider, tmp_path, archive_bytes=b"this is not a gzipped tarball"
    )

    assert [meta.error_summary for meta in recipe.rendered_agents[-1]] == [_ARCHIVE_EXTRACTION_FAILED_SUMMARY]


def test_mapper_whose_archive_is_extracted_is_reported_as_succeeded(
    temp_mngr_ctx: MngrContext,
    local_provider: LocalProviderInstance,
    tmp_path: Path,
) -> None:
    """The happy path: the archive is extracted, the recipe hook sees its directory."""
    archive_bytes = _make_tarball_bytes("outcome.json", b'{"status": "ok"}')

    recipe, info, output_dir, metadata = _poll_one_mapper_that_published(
        temp_mngr_ctx, local_provider, tmp_path, archive_bytes=archive_bytes
    )

    assert [meta.error_summary for meta in metadata] == [None]
    agent_dir = output_dir / str(info.agent_name)
    assert recipe.finalized_mapper_dirs == [agent_dir]
    assert (agent_dir / "outcome.json").read_bytes() == b'{"status": "ok"}'
