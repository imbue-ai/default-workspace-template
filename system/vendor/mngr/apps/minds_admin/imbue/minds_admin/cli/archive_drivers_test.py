import json
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import cast
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import Field

from imbue.imbue_common.mutable_model import MutableModel
from imbue.minds_admin.cli.archive_drivers import ArchiveContext
from imbue.minds_admin.cli.archive_drivers import ArchiveLink
from imbue.minds_admin.cli.archive_drivers import _read_archive_source
from imbue.minds_admin.cli.archive_drivers import _stream_archive_to_bucket
from imbue.minds_admin.cli.archive_drivers import latest_manifest_by_workspace
from imbue.minds_admin.cli.archive_drivers import list_bucket_archives
from imbue.minds_admin.cli.archive_drivers import presigned_archive_url
from imbue.minds_admin.cli.archive_drivers import render_candidates_table
from imbue.minds_admin.cli.archive_drivers import render_links_csv
from imbue.minds_admin.cli.archive_drivers import render_links_markdown
from imbue.minds_admin.cli.archive_drivers import render_manifests_table
from imbue.minds_admin.cli.archive_drivers import zip_object_removed_unless_completed
from imbue.minds_admin.cli.cutover_drivers import minimal_mngr_context
from imbue.minds_admin.cli.testing import make_workspace_storage_config
from imbue.minds_admin.slices.archive_state import ArchiveStateStore
from imbue.minds_admin.slices.archive_types import ARCHIVE_BYTES_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SHA256_MARKER
from imbue.minds_admin.slices.archive_types import ARCHIVE_SUMMARY_MARKER
from imbue.minds_admin.slices.archive_types import ArchiveCandidate
from imbue.minds_admin.slices.archive_types import ArchiveError
from imbue.minds_admin.slices.archive_types import RetireVerdict
from imbue.minds_admin.slices.cutover_db import CutoverPoolRow
from imbue.minds_admin.slices.testing import make_gen1_pool_row
from imbue.minds_admin.slices.testing import make_ready_gen1_server
from imbue.minds_admin.slices.testing import make_test_management_identities
from imbue.minds_admin.slices.testing import make_workspace_archive_manifest
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.host import OuterHostInterface
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr_imbue_cloud.slices.mock_slice_vm_client_test import MockSliceVmClient
from imbue.mngr_latchkey.remote.mock_outer_host_test import StubOuter

_HOST_ID = f"host-{uuid4().hex}"
_INSPECT = json.dumps([{"Name": "/minds-dev-x-slice", "Config": {}, "HostConfig": {}, "Mounts": []}])


class ScriptedOuter(StubOuter):
    """A VM root whose commands answer from a substring-keyed script (first match wins), recording every command."""

    answers_by_fragment: dict[str, CommandResult] = Field(default_factory=dict)

    def execute_idempotent_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
    ) -> CommandResult:
        for fragment, answer in self.answers_by_fragment.items():
            if fragment in command:
                super().execute_idempotent_command(command, user, cwd, env, timeout_seconds)
                return answer
        return super().execute_idempotent_command(command, user, cwd, env, timeout_seconds)


def _ok(stdout: str) -> CommandResult:
    return CommandResult(stdout=stdout, stderr="", success=True)


def _scripted_outer(overrides: Mapping[str, CommandResult] | None = None) -> ScriptedOuter:
    answers = {
        "docker ps -aq": _ok("c0ffee\n"),
        "docker inspect --format": _ok("/var/lib/docker/overlay2/abc/diff\n"),
        "docker inspect c0ffee": _ok(_INSPECT),
        "docker volume inspect": _ok("/mngr-btrfs/0123abcd\n"),
        "test -d": _ok(""),
        "elif [ -d /home/user ]": _ok("legacy\n"),
        "git -c safe.directory": _ok("minds-v0.3.9\n"),
    }
    if overrides:
        answers.update(overrides)
    return ScriptedOuter(answers_by_fragment=answers)


