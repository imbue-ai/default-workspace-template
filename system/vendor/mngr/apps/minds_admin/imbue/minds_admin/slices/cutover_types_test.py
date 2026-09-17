import json
from uuid import uuid4

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.cutover_types import BoxOutcome
from imbue.minds_admin.slices.cutover_types import CutoverBoxStage
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.cutover_types import CutoverWorkspaceState
from imbue.minds_admin.slices.cutover_types import LatchkeyReplayPlan
from imbue.minds_admin.slices.cutover_types import PreflightReport
from imbue.minds_admin.slices.cutover_types import RowVerdict
from imbue.minds_admin.slices.cutover_types import StageReport
from imbue.minds_admin.slices.cutover_types import WorkspaceOutcome
from imbue.minds_admin.slices.cutover_types import bake_tag_generation_error_or_none
from imbue.minds_admin.slices.cutover_types import classify_harvested_latchkey_files
from imbue.minds_admin.slices.cutover_types import classify_pool_row
from imbue.minds_admin.slices.cutover_types import classify_unplaced_gen1_row
from imbue.minds_admin.slices.cutover_types import gen1_data_disk_size_error_or_none
from imbue.minds_admin.slices.cutover_types import is_version_at_or_above_floor
from imbue.minds_admin.slices.cutover_types import parse_version_tag
from imbue.minds_admin.slices.cutover_types import restamped_gen1_disk_gb_or_none
from imbue.minds_admin.slices.cutover_types import version_tag_error_or_none
from imbue.minds_admin.slices.testing import make_cutover_workspace_state
from imbue.minds_admin.slices.testing import make_harvested_file
from imbue.minds_admin.slices.testing import make_harvested_latchkey_state


@pytest.mark.parametrize(
    ("status", "server_id", "verdict"),
    [
        ("leased", "s1", RowVerdict.CANDIDATE),
        ("stopped", "s1", RowVerdict.CANDIDATE),
        ("stopped", None, RowVerdict.CANDIDATE),
        ("available", "s1", RowVerdict.DESTROY),
        ("released", "s1", RowVerdict.DESTROY),
        ("stopping", "s1", RowVerdict.REFUSED),
        ("starting", "s1", RowVerdict.REFUSED),
        ("crashed", "s1", RowVerdict.REFUSED),
        ("removing", "s1", RowVerdict.REFUSED),
        ("unreachable", "s1", RowVerdict.REFUSED),
        ("baking", "s1", RowVerdict.REFUSED),
        ("mystery", "s1", RowVerdict.REFUSED),
    ],
)
def test_classify_pool_row_covers_every_status(status: str, server_id: str | None, verdict: RowVerdict) -> None:
    classification = classify_pool_row(status, server_id)
    assert classification.verdict == verdict
    if verdict == RowVerdict.REFUSED:
        assert classification.remedy


def test_finalized_stopped_rows_are_candidates_with_the_admin_start_note() -> None:
    # The migrate admin-starts a stopped row before harvesting it, so no shape
    # of "stopped" blocks a migration any more.
    classification = classify_pool_row("stopped", None)
    assert classification.verdict == RowVerdict.CANDIDATE
    assert classification.remedy is not None and "admin-starts" in classification.remedy


def test_unplaced_gen1_rows_are_migratable_only_as_finalized_stops() -> None:
    finalized = classify_unplaced_gen1_row("stopped")
    assert finalized.verdict == RowVerdict.CANDIDATE
    for status in ("leased", "available", "released", "starting", "crashed"):
        classification = classify_unplaced_gen1_row(status)
        assert classification.verdict == RowVerdict.REFUSED
        assert classification.remedy
    unplaced_lease = classify_unplaced_gen1_row("leased")
    assert unplaced_lease.remedy is not None and "no box placement" in unplaced_lease.remedy


def test_parse_version_tag_accepts_release_tags_and_prereleases_only() -> None:
    assert parse_version_tag("minds-v0.4.2\n") is not None
    parsed = parse_version_tag("minds-v0.4.2")
    assert parsed is not None
    assert parsed.as_tuple() == (0, 4, 2)
    assert parsed.prerelease is None
    prerelease = parse_version_tag("minds-v0.5.0-rc1")
    assert prerelease is not None
    assert prerelease.prerelease == "rc1"
    assert parse_version_tag("v0.4.2") is None
    assert parse_version_tag("fatal: No names found") is None
    assert parse_version_tag("") is None


def test_version_floor_is_minds_v0_3_10() -> None:
    below = parse_version_tag("minds-v0.3.9")
    at_floor = parse_version_tag("minds-v0.3.10")
    above = parse_version_tag("minds-v0.4.0")
    assert below is not None and at_floor is not None and above is not None
    assert not is_version_at_or_above_floor(below)
    assert is_version_at_or_above_floor(at_floor)
    assert is_version_at_or_above_floor(above)


def test_version_tag_error_names_the_missing_tag_or_the_floor() -> None:
    assert version_tag_error_or_none("minds-v0.4.2") is None
    assert version_tag_error_or_none("minds-v0.3.10\n") is None
    below = version_tag_error_or_none("minds-v0.3.9")
    assert below is not None and "below the cutover floor" in below and "minds-v0.3.9" in below
    untagged = version_tag_error_or_none("fatal: No names found, cannot describe anything.")
    assert untagged is not None and "not a minds-v* release tag" in untagged


