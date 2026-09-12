import base64
import os
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroupState
from imbue.concurrency_group.errors import ProcessTimeoutError
from imbue.concurrency_group.executor import ConcurrencyGroupExecutor
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.secret_wrapping import wrap_dek
from imbue.minds_admin.cli._tier_secrets import WorkspaceStorageConfig
from imbue.minds_admin.cli.cutover_drivers import CutoverContext
from imbue.minds_admin.cli.cutover_drivers import _S3_COPY_PART_BYTES
from imbue.minds_admin.cli.cutover_drivers import _migrate_workspace
from imbue.minds_admin.cli.cutover_drivers import _remove_box_transfer_dirs
from imbue.minds_admin.cli.cutover_drivers import _render_migrate_reserve_script
from imbue.minds_admin.cli.cutover_drivers import _repave_box
from imbue.minds_admin.cli.cutover_drivers import _rollback_workspace
from imbue.minds_admin.cli.cutover_drivers import _run_on_box_checked
from imbue.minds_admin.cli.cutover_drivers import _s3_multipart_copy
from imbue.minds_admin.cli.cutover_drivers import _target_capacity_error_or_none
from imbue.minds_admin.cli.cutover_drivers import box_transfer_dir
from imbue.minds_admin.cli.cutover_drivers import build_preflight_report
from imbue.minds_admin.cli.cutover_drivers import build_saved_product_artifact
from imbue.minds_admin.cli.cutover_drivers import is_artifact_resave_due
from imbue.minds_admin.cli.cutover_drivers import is_parked_row_shape
from imbue.minds_admin.cli.cutover_drivers import minimal_mngr_context
from imbue.minds_admin.cli.cutover_drivers import partition_migration_rows
from imbue.minds_admin.cli.cutover_drivers import render_preflight_table
from imbue.minds_admin.cli.cutover_drivers import render_stage_table
from imbue.minds_admin.cli.cutover_drivers import repave_dry_run_detail
from imbue.minds_admin.cli.cutover_drivers import repave_pre_reinstall_server_fields
from imbue.minds_admin.cli.cutover_drivers import repave_scope_refusal_or_none
from imbue.minds_admin.cli.cutover_drivers import require_named_servers_selected
from imbue.minds_admin.cli.cutover_drivers import rollback_would_clobber_newer_artifact
from imbue.minds_admin.cli.cutover_drivers import s3_copy_part_ranges
from imbue.minds_admin.cli.cutover_drivers import target_box_refusal_or_none
from imbue.minds_admin.cli.cutover_drivers import undestroyed_pool_host_ids
from imbue.minds_admin.cli.cutover_drivers import unreachable_vm_error
from imbue.minds_admin.cli.cutover_drivers import unwrap_age_identity
from imbue.minds_admin.slices.cutover_db import CutoverPoolRow
from imbue.minds_admin.slices.cutover_state import CutoverStateStore
from imbue.minds_admin.slices.cutover_types import BoxOutcome
from imbue.minds_admin.slices.cutover_types import BoxPreflight
from imbue.minds_admin.slices.cutover_types import CutoverBoxStage
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.cutover_types import RowVerdict
from imbue.minds_admin.slices.cutover_types import StageReport
from imbue.minds_admin.slices.cutover_types import WorkspaceOutcome
from imbue.minds_admin.slices.cutover_types import WorkspacePreflight
from imbue.minds_admin.slices.testing import make_cutover_workspace_state
from imbue.minds_admin.slices.testing import make_harvested_keys
from imbue.minds_admin.slices.testing import make_saved_product_artifact
from imbue.minds_admin.slices.testing import make_test_management_identities
from imbue.mngr_imbue_cloud.data_types import BareMetalServer
from imbue.mngr_imbue_cloud.data_types import PoolHostDestroyOutcome
from imbue.mngr_imbue_cloud.primitives import BareMetalServerDbId
from imbue.mngr_imbue_cloud.primitives import BareMetalServerStatus
from imbue.mngr_imbue_cloud.primitives import PoolHostDestroyOutcomeStatus
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DELIVERED
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_DRAINING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_INSTALLING
from imbue.mngr_imbue_cloud.primitives import SERVER_STATUS_READY
from imbue.mngr_imbue_cloud.slices.gen2_scripts.box_commands import build_qemu_slice_env_file
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_meta_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_network_config
from imbue.mngr_imbue_cloud.slices.gen2_scripts.guest import build_qemu_slice_user_data
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_CONTAINER_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_SLICE_SERVICE_USER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.layout import GEN2_VM_SSH_PORT_PLACEHOLDER
from imbue.mngr_imbue_cloud.slices.gen2_scripts.testing import assert_valid_bash
from imbue.mngr_imbue_cloud.slices.mock_slice_vm_client_test import MockSliceVmClient

_TEST_SSH_CA = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFAKECAFAKECAFAKECAFAKECAFAKECAFAKECAFAKE minds-dev-ssh-ca"
_TEST_POOL_PUBLIC_KEY = "ssh-ed25519 AAAAPOOL pool"