def _row() -> CutoverPoolRow:
    """A leased gen-1 row placed on a box (the shape the archive reads), baked below the floor."""
    return make_gen1_pool_row(
        status="leased",
        host_id=_HOST_ID,
        agent_id=f"agent-{uuid4().hex}",
        host_name="my-mind",
        vps_address="15.204.1.2",
        ssh_port=22010,
        container_ssh_port=22011,
        bare_metal_server_id=str(uuid4()),
        slice_instance_name="mngr-slice-dev-x-0123abcd",
        slice_disk_name="mngr-slice-dev-x-0123abcd-data",
        attributes={"repo_branch_or_tag": "minds-v0.3.9"},
        artifact_generation=0,
        region="US-EAST-VA",
    )


def test_read_archive_source_reads_the_container_its_layer_its_volume_and_its_version() -> None:
    outer = _scripted_outer()
    source = _read_archive_source(cast(OuterHostInterface, outer), _row(), volume_path_override=None)
    assert source.container_id == "c0ffee"
    assert source.container_name == "minds-dev-x-slice"
    assert source.container_layer_path == "/var/lib/docker/overlay2/abc/diff"
    assert source.volume_subvolume_path == "/mngr-btrfs/0123abcd"
    assert source.home_layout == "legacy"
    assert source.version_probe == "minds-v0.3.9"
    # The volume is looked up by the host's unified docker volume name.
    assert any(f"mngr-host-vol-{_HOST_ID.removeprefix('host-')}" in command for command in outer.recorded_commands())


def test_read_archive_source_takes_the_volume_override_and_refuses_relative_paths() -> None:
    outer = _scripted_outer()
    source = _read_archive_source(cast(OuterHostInterface, outer), _row(), volume_path_override="/mnt/data/sub")
    assert source.volume_subvolume_path == "/mnt/data/sub"
    assert not any("docker volume inspect" in command for command in outer.recorded_commands())
    relative = _scripted_outer({"docker volume inspect": _ok("relative/path\n")})
    with pytest.raises(ArchiveError, match="volume path is not absolute.*pass --volume-path"):
        _read_archive_source(cast(OuterHostInterface, relative), _row(), volume_path_override=None)
    # A container whose root is not an overlay mount has no upper dir; --volume-path cannot fix that, so the hint must not name it.
    no_upper_dir = _scripted_outer({"docker inspect --format": _ok("\n")})
    with pytest.raises(ArchiveError, match="container layer path is not absolute.*overlay mount") as excinfo:
        _read_archive_source(cast(OuterHostInterface, no_upper_dir), _row(), volume_path_override=None)
    assert "--volume-path" not in str(excinfo.value)


class RecordingSliceClient(MockSliceVmClient):
    """A box that records what it is asked to run and answers the archive script with the scripted result."""

    archive_result: tuple[int | None, str, str] = (0, "", "")
    calls: list[tuple[str, str]] = Field(default_factory=list)

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        self.calls.append((label, remote_command))
        if label.startswith("archive:"):
            return self.archive_result
        if label == "cache-keygen":
            return (0, "", "")
        if label == "cache-keycat":
            return (0, "ssh-ed25519 AAAATRANSFER transfer\n", "")
        return (0, "", "")


def _context(tmp_path: Path, mngr_ctx: Any) -> ArchiveContext:
    state = ArchiveStateStore(root=tmp_path / "archives")
    state.ensure_layout()
    return ArchiveContext(
        env_name="dev-x",
        dsn="not-a-dsn",
        identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
        mngr_ctx=mngr_ctx,
        storage=make_workspace_storage_config(),
        state=state,
    )


def test_stream_archive_stages_the_env_and_script_then_reads_the_markers(tmp_path: Path) -> None:
    sha = "cd" * 32
    summary = json.dumps({"entry_count": 2, "entry_bytes": 5, "skipped_count": 0, "skipped_paths": []})
    client = RecordingSliceClient(
        box_address="10.0.0.1",
        box_ssh_user="limaops",
        archive_result=(
            0,
            f"{ARCHIVE_SHA256_MARKER} {sha}\n{ARCHIVE_BYTES_MARKER} 4096\n",
            f"{ARCHIVE_SUMMARY_MARKER} {summary}\n",
        ),
    )
    outer = _scripted_outer()
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = _context(tmp_path, mngr_ctx)
        found_sha, size, stderr = _stream_archive_to_bucket(
            ctx,
            client,
            cast(OuterHostInterface, outer),
            make_ready_gen1_server(),
            _row(),
            vm_ssh_port=22010,
            vm_command="python3 /root/.mngr-archive/archive_stream_zip.py --root volume=/s",
            object_prefix="dev-x/archives/host-x/20260918T120000Z",
        )
    assert (found_sha, size) == (sha, 4096)
    assert ARCHIVE_SUMMARY_MARKER in stderr
    labels = [label for label, _ in client.calls]
    assert labels[:2] == ["cache-keygen", "cache-keycat"]
    assert "write-env" in labels and "write-archive-script" in labels
    assert any(label.startswith("archive:") for label in labels)
    # The transfer key is authorized on the VM for the run and removed again, and the transfer dir is dropped.
    commands = outer.recorded_commands()
    assert any("authorized_keys" in command and "AAAATRANSFER" in command for command in commands)
    assert any("grep -vF" in command for command in commands)
    assert labels[-1] == "rm-td"