@pytest.mark.parametrize(
    ("box_generation", "ref", "expected_fragment"),
    [
        # Release tags below 0.6 pair with gen-1: allowed there, refused on gen-2.
        (1, "minds-v0.5.0", None),
        (2, "minds-v0.5.0", "predates minds-v0.6.0"),
        (2, "minds-v0.5.99", "predates minds-v0.6.0"),
        # 0.6+ release tags pair with gen-2: allowed there, refused on gen-1.
        (2, "minds-v0.6.0", None),
        (2, "minds-v1.2.3", None),
        (1, "minds-v0.6.0", "gen-2 release"),
        (1, "minds-v1.0.0", "gen-2 release"),
        # Non-release refs (dev branches, absent) are exempt on both generations.
        (1, "main", None),
        (2, "main", None),
        (2, "release/minds_v0.5.0", None),
        (1, None, None),
        (2, None, None),
    ],
)
def test_bake_tag_generation_guard_pairs_release_lines_with_generations(
    box_generation: int, ref: str | None, expected_fragment: str | None
) -> None:
    error = bake_tag_generation_error_or_none(box_generation, ref)
    if expected_fragment is None:
        assert error is None
    else:
        assert error is not None and expected_fragment in error


def test_preflight_report_is_clean_only_without_refusals_or_errors() -> None:
    clean = PreflightReport(env_name="dev-x", boxes=(), distinct_version_tags=(), refused_count=0, error_count=0)
    assert clean.is_clean
    assert not clean.model_copy_update(to_update(clean.field_ref().refused_count, 1)).is_clean
    assert not clean.model_copy_update(to_update(clean.field_ref().error_count, 1)).is_clean


def test_stage_report_counts_failed_boxes_and_failed_workspaces() -> None:
    report = StageReport(
        stage_name="migrate",
        env_name="dev-x",
        is_dry_run=False,
        boxes=(
            BoxOutcome(server_id="a", stage=None, detail="unreachable"),
            BoxOutcome(
                server_id="b",
                stage=CutoverBoxStage.FINISHED,
                workspaces=(
                    WorkspaceOutcome(host_db_id="1", host_id="host-1", stage=CutoverStage.RESTORED),
                    WorkspaceOutcome(host_db_id="2", host_id="host-2", stage=CutoverStage.FAILED, detail="probe"),
                ),
            ),
        ),
    )
    assert report.failed_count == 2


def test_gen1_data_disk_size_must_match_the_stamped_gen2_size_minus_the_base() -> None:
    # 039 stamped disk_gb = gen-1 virtual size + 16: a 28 GiB gen-1 disk belongs to a 44 GiB row.
    assert gen1_data_disk_size_error_or_none(28, 44) is None
    error = gen1_data_disk_size_error_or_none(30, 44)
    assert error is not None
    assert "30 GiB" in error and "44" in error


@pytest.mark.parametrize("plan", list(LatchkeyReplayPlan))
def test_harvested_latchkey_state_decides_its_replay_plan_from_the_machines_own_tmpfs_pair(
    plan: LatchkeyReplayPlan,
) -> None:
    state = make_harvested_latchkey_state(plan)
    assert state.replay_plan == plan
    # The desktop-owned pair alone never makes a FULL replay: the gateway's
    # wrapper refuses to start without the machine's own key and password.
    if plan == LatchkeyReplayPlan.DISK_ONLY:
        assert {h.path.rsplit("/", 1)[1] for h in state.tmpfs_files} == {
            "desktop_gateway_password",
            "desktop_permissions_override",
        }


def test_classify_harvested_latchkey_files_sorts_by_location_and_refuses_strays() -> None:
    full = make_harvested_latchkey_state(LatchkeyReplayPlan.FULL)
    reclassified = classify_harvested_latchkey_files(True, list(reversed(full.all_files)))
    assert set(reclassified.disk_files) == set(full.disk_files)
    assert set(reclassified.supervisor_confs) == set(full.supervisor_confs)
    assert set(reclassified.tmpfs_files) == set(full.tmpfs_files)
    stray = make_harvested_file("/root/.ssh/authorized_keys", b"ssh-ed25519 AAAA\n", "0600")
    with pytest.raises(CutoverError, match="outside every known location"):
        classify_harvested_latchkey_files(True, [stray])
    # A path that carries a known prefix but climbs out of it would land
    # outside the operator's state dir when written under it.
    traversal = make_harvested_file(
        "/root/.latchkey/../../../../home/op/.ssh/authorized_keys", b"ssh-ed25519 AAAA\n", "0600"
    )
    with pytest.raises(CutoverError, match="not a normalized absolute path"):
        classify_harvested_latchkey_files(True, [traversal])
    with pytest.raises(CutoverError, match="not a normalized absolute path"):
        classify_harvested_latchkey_files(True, [make_harvested_file("root/.latchkey/config.json", b"{}", "0600")])
    with pytest.raises(CutoverError, match="no latchkey directory yet printed files"):
        classify_harvested_latchkey_files(False, list(full.tmpfs_files))


def test_workspace_records_written_before_the_latchkey_leg_still_parse() -> None:
    # CLEANUP: drop with the ``latchkey_replay_plan`` default once no pre-latchkey
    # record remains in any state dir.
    record = make_cutover_workspace_state(uuid4().hex, str(uuid4()))
    dumped = json.loads(record.model_dump_json())
    del dumped["latchkey_replay_plan"]
    reread = CutoverWorkspaceState.model_validate(dumped)
    assert reread.latchkey_replay_plan is None


def test_unmeasured_039_disk_stamp_is_restamped_from_the_measured_disk() -> None:
    # A row stopped on no box when 039 ran carries the default 44; its 29 GiB disk belongs to a 45 GiB row.
    assert restamped_gen1_disk_gb_or_none(29, 44) == 45
    # A 28 GiB disk agrees with the stamp, so nothing is restamped.
    assert restamped_gen1_disk_gb_or_none(28, 44) is None
    # A measured stamp that disagrees stays a refusal, never a silent restamp.
    assert restamped_gen1_disk_gb_or_none(30, 45) is None
    assert gen1_data_disk_size_error_or_none(30, 45) is not None
