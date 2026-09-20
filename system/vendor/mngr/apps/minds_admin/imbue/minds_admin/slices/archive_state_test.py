from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.minds_admin.slices.archive_state import ArchiveStateStore
from imbue.minds_admin.slices.archive_types import WorkspaceArchiveManifest
from imbue.minds_admin.slices.cutover_types import CutoverError
from imbue.minds_admin.slices.testing import make_workspace_archive_manifest


def _store(tmp_path: Path) -> ArchiveStateStore:
    store = ArchiveStateStore(root=tmp_path / "archives")
    store.ensure_layout()
    return store


def test_state_store_keeps_every_manifest_of_a_workspace_by_stamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.root.stat().st_mode & 0o777 == 0o700
    host_db_id = str(uuid4())
    first = make_workspace_archive_manifest(host_db_id, created_at=datetime(2026, 9, 1, tzinfo=timezone.utc))
    second = make_workspace_archive_manifest(
        host_db_id, created_at=first.created_at + timedelta(days=1), owner_email="bob@example.com"
    )
    other = make_workspace_archive_manifest(str(uuid4()), created_at=first.created_at)
    paths = [store.write_manifest(manifest) for manifest in (second, first, other)]
    assert [path.relative_to(store.root) for path in paths] == [
        Path("manifests") / host_db_id / "20260902T000000Z.json",
        Path("manifests") / host_db_id / "20260901T000000Z.json",
        Path("manifests") / other.host_db_id / "20260901T000000Z.json",
    ]
    assert [WorkspaceArchiveManifest.model_validate_json(path.read_text()) for path in paths] == [second, first, other]


def test_state_store_refuses_a_second_archive_of_the_same_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    host_db_id = str(uuid4())
    with store.acquire_workspace_lock(host_db_id):
        with pytest.raises(CutoverError, match="another invocation holds"):
            with store.acquire_workspace_lock(host_db_id):
                pass
        # A different workspace is not serialized behind it.
        with store.acquire_workspace_lock(str(uuid4())):
            pass
    with store.acquire_workspace_lock(host_db_id):
        pass


def test_state_store_writes_one_report_file_per_rendering(tmp_path: Path) -> None:
    paths = _store(tmp_path).write_report("archive-links", (("md", "# links\n"), ("csv", "a,b\n")))
    assert [path.suffix for path in paths] == [".md", ".csv"]
    assert paths[0].stem == paths[1].stem
    assert paths[0].read_text() == "# links\n"
    assert paths[1].read_text() == "a,b\n"