def test_stream_archive_reports_a_failed_upload_with_its_stderr(tmp_path: Path) -> None:
    client = RecordingSliceClient(
        box_address="10.0.0.1", box_ssh_user="limaops", archive_result=(1, "", "s5cmd: boom\n")
    )
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = _context(tmp_path, mngr_ctx)
        with pytest.raises(ArchiveError, match="s5cmd: boom"):
            _stream_archive_to_bucket(
                ctx,
                client,
                cast(OuterHostInterface, _scripted_outer()),
                make_ready_gen1_server(),
                _row(),
                vm_ssh_port=22010,
                vm_command="python3 x",
                object_prefix="dev-x/archives/host-x/20260918T120000Z",
            )
    # The transfer dir is removed on the failure path too.
    assert client.calls[-1][0] == "rm-td"


class FakeS3Client(MutableModel):
    """A fake boto3 S3 client over an in-memory object map."""

    objects: dict[str, bytes] = Field(default_factory=dict)
    presign_calls: list[dict[str, Any]] = Field(default_factory=list)
    deleted_keys: list[str] = Field(default_factory=list)

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)
        self.deleted_keys.append(Key)

    def get_paginator(self, name: str) -> "FakeS3Client":
        assert name == "list_objects_v2"
        return self

    def paginate(self, *, Bucket: str, Prefix: str) -> list[dict[str, Any]]:
        return [{"Contents": [{"Key": key} for key in sorted(self.objects) if key.startswith(Prefix)]}]

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        class _Body:
            def __init__(self, content: bytes) -> None:
                self._content = content

            def read(self) -> bytes:
                return self._content

        return {"Body": _Body(self.objects[Key])}

    def generate_presigned_url(self, operation: str, *, Params: dict[str, Any], ExpiresIn: int) -> str:
        self.presign_calls.append({"operation": operation, "Params": Params, "ExpiresIn": ExpiresIn})
        return f"https://s3.example/{Params['Key']}?expires={ExpiresIn}"


def test_list_bucket_archives_reads_only_the_manifests_and_orders_them() -> None:
    older = make_workspace_archive_manifest(
        str(uuid4()), created_at=datetime(2026, 9, 1, tzinfo=timezone.utc), host_id="host-b", host_name="bee"
    )
    newer = make_workspace_archive_manifest(
        older.host_db_id, created_at=older.created_at + timedelta(days=1), host_id="host-b", host_name="bee"
    )
    other = make_workspace_archive_manifest(
        str(uuid4()), created_at=older.created_at, host_id="host-a", host_name="ay"
    )
    fake = FakeS3Client(
        objects={manifest.manifest_key: manifest.model_dump_json().encode() for manifest in (older, newer, other)}
        | {older.zip_key: b"zip", "dev-x/host-a/gen-1/DISK": b"artifact"}
    )
    assert list_bucket_archives(fake, make_workspace_storage_config(), None) == [other, older, newer]
    assert list_bucket_archives(fake, make_workspace_storage_config(), "host-b") == [older, newer]
    assert latest_manifest_by_workspace([older, newer, other]) == [other, newer]