class ScriptedSliceVmClient(MockSliceVmClient):
    """A box whose ``run_on_box`` answers from a per-label script; each label's answers are consumed in order."""

    answers_by_label: dict[str, list[tuple[int, str, str]]] = Field(default_factory=dict)
    calls: list[tuple[str, str]] = Field(default_factory=list)

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        self.calls.append((label, remote_command))
        answers = self.answers_by_label[label]
        return answers.pop(0) if len(answers) > 1 else answers[0]


class TimingOutSliceVmClient(MockSliceVmClient):
    """A box whose every command times out (``run_on_box`` raises instead of returning)."""

    def run_on_box(
        self, remote_command: str, *, timeout: float, label: str, is_streaming: bool = False
    ) -> tuple[int | None, str, str]:
        raise ProcessTimeoutError(("ssh", "box"), "", "")


def _client(answers_by_label: dict[str, list[tuple[int, str, str]]]) -> ScriptedSliceVmClient:
    return ScriptedSliceVmClient(box_address="10.0.0.1", box_ssh_user="slicehost", answers_by_label=answers_by_label)


def _storage() -> WorkspaceStorageConfig:
    return WorkspaceStorageConfig(
        s3_endpoint="https://s3.example",
        s3_region="us-east-1",
        access_key_id=SecretStr("AKIA"),
        secret_access_key=SecretStr("secret"),
        bucket="bucket",
        kek_base64=SecretStr(base64.b64encode(os.urandom(32)).decode()),
        key_prefix="dev-x/",
    )


def test_minimal_mngr_context_enters_its_concurrency_group_for_the_block(tmp_path: Path) -> None:
    # The snapshot-helper provisioning runs its steps in child groups of the
    # context's group, which make_concurrency_group refuses on an unentered parent.
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        assert mngr_ctx.concurrency_group.state == ConcurrencyGroupState.ACTIVE
        with ConcurrencyGroupExecutor(parent_cg=mngr_ctx.concurrency_group, name="probe", max_workers=1) as executor:
            future = executor.submit(len, "abc")
        assert future.result() == 3
    assert mngr_ctx.concurrency_group.state == ConcurrencyGroupState.EXITED


def test_box_transfer_dir_is_absolute_under_the_dialed_users_home() -> None:
    # The transfer scripts find the dir through $HOME, so it follows the user the client dials as.
    assert (
        box_transfer_dir(GEN2_SLICE_SERVICE_USER, "mngr-slice-abc")
        == f"/home/{GEN2_SLICE_SERVICE_USER}/.mngr-transfers/mngr-slice-abc"
    )
    assert box_transfer_dir("limaops", "mngr-slice-abc") == "/home/limaops/.mngr-transfers/mngr-slice-abc"


def test_unwrap_age_identity_reads_the_connector_envelope() -> None:
    # The saved artifact's DEK is whatever the product stop recorded on the
    # row; the migrate must unwrap it with the tier KEK exactly as the
    # connector's own restore would.
    storage = _storage()
    identity = f"AGE-SECRET-KEY-1{uuid4().hex.upper()}"
    kek = base64.b64decode(storage.kek_base64.get_secret_value())
    wrapped = base64.b64encode(wrap_dek(kek, identity.encode("utf-8"))).decode("ascii")
    assert unwrap_age_identity(storage, wrapped) == identity


def test_s3_copy_part_ranges_cover_the_object_exactly() -> None:
    assert s3_copy_part_ranges(10, 4) == [(0, 3), (4, 7), (8, 9)]
    assert s3_copy_part_ranges(8, 4) == [(0, 3), (4, 7)]
    assert s3_copy_part_ranges(3, 4) == [(0, 2)]


def test_s3_copy_part_ranges_refuse_empty_objects_and_parts() -> None:
    with pytest.raises(CutoverError, match="copy parts"):
        s3_copy_part_ranges(0, 4)
    with pytest.raises(CutoverError, match="copy parts"):
        s3_copy_part_ranges(4, 0)


class RecordingS3Client(MutableModel):
    """A fake boto3 S3 client recording multipart-copy calls; ``failing_part_number`` makes that part copy raise."""

    failing_part_number: int | None = None
    calls: list[tuple[str, dict[str, Any]]] = Field(default_factory=list)

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_multipart_upload", kwargs))
        return {"UploadId": "upload-1"}

    def upload_part_copy(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upload_part_copy", kwargs))
        if kwargs["PartNumber"] == self.failing_part_number:
            raise ClientError({"Error": {"Code": "InternalError"}}, "UploadPartCopy")
        return {"CopyPartResult": {"ETag": f'"etag-{kwargs["PartNumber"]}"'}}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("complete_multipart_upload", kwargs))
        return {}

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("abort_multipart_upload", kwargs))
        return {}


