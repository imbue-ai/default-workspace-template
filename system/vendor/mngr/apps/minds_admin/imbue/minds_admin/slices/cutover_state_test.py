import json
from uuid import uuid4

import pytest

from imbue.imbue_common.model_update import to_update
from imbue.minds_admin.slices.cutover_state import CutoverStateStore
from imbue.minds_admin.slices.cutover_types import CutoverBoxStage
from imbue.minds_admin.slices.cutover_types import CutoverBoxState
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.cutover_types import CutoverStage
from imbue.minds_admin.slices.testing import make_cutover_workspace_state
from imbue.minds_admin.slices.testing import make_harvested_keys


def test_state_store_round_trips_workspace_inspect_and_box_records(cutover_state_store: CutoverStateStore) -> None:
    store = cutover_state_store
    assert store.root.stat().st_mode & 0o777 == 0o700
    host_db_id = str(uuid4())
    assert store.read_workspace(host_db_id) is None
    state = make_cutover_workspace_state(host_db_id, str(uuid4()))
    store.write_workspace(state)
    assert store.read_workspace(host_db_id) == state
    assert store.list_workspaces() == [state]
    store.write_inspect(host_db_id, {"Name": "/c", "Config": {}})
    assert store.read_inspect(host_db_id) == {"Name": "/c", "Config": {}}
    # The inspect sidecar is not mistaken for a workspace record.
    assert store.list_workspaces() == [state]
    assert store.read_box("s1") is None
    assert store.list_boxes() == []
    box = CutoverBoxState(server_id="s1", stage=CutoverBoxStage.REPAVED, storage_partition_bytes=10**12)
    store.write_box(box)
    assert store.read_box("s1") == box
    other = CutoverBoxState(server_id="s0", stage=CutoverBoxStage.REPAVED)
    store.write_box(other)
    assert store.list_boxes() == [other, box]


def test_state_store_replaces_records_atomically(cutover_state_store: CutoverStateStore) -> None:
    # The restore's per-box threads list every workspace record while their
    # siblings write theirs: a record is renamed into place, never truncated,
    # so a concurrent reader gets the old or the new file and no .tmp sibling
    # is left behind.
    store = cutover_state_store
    host_db_id = str(uuid4())
    state = make_cutover_workspace_state(host_db_id, str(uuid4()))
    store.write_workspace(state)
    store.write_workspace(state.model_copy_update(to_update(state.field_ref().stage, CutoverStage.PARKED)))
    workspaces_dir = store.root / "workspaces"
    assert sorted(path.name for path in workspaces_dir.iterdir()) == [f"{host_db_id}.json"]
    reread = store.read_workspace(host_db_id)
    assert reread is not None and reread.stage == CutoverStage.PARKED
    assert (workspaces_dir / f"{host_db_id}.json").stat().st_mode & 0o777 == 0o600


def test_state_store_writes_keys_0600_and_shreds_them(cutover_state_store: CutoverStateStore) -> None:
    store = cutover_state_store
    host_db_id = str(uuid4())
    keys = make_harvested_keys()
    assert store.read_keys(host_db_id) is None
    store.write_keys(host_db_id, keys)
    keys_dir = store.root / "keys" / host_db_id
    assert keys_dir.stat().st_mode & 0o777 == 0o700
    for path in keys_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert store.read_keys(host_db_id) == keys
    store.shred_keys(host_db_id)
    assert not keys_dir.exists()
    assert store.read_keys(host_db_id) is None
    # Shredding an absent dir is a no-op.
    store.shred_keys(host_db_id)


def test_state_store_writes_json_and_text_reports(cutover_state_store: CutoverStateStore) -> None:
    json_path = cutover_state_store.write_report("preflight", json.dumps({"ok": True}), "OK\n")
    assert json_path.parent == cutover_state_store.root / "reports"
    assert json_path.name.startswith("preflight-") and json_path.suffix == ".json"
    assert json.loads(json_path.read_text()) == {"ok": True}
    assert json_path.with_suffix(".txt").read_text() == "OK\n"


def test_state_store_locks_refuse_a_second_holder_and_release_on_exit(cutover_state_store: CutoverStateStore) -> None:
    store = cutover_state_store
    with store.acquire_locks(["box-a", "workspace-1"]):
        # A disjoint set is fine (parallel invocations with different targets).
        with store.acquire_locks(["box-b", "workspace-2"]):
            pass
        # Any overlap refuses immediately instead of waiting.
        with pytest.raises(CutoverError, match="box-a"):
            with store.acquire_locks(["box-a"]):
                pass
        with pytest.raises(CutoverError, match="workspace-1"):
            with store.acquire_locks(["workspace-1", "workspace-3"]):
                pass
    # Released on exit: the same names can be taken again.
    with store.acquire_locks(["box-a", "workspace-1"]):
        pass