def test_presigned_archive_url_caps_the_lifetime_at_seven_days() -> None:
    fake = FakeS3Client()
    manifest = make_workspace_archive_manifest(str(uuid4()), created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    url = presigned_archive_url(fake, manifest, expires_seconds=3600)
    assert url.endswith("?expires=3600")
    assert fake.presign_calls[0]["Params"]["Key"] == manifest.zip_key
    assert "my-mind-archive.zip" in fake.presign_calls[0]["Params"]["ResponseContentDisposition"]
    with pytest.raises(ArchiveError, match="presigned link lasts"):
        presigned_archive_url(fake, manifest, expires_seconds=8 * 24 * 3600)


def test_links_document_names_the_owner_size_checksum_and_link() -> None:
    manifest = make_workspace_archive_manifest(str(uuid4()), created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    expires_at = datetime(2026, 9, 8, tzinfo=timezone.utc)
    link = ArchiveLink(manifest=manifest, url="https://s3.example/zip", expires_at=expires_at)
    markdown = render_links_markdown([link], env_name="production")
    assert "alice@example.com" in markdown and "my-mind" in markdown and "4.0 KB" in markdown
    assert manifest.zip_sha256 in markdown and "https://s3.example/zip" in markdown
    csv_text = render_links_csv([link])
    assert csv_text.splitlines()[0].startswith("owner_email,workspace_name,host_db_id")
    assert "alice@example.com,my-mind" in csv_text
    table = render_manifests_table([manifest])
    assert "minds-v0.3.9" in table and f"s3://bucket/{manifest.zip_key}" in table


def test_candidates_table_counts_the_verdicts() -> None:
    candidates = [
        ArchiveCandidate(
            host_db_id=str(uuid4()),
            host_id="host-a",
            host_name="a",
            leased_to_user="0123456789abcdef",
            status="leased",
            baked_version="minds-v0.3.9",
            live_version="minds-v0.3.9",
            verdict=RetireVerdict.RETIRE,
            reason="below the floor",
        ),
        ArchiveCandidate(
            host_db_id=str(uuid4()),
            host_id="host-b",
            host_name="b",
            leased_to_user=None,
            status="stopped",
            baked_version=None,
            live_version=None,
            verdict=RetireVerdict.UNKNOWN_UNTIL_STARTED,
            reason="start it",
        ),
    ]
    table = render_candidates_table(candidates)
    assert "retire 1; unknown until started 1; total 2" in table
    assert "RETIRE" in table and "UNKNOWN_UNTIL_STARTED" in table


def test_client_error_while_listing_becomes_an_archive_error() -> None:
    class _FailingClient:
        def get_paginator(self, name: str) -> "_FailingClient":
            return self

        def paginate(self, **kwargs: Any) -> list[dict[str, Any]]:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "ListObjectsV2")

    with pytest.raises(ArchiveError, match="could not list"):
        list_bucket_archives(_FailingClient(), make_workspace_storage_config(), None)


def test_a_failed_create_removes_the_zip_it_landed_and_a_completed_one_keeps_it() -> None:
    zip_key = "dev-x/archives/host-d/20260918T120000Z/workspace.zip"
    fake = FakeS3Client(objects={zip_key: b"truncated"})
    with pytest.raises(ArchiveError, match="stream died"):
        with zip_object_removed_unless_completed(fake, "bucket", zip_key):
            raise ArchiveError("stream died")
    assert fake.deleted_keys == [zip_key]
    assert zip_key not in fake.objects
    kept = FakeS3Client(objects={zip_key: b"whole"})
    with zip_object_removed_unless_completed(kept, "bucket", zip_key):
        pass
    assert kept.deleted_keys == []

    # A failed removal is logged, never raised: the run's own error is the one that surfaces.
    class _UndeletableClient:
        def delete_object(self, *, Bucket: str, Key: str) -> None:
            raise ClientError({"Error": {"Code": "AccessDenied"}}, "DeleteObject")

    with capture_loguru(level="WARNING") as log_output:
        with pytest.raises(ArchiveError, match="stream died"):
            with zip_object_removed_unless_completed(_UndeletableClient(), "bucket", zip_key):
                raise ArchiveError("stream died")
    assert "Could not remove the zip" in log_output.getvalue()


def test_a_manifest_this_build_cannot_read_becomes_an_archive_error_naming_it() -> None:
    key = "dev-x/archives/host-c/20260918T120000Z/manifest.json"
    fake = FakeS3Client(objects={key: b"{}"})
    with pytest.raises(ArchiveError, match=f"s3://bucket/{key} is not one this build can read"):
        list_bucket_archives(fake, make_workspace_storage_config(), None)