def test_s3_multipart_copy_copies_ranged_parts_without_etag_preconditions() -> None:
    client = RecordingS3Client()
    size = _S3_COPY_PART_BYTES * 2 + 5
    _s3_multipart_copy(client, "bucket", source_key="src", dest_key="dst", size=size)
    assert [name for name, _ in client.calls] == [
        "create_multipart_upload",
        "upload_part_copy",
        "upload_part_copy",
        "upload_part_copy",
        "complete_multipart_upload",
    ]
    part_calls = [kwargs for name, kwargs in client.calls if name == "upload_part_copy"]
    # OVH's gateway answers 412 to CopySourceIfMatch on multipart-uploaded sources, so no call may pin ETags.
    assert all("CopySourceIfMatch" not in kwargs for kwargs in part_calls)
    assert [kwargs["PartNumber"] for kwargs in part_calls] == [1, 2, 3]
    assert part_calls[0]["CopySource"] == {"Bucket": "bucket", "Key": "src"}
    assert part_calls[-1]["CopySourceRange"] == f"bytes={_S3_COPY_PART_BYTES * 2}-{size - 1}"
    assert client.calls[-1][1]["MultipartUpload"] == {
        "Parts": [
            {"ETag": '"etag-1"', "PartNumber": 1},
            {"ETag": '"etag-2"', "PartNumber": 2},
            {"ETag": '"etag-3"', "PartNumber": 3},
        ]
    }


def test_s3_multipart_copy_aborts_the_upload_when_a_part_copy_fails() -> None:
    client = RecordingS3Client(failing_part_number=2)
    with pytest.raises(ClientError):
        _s3_multipart_copy(client, "bucket", source_key="src", dest_key="dst", size=_S3_COPY_PART_BYTES * 2)
    assert client.calls[-1] == ("abort_multipart_upload", {"Bucket": "bucket", "Key": "dst", "UploadId": "upload-1"})


def test_unreachable_vm_error_names_the_start_remedy_only_for_stopped_rows() -> None:
    row_id = uuid4().hex
    stopped = unreachable_vm_error("1.2.3.4", 2222, "stopped", row_id)
    assert f"minds-admin workspaces start {row_id}" in stopped
    assert "1.2.3.4:2222" in stopped
    assert "workspaces start" not in unreachable_vm_error("1.2.3.4", 2222, "leased", row_id)


def test_run_on_box_checked_raises_with_stderr_on_failure() -> None:
    client = _client(answers_by_label={"probe": [(1, "", "boom\n")]})
    with pytest.raises(CutoverError, match="boom"):
        _run_on_box_checked(client, "false", timeout=1.0, label="probe")
    ok = _client(answers_by_label={"probe": [(0, "fine", "")]})
    assert _run_on_box_checked(ok, "true", timeout=1.0, label="probe") == "fine"


def test_remove_box_transfer_dirs_is_best_effort_and_never_raises() -> None:
    # It cleans up on failure paths, where a raise would mask the original error.
    timed_out = TimingOutSliceVmClient(box_address="10.0.0.1", box_ssh_user="slicehost")
    _remove_box_transfer_dirs(timed_out, ("/home/slicehost/.mngr-transfers/x",), what="a timed-out box")
    failing = _client(answers_by_label={"rm-td": [(1, "", "rm: cannot remove\n")]})
    _remove_box_transfer_dirs(failing, ("/home/slicehost/.mngr-transfers/x",), what="a failing rm")
    assert failing.calls == [("rm-td", "rm -rf /home/slicehost/.mngr-transfers/x")]


def test_build_saved_product_artifact_rewrites_the_key_prefix_onto_the_rollback_copy() -> None:
    manifest = {
        "generation": 5,
        "key_prefix": "dev-x/host-abc/gen-5",
        "age_recipient": "age1xyz",
        "object_by_name": {
            "DISK": {"sha256": "aa" * 32, "size_bytes": 1},
            "DATADISK": {"sha256": "bb" * 32, "size_bytes": 2},
            "META": {"sha256": "cc" * 32, "size_bytes": 3},
        },
    }
    saved = build_saved_product_artifact(
        manifest, "wrapped", rollback_key_prefix="dev-x/cutover/host-abc/rollback", fallback_generation=1
    )
    assert saved.generation == 5
    assert saved.key_prefix == "dev-x/cutover/host-abc/rollback"
    assert saved.age_recipient == "age1xyz"
    assert saved.datadisk_sha256 == "bb" * 32
    assert saved.wrapped_dek == "wrapped"
    # The manifest is the row's, verbatim, except the prefix now names the copy
    # (the rollback flip writes it back onto the row so the product restore
    # downloads the copy).
    assert saved.manifest_json["key_prefix"] == "dev-x/cutover/host-abc/rollback"
    assert saved.manifest_json["object_by_name"] == manifest["object_by_name"]
    # The source manifest itself is not mutated.
    assert manifest["key_prefix"] == "dev-x/host-abc/gen-5"


def test_build_saved_product_artifact_refuses_a_manifest_missing_its_coordinates() -> None:
    with pytest.raises(CutoverError, match="lacks"):
        build_saved_product_artifact(
            {"generation": 5, "key_prefix": "p", "age_recipient": "r", "object_by_name": {}},
            "wrapped",
            rollback_key_prefix="rp",
            fallback_generation=1,
        )


def _workspace(**overrides: object) -> WorkspacePreflight:
    fields: dict[str, object] = dict(
        host_db_id=uuid4().hex,
        host_id=f"host-{uuid4().hex}",
        host_name="ws-a",
        leased_to_user="0123456789abcdef",
        status="leased",
        verdict=RowVerdict.CANDIDATE,
        version_tag="minds-v0.4.1",
        is_version_ok=True,
        disk_gb=44,
    )
    fields.update(overrides)
    return WorkspacePreflight.model_validate(fields)


