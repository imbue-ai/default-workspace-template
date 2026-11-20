from pathlib import Path

import pytest

from app_store.errors import StoreStateError
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import RecordStore
from app_store.records import now_iso


def _record(slug: str) -> InstallRecord:
    return InstallRecord(
        slug=slug, version="1", status=InstallStatus.QUEUED, job=JobKind.INSTALL, updated_at=now_iso()
    )


def test_records_and_the_queue_survive_a_new_store_over_the_same_file(tmp_path: Path) -> None:
    path = tmp_path / "installs.json"
    store = RecordStore(path=path)
    store.put(_record("a"))
    store.put(_record("b"))
    store.enqueue("a")
    store.enqueue("b")
    store.enqueue("a")
    store.update("a", status=InstallStatus.INSTALLED, job=None, port=8090)

    reloaded = RecordStore(path=path).read()
    assert [record.slug for record in reloaded.records] == ["a", "b"]
    assert reloaded.queue == ("a", "b")
    found = reloaded.find("a")
    assert found is not None and (found.status, found.port) == (InstallStatus.INSTALLED, 8090)

    store.dequeue("a")
    store.remove("b")
    assert RecordStore(path=path).read().queue == ()
    assert RecordStore(path=path).read().find("b") is None
    assert not list(tmp_path.glob("*.tmp-*"))


def test_updating_a_record_that_does_not_exist_raises(tmp_path: Path) -> None:
    with pytest.raises(StoreStateError, match="no install record"):
        RecordStore(path=tmp_path / "installs.json").update("x", status=InstallStatus.FAILED)


def test_records_that_do_not_validate_stop_the_store_rather_than_forget_installs(tmp_path: Path) -> None:
    path = tmp_path / "installs.json"
    path.write_text('{"records": [{"slug": "a"}]}')
    with pytest.raises(StoreStateError, match="do not validate"):
        RecordStore(path=path).read()


def test_a_record_store_that_cannot_write_raises(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StoreStateError):
        RecordStore(path=blocker / "installs.json").put(_record("a"))
