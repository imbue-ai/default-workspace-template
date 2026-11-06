from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import StoreRecords
from app_store.records import now_iso
from app_store.testing import make_registry_row
from app_store.views import AppOrigin
from app_store.views import busy_slug
from app_store.views import workspace_app_entries


def _record(slug: str, status: InstallStatus) -> InstallRecord:
    return InstallRecord(slug=slug, version="1", status=status, job=JobKind.INSTALL, updated_at=now_iso())


def test_the_workspaces_apps_are_listed_in_launcher_order_without_internals_or_the_store() -> None:
    rows = [
        make_registry_row("zeta", 8201, display_name="Zeta", priority="user"),
        make_registry_row("chat", 8010, display_name="Chat", priority="chat", launcher_rank=10),
        make_registry_row("alpha", 8202, display_name="alpha", priority="user"),
        make_registry_row("terminal-pty", 7683, internal=True),
        make_registry_row("app-store", 8060, display_name="App Store", launcher_rank=48),
        make_registry_row("datasette", 8080, display_name="Datasette", priority="user"),
    ]
    entries = workspace_app_entries(rows, store_slugs={"datasette"})
    assert [(entry.name, entry.origin) for entry in entries] == [
        ("chat", AppOrigin.BUILT_IN),
        ("alpha", AppOrigin.MADE_HERE),
        ("datasette", AppOrigin.STORE),
        ("zeta", AppOrigin.MADE_HERE),
    ]
    assert entries[0].icon_svg == ""
    assert entries[0].url == "http://localhost:8010"


def test_the_store_is_busy_with_the_head_of_its_queue() -> None:
    assert busy_slug(StoreRecords()) is None
    records = StoreRecords(
        records=(_record("a", InstallStatus.DOWNLOADING), _record("b", InstallStatus.QUEUED)), queue=("a", "b")
    )
    assert busy_slug(records) == "a"
    assert busy_slug(StoreRecords(queue=("ghost",))) is None