def test_build_preflight_report_counts_refusals_and_errors() -> None:
    boxes = [
        BoxPreflight(
            server_id="a",
            public_address="1.1.1.1",
            status="ready",
            workspaces=(
                _workspace(),
                _workspace(version_tag="minds-v0.3.10"),
                _workspace(status="crashed", verdict=RowVerdict.REFUSED, remedy="destroy it", version_tag=None),
            ),
        ),
        BoxPreflight(
            server_id="b",
            public_address="2.2.2.2",
            status="ready",
            workspaces=(_workspace(error="VM probe failed"),),
        ),
        BoxPreflight(server_id="c", public_address="3.3.3.3", status="ready", workspaces=(), error="box unreachable"),
    ]
    # A finalized-stopped row is on no box; the migrate admin-starts it first.
    finalized = _workspace(
        status="stopped", verdict=RowVerdict.CANDIDATE, remedy="stopped on no box", version_tag=None
    )
    report = build_preflight_report("dev-x", boxes, [finalized])
    assert report.distinct_version_tags == ("minds-v0.3.10", "minds-v0.4.1")
    assert report.unplaced_workspaces == (finalized,)
    assert report.refused_count == 1
    assert report.error_count == 2
    assert not report.is_clean
    text = render_preflight_table(report)
    assert "box unreachable" in text
    assert "destroy it" in text
    assert "(none)" in text and "stopped on no box" in text
    assert "NOT CLEAN" in text


def test_render_stage_table_lists_boxes_then_their_workspaces() -> None:
    report = StageReport(
        stage_name="migrate",
        env_name="dev-x",
        is_dry_run=True,
        boxes=(
            BoxOutcome(
                server_id="abcdef0123456789",
                stage=CutoverBoxStage.FINISHED,
                workspaces=(WorkspaceOutcome(host_db_id="1", host_id="host-1", stage=CutoverStage.RESTORED),),
                detail="dry run",
            ),
            BoxOutcome(server_id="fedcba9876543210", stage=None, detail="refused rows"),
        ),
    )
    text = render_stage_table(report)
    lines = text.splitlines()
    assert lines[0].startswith("DRY RUN -- migrate (dev-x): 1 failed")
    assert "abcdef01" in text and "host-1" in text and "RESTORED" in text
    assert "FAILED" in text and "refused rows" in text


def _ready_gen1_server() -> BareMetalServer:
    now = datetime(2026, 8, 28, tzinfo=timezone.utc)
    return BareMetalServer(
        id=BareMetalServerDbId(str(uuid4())),
        plan_code="24rise02-v1-us",
        region="vin",
        public_address="15.204.1.2",
        cpu_threads=32,
        ram_gb=128,
        disk_gb=1000,
        memory_per_slice_gb=8,
        cpu_overcommit_ratio=1.5,
        slot_count=15,
        status=BareMetalServerStatus(SERVER_STATUS_READY),
        created_at=now,
        updated_at=now,
        uplink_mbps=1000,
    )


def _ready_gen2_server() -> BareMetalServer:
    gen1 = _ready_gen1_server()
    return gen1.model_copy_update(
        to_update(gen1.field_ref().box_generation, 2),
        to_update(gen1.field_ref().disk_gb, 879),
        to_update(gen1.field_ref().cpu_overcommit_ratio, 4.0),
    )


def _gen2_server_with_status(status: str) -> BareMetalServer:
    gen2 = _ready_gen2_server()
    return gen2.model_copy_update(to_update(gen2.field_ref().status, BareMetalServerStatus(status)))


def test_require_named_servers_selected_refuses_named_boxes_the_scope_dropped() -> None:
    kept = _ready_gen1_server()
    dropped_id = str(uuid4())
    # Unscoped: whatever the filter kept is the batch.
    require_named_servers_selected([], [kept], reason="unused")
    require_named_servers_selected([str(kept.id)], [kept, None], reason="unused")
    with pytest.raises(CutoverError, match=f"--server-id {dropped_id}: not gen-1"):
        require_named_servers_selected([str(kept.id), dropped_id], [kept], reason="not gen-1")


def test_target_box_must_be_a_ready_gen2_box() -> None:
    gen1_ready = _ready_gen1_server()
    assert target_box_refusal_or_none(None) == "no bare_metal_servers row with a public address"
    refusal = target_box_refusal_or_none(gen1_ready)
    assert refusal is not None and "gen-2" in refusal
    gen2_installing = _ready_gen2_server().model_copy_update(
        to_update(_ready_gen2_server().field_ref().status, BareMetalServerStatus(SERVER_STATUS_INSTALLING))
    )
    refusal = target_box_refusal_or_none(gen2_installing)
    assert refusal is not None and "expected ready" in refusal
    assert target_box_refusal_or_none(_ready_gen2_server()) is None


def test_target_capacity_check_refuses_a_full_box_before_the_stop(tmp_path: Path) -> None:
    # The estimate mirrors the bake's pre-check: existing occupancy (disk
    # names) at the default machine size against the box's two budgets. The
    # reserve script stays the authoritative guard.
    target = _ready_gen2_server()
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = CutoverContext(
            env_name="dev-x",
            dsn="not-a-dsn",
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
            identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
            ssh_ca_public_key=_TEST_SSH_CA,
            storage=_storage(),
            state=CutoverStateStore(root=tmp_path / "cutover"),
            mngr_ctx=mngr_ctx,
        )
        roomy = _client(answers_by_label={})
        roomy.disk_names = {f"mngr-slice-dev-x-{index:032x}-data" for index in range(3)}
        assert _target_capacity_error_or_none(ctx, target, roomy) is None
        full = _client(answers_by_label={})
        full.disk_names = {f"mngr-slice-dev-x-{index:032x}-data" for index in range(50)}
        error = _target_capacity_error_or_none(ctx, target, full)
        assert error is not None and "in use" in error
        # A target whose row lacks sizing inputs refuses with the same soft
        # error (not a raw provisioning failure), still before any stop.
        unsized = target.model_copy_update(to_update(target.field_ref().cpu_threads, None))
        unsized_error = _target_capacity_error_or_none(ctx, unsized, roomy)
        assert unsized_error is not None and "sizing is unusable" in unsized_error


def test_repave_scope_refuses_boxes_that_are_not_steady_gen1() -> None:
    assert repave_scope_refusal_or_none(_ready_gen1_server()) is None
    installing = _ready_gen1_server().model_copy_update(
        to_update(_ready_gen1_server().field_ref().status, BareMetalServerStatus(SERVER_STATUS_INSTALLING))
    )
    refusal = repave_scope_refusal_or_none(installing)
    assert refusal is not None and "only a ready (or draining) gen-1 box repaves" in refusal
    # A gen-2 box passes scope: already-ready is skipped and mid-install is
    # resumed by the unguarded body, not refused here.
    assert repave_scope_refusal_or_none(_ready_gen2_server()) is None


def test_repave_flips_a_drained_gen2_box_to_delivered_so_setup_reinstalls_it() -> None:
    """A gen-2 box prepped before storage encryption is drained and repaved; setup only
    reinstalls from ``delivered``, so the repave must move it off ``draining``."""
    assert repave_pre_reinstall_server_fields(_gen2_server_with_status(SERVER_STATUS_DRAINING)) == {
        "status": SERVER_STATUS_DELIVERED
    }
    # A crashed gen-2 repave resumes from where setup left it, untouched.
    for resumable_status in (SERVER_STATUS_DELIVERED, SERVER_STATUS_INSTALLING):
        assert repave_pre_reinstall_server_fields(_gen2_server_with_status(resumable_status)) == {}
    assert repave_pre_reinstall_server_fields(_ready_gen1_server()) == {
        "box_generation": 2,
        "status": SERVER_STATUS_DELIVERED,
        "cpu_overcommit_ratio": 4.0,
    }


def test_repave_dry_run_detail_names_the_columns_the_real_run_would_set() -> None:
    assert repave_dry_run_detail(_ready_gen1_server()) == (
        "dry run: would set box_generation=2, status=delivered, cpu_overcommit_ratio=4.0, "
        "reinstall + prep, measure the partition"
    )
    assert repave_dry_run_detail(_gen2_server_with_status(SERVER_STATUS_DRAINING)) == (
        "dry run: would set status=delivered, reinstall + prep, measure the partition"
    )
    assert repave_dry_run_detail(_gen2_server_with_status(SERVER_STATUS_DELIVERED)) == (
        "dry run: no row change, reinstall + prep, measure the partition"
    )
    assert repave_dry_run_detail(_gen2_server_with_status(SERVER_STATUS_INSTALLING)) == (
        "dry run: no row change, resume the prep, measure the partition"
    )


def test_repave_refuses_a_box_with_in_flight_migrations(tmp_path: Path) -> None:
    gen1 = _ready_gen1_server()
    state_store = CutoverStateStore(root=tmp_path / "cutover")
    state_store.ensure_layout()
    # A migration mid-flight off this box: the repave must not pull the origin
    # (or target) box out from under it. The in-flight check runs before the
    # pool-row fetch, so the invalid DSN proves the DB is never even reached.
    in_flight = make_cutover_workspace_state(uuid4().hex, str(gen1.id))
    state_store.write_workspace(in_flight)
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = CutoverContext(
            env_name="dev-x",
            dsn="not-a-dsn",
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
            identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
            ssh_ca_public_key=_TEST_SSH_CA,
            storage=_storage(),
            state=state_store,
            mngr_ctx=mngr_ctx,
        )
        outcome = _repave_box(ctx, gen1, is_dry_run=False)
    assert outcome.stage is None
    assert outcome.detail is not None and "in-flight migrations reference this box" in outcome.detail
    assert in_flight.host_db_id in outcome.detail


def test_box_workers_report_a_failed_box_level_step_instead_of_raising(tmp_path: Path) -> None:
    # The thread fan-out aborts the whole batch (daemon threads included) when a
    # worker raises, so a pool-DB failure on one box must become that box's own
    # failed outcome. An invalid DSN makes the first connection raise.
    gen1 = _ready_gen1_server()
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = CutoverContext(
            env_name="dev-x",
            dsn="not-a-dsn",
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
            identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
            ssh_ca_public_key=_TEST_SSH_CA,
            storage=_storage(),
            state=CutoverStateStore(root=tmp_path / "cutover"),
            mngr_ctx=mngr_ctx,
        )
        repave = _repave_box(ctx, gen1, is_dry_run=False)
    assert repave.stage is None
    assert repave.detail is not None and repave.detail.startswith("repave failed: invalid dsn")


def test_undestroyed_pool_host_ids_names_the_rows_a_destroy_left_behind() -> None:
    destroyed, gone, failed, leased = (str(uuid4()) for _ in range(4))
    outcomes = [
        PoolHostDestroyOutcome(pool_host_id=destroyed, status=PoolHostDestroyOutcomeStatus.DESTROYED),
        PoolHostDestroyOutcome(pool_host_id=gone, status=PoolHostDestroyOutcomeStatus.ALREADY_GONE),
        PoolHostDestroyOutcome(pool_host_id=failed, status=PoolHostDestroyOutcomeStatus.FAILED, detail="ssh"),
        PoolHostDestroyOutcome(pool_host_id=leased, status=PoolHostDestroyOutcomeStatus.SKIPPED_LEASED),
    ]
    assert undestroyed_pool_host_ids(outcomes) == [failed, leased]
    assert undestroyed_pool_host_ids([]) == []


def _gen1_row(**overrides: object) -> CutoverPoolRow:
    """A parked-shape gen-1 pool row (stopped, no placement, no artifact) unless overridden."""
    fields: dict[str, object] = dict(
        id=str(uuid4()),
        status="stopped",
        host_id=f"host-{uuid4().hex}",
        agent_id=None,
        host_name="slice-x",
        leased_to_user="0123456789abcdef",
        vps_address=None,
        ssh_port=None,
        container_ssh_port=None,
        bare_metal_server_id=None,
        slice_instance_name="mngr-slice-dev-x-" + "a" * 16,
        slice_disk_name="mngr-slice-dev-x-" + "a" * 16 + "-data",
        outer_host_public_key=None,
        container_host_public_key=None,
        box_generation=1,
        memory_units=8,
        disk_gb=44,
        attributes={},
        artifact_generation=3,
        region=None,
        artifact_manifest=None,
        wrapped_dek=None,
    )
    fields.update(overrides)
    return CutoverPoolRow.model_validate(fields)


def test_is_artifact_resave_due_when_an_unparked_stopped_row_carries_a_newer_generation() -> None:
    # Between the save and the park the owner can start+stop the workspace;
    # the row's artifact generation then moves past the saved rollback copy,
    # and the migration must re-save rather than transplant the stale copy.
    saved = make_saved_product_artifact()
    state = make_cutover_workspace_state(uuid4().hex, str(uuid4()))
    state = state.model_copy_update(to_update(state.field_ref().saved_artifact, saved))
    manifest = {"generation": saved.generation + 1, "key_prefix": "p"}
    newer = _gen1_row(artifact_manifest=manifest, artifact_generation=saved.generation + 1)
    assert is_artifact_resave_due(state, newer)
    # The saved copy is current: no re-save.
    same = _gen1_row(artifact_manifest={"generation": saved.generation}, artifact_generation=saved.generation)
    assert not is_artifact_resave_due(state, same)
    # A parked row (manifest cleared) and a non-stopped row never re-save.
    assert not is_artifact_resave_due(state, _gen1_row(artifact_generation=saved.generation + 1))
    assert not is_artifact_resave_due(state, _gen1_row(status="leased", artifact_manifest=manifest))
    # Nothing saved yet: the plain save path covers it, not the re-save.
    unsaved = make_cutover_workspace_state(uuid4().hex, str(uuid4()))
    assert not is_artifact_resave_due(unsaved, newer)


def test_rollback_refuses_a_gen1_stop_whose_artifact_moved_past_the_saved_copy() -> None:
    # The owner ran (and stopped) the workspace between an interrupted
    # migrate's save and its park: the row's own artifact is the current disk,
    # and a rollback's park + artifact flip would clobber it with the stale
    # saved copy.
    saved = make_saved_product_artifact()
    manifest = {"generation": saved.generation + 1, "key_prefix": "p"}
    newer = _gen1_row(artifact_manifest=manifest, artifact_generation=saved.generation + 1)
    assert rollback_would_clobber_newer_artifact(saved, newer)
    # The crash-between-save-and-park shape with no owner interference: the
    # saved copy IS the row's artifact, so the park + flip is a benign repoint.
    same = _gen1_row(artifact_manifest={"generation": saved.generation}, artifact_generation=saved.generation)
    assert not rollback_would_clobber_newer_artifact(saved, same)
    # A parked row (manifest cleared) and a leased row are other branches' work.
    assert not rollback_would_clobber_newer_artifact(saved, _gen1_row(artifact_generation=saved.generation + 1))
    assert not rollback_would_clobber_newer_artifact(saved, _gen1_row(status="leased", artifact_manifest=manifest))
    # A finalized gen-2 stop is a completed migration: losing its
    # post-migration artifact is the rollback's stated policy, not a clobber.
    gen2_stop = _gen1_row(box_generation=2, artifact_manifest=manifest, artifact_generation=saved.generation + 1)
    assert not rollback_would_clobber_newer_artifact(saved, gen2_stop)


def test_is_parked_row_shape_requires_the_cleared_manifest_not_just_no_placement() -> None:
    # The connector's 409 guard fires only when the manifest is NULL too; a
    # retention-finalized stop (no placement, manifest kept) is an ordinary
    # startable row, so a resuming migrate must still park it.
    assert is_parked_row_shape(_gen1_row())
    finalized = _gen1_row(artifact_manifest={"generation": 3, "key_prefix": "p"}, wrapped_dek="d2VkZWs=")
    assert not is_parked_row_shape(finalized)
    assert not is_parked_row_shape(_gen1_row(bare_metal_server_id=str(uuid4())))
    assert not is_parked_row_shape(_gen1_row(vps_address="15.204.1.2"))
    assert not is_parked_row_shape(_gen1_row(status="leased"))


def test_partition_migration_rows_splits_candidates_from_unmigratable_rows() -> None:
    # Leased and stopped gen-1 rows migrate (in selection order); unleased rows
    # get the pool-destroy remedy, wedged rows their classification remedy, and
    # gen-2 rows fail unless the state dir says this tooling migrated them.
    leased = _gen1_row(status="leased", bare_metal_server_id=str(uuid4()), vps_address="15.204.1.2")
    stopped = _gen1_row()
    available = _gen1_row(status="available", bare_metal_server_id=str(uuid4()))
    starting = _gen1_row(status="starting", bare_metal_server_id=str(uuid4()))
    migrated = _gen1_row(status="leased", box_generation=2, bare_metal_server_id=str(uuid4()))
    foreign_gen2 = _gen1_row(status="leased", box_generation=2, bare_metal_server_id=str(uuid4()))

    candidates, unmigratable = partition_migration_rows(
        [leased, stopped, available, starting, migrated, foreign_gen2], {migrated.id}
    )

    assert [row.id for row in candidates] == [leased.id, stopped.id]
    outcomes_by_id = {outcome.host_db_id: outcome for outcome in unmigratable}
    assert set(outcomes_by_id) == {available.id, starting.id, migrated.id, foreign_gen2.id}
    destroy = outcomes_by_id[available.id]
    assert destroy.stage == CutoverStage.FAILED
    assert destroy.detail is not None and f"pool destroy {available.id}" in destroy.detail
    wedged = outcomes_by_id[starting.id]
    assert wedged.stage == CutoverStage.FAILED
    assert wedged.detail is not None and "wait" in wedged.detail
    # A migration this tooling finished is reported, not failed.
    assert outcomes_by_id[migrated.id].stage == CutoverStage.RESTORED
    assert outcomes_by_id[migrated.id].detail == "already migrated"
    foreign = outcomes_by_id[foreign_gen2.id]
    assert foreign.stage == CutoverStage.FAILED
    assert foreign.detail is not None and "already on gen-2" in foreign.detail


def test_failed_remigration_leaves_a_terminal_record_untouched(tmp_path: Path) -> None:
    # A re-migration of a ROLLED_BACK workspace that dies before writing its
    # own state (here: the target capacity check refuses an unsized box) must
    # not flip the terminal record to FAILED -- a phantom in-flight record
    # still naming the old target would refuse retargets and block repaves of
    # both recorded boxes.
    state_store = CutoverStateStore(root=tmp_path / "cutover")
    state_store.ensure_layout()
    row = _gen1_row(status="leased", bare_metal_server_id=str(uuid4()), vps_address="15.204.1.2")
    rolled_back = make_cutover_workspace_state(row.id, str(uuid4()), target_server_id="old-target")
    state_store.write_workspace(
        rolled_back.model_copy_update(to_update(rolled_back.field_ref().stage, CutoverStage.ROLLED_BACK))
    )
    target = _ready_gen2_server()
    unsized_target = target.model_copy_update(to_update(target.field_ref().cpu_threads, None))
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = CutoverContext(
            env_name="dev-x",
            dsn="invalid dsn",
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
            identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
            ssh_ca_public_key=_TEST_SSH_CA,
            storage=_storage(),
            state=state_store,
            mngr_ctx=mngr_ctx,
        )
        outcome = _migrate_workspace(ctx, unsized_target, row, is_keep_origin_vm=False, replay_inputs_cache={})
    assert outcome.stage == CutoverStage.FAILED
    assert outcome.detail is not None and "sizing is unusable" in outcome.detail
    record = state_store.read_workspace(row.id)
    assert record is not None and record.stage == CutoverStage.ROLLED_BACK


def test_rollback_of_an_already_rolled_back_record_touches_nothing_but_the_keys(tmp_path: Path) -> None:
    # A completed rollback is terminal: the row may since have run and stopped
    # again on gen-1 with a newer artifact (post-rollback work), which a
    # re-run's park + artifact flip would clobber with the stale saved copy.
    # The invalid DSN proves the row is never even fetched; only the leftover
    # keys (a crash between the ROLLED_BACK write and the shred) are shredded.
    state_store = CutoverStateStore(root=tmp_path / "cutover")
    state_store.ensure_layout()
    rolled_back = make_cutover_workspace_state(uuid4().hex, str(uuid4()))
    rolled_back = rolled_back.model_copy_update(
        to_update(rolled_back.field_ref().stage, CutoverStage.ROLLED_BACK),
        to_update(rolled_back.field_ref().saved_artifact, make_saved_product_artifact()),
    )
    state_store.write_workspace(rolled_back)
    state_store.write_keys(rolled_back.host_db_id, make_harvested_keys())
    with minimal_mngr_context(tmp_path / "mngr-profile") as mngr_ctx:
        ctx = CutoverContext(
            env_name="dev-x",
            dsn="invalid dsn",
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
            identities=make_test_management_identities("dev", "pem", operator_key_path=tmp_path / "operator-key"),
            ssh_ca_public_key=_TEST_SSH_CA,
            storage=_storage(),
            state=state_store,
            mngr_ctx=mngr_ctx,
        )
        outcome = _rollback_workspace(ctx, rolled_back)
    assert outcome.stage == CutoverStage.ROLLED_BACK
    assert outcome.detail == "already rolled back"
    record = state_store.read_workspace(rolled_back.host_db_id)
    assert record is not None and record.stage == CutoverStage.ROLLED_BACK
    assert state_store.read_keys(rolled_back.host_db_id) is None


def test_render_migrate_reserve_script_picks_free_ports_and_replays_the_harvested_vm_keys() -> None:
    repaved = _ready_gen2_server()
    state = make_cutover_workspace_state(uuid4().hex, str(uuid4()), target_server_id=str(repaved.id))
    state = state.model_copy_update(to_update(state.field_ref().saved_artifact, make_saved_product_artifact()))
    keys = make_harvested_keys()

    script = _render_migrate_reserve_script(
        repaved, state, keys, ssh_ca_public_key=_TEST_SSH_CA, pool_public_key=_TEST_POOL_PUBLIC_KEY
    )

    assert_valid_bash(script)
    # Free ports are picked on the target (the migrated machine gets fresh
    # coordinates); nothing is fetched from the artifact's meta object.
    assert "pick_port" in script
    assert "fixed_port" not in script
    assert "s5cmd" not in script
    # The user-data carries the harvested VM host key and root keys (comments,
    # blank lines, and the gen-1 pool key dropped) plus the tier CA trust the
    # gen-2 target uses for management access instead of that key.
    expected_user_data = build_qemu_slice_user_data(
        host_dir="/mngr-btrfs",
        root_authorized_public_keys=("ssh-ed25519 AAAAOWNER owner",),
        host_private_key_pem=keys.vm_host_private_key.get_secret_value(),
        host_public_key_openssh=keys.vm_host_public_key,
        trusted_user_ca_public_key=_TEST_SSH_CA,
    )
    assert base64.b64encode(expected_user_data.encode()).decode() in script
    # The env template is sized from the target row: default units, the migrated
    # data disk, vCPUs from the 4.0 overcommit (32 threads x 4.0 x 8 / 120 units -> 8).
    expected_env = build_qemu_slice_env_file(
        instance_name=state.slice_instance_name,
        ordinal=None,
        vcpus=8,
        units=8,
        total_units=120,
        data_disk_gib=44,
        vm_ssh_host_port=GEN2_VM_SSH_PORT_PLACEHOLDER,
        container_ssh_host_port=GEN2_CONTAINER_SSH_PORT_PLACEHOLDER,
        uplink_mbps=1000,
    )
    assert base64.b64encode(expected_env.encode()).decode() in script
    # The cidata is placement-free: the stable-id meta-data and the DHCP
    # network-config, so cloud-init runs exactly once on the transplanted VM.
    expected_meta = build_qemu_slice_meta_data(state.slice_instance_name)
    assert base64.b64encode(expected_meta.encode()).decode() in script
    assert base64.b64encode(build_qemu_slice_network_config().encode()).decode() in script
    # Budgets from the target row (879 - 64 GiB disk, 120 units) and the df guard for boot + data + margin.
    assert "budget_gib=815" in script
    assert "budget_mib=122880" in script
    assert f"if [ {(10 + 44 + 2) * 1024**3} -gt 0 ]" in script


def test_render_migrate_reserve_script_refuses_missing_sizing_or_saved_artifact() -> None:
    repaved = _ready_gen2_server()
    unsized = repaved.model_copy_update(to_update(repaved.field_ref().cpu_threads, None))
    state = make_cutover_workspace_state(uuid4().hex, str(uuid4()), target_server_id=str(repaved.id))
    stopped = state.model_copy_update(to_update(state.field_ref().saved_artifact, make_saved_product_artifact()))
    with pytest.raises(CutoverError, match="cpu_threads"):
        _render_migrate_reserve_script(
            unsized,
            stopped,
            make_harvested_keys(),
            ssh_ca_public_key=_TEST_SSH_CA,
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
        )
    with pytest.raises(CutoverError, match="no saved artifact"):
        _render_migrate_reserve_script(
            repaved,
            state,
            make_harvested_keys(),
            ssh_ca_public_key=_TEST_SSH_CA,
            pool_public_key=_TEST_POOL_PUBLIC_KEY,
        )
