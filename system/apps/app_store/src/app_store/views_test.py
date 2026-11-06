from app_store.catalog import load_app_catalog
from app_store.records import InstallRecord
from app_store.records import InstallStatus
from app_store.records import JobKind
from app_store.records import StoreRecords
from app_store.records import now_iso
from app_store.room import WorkspaceRoom
from app_store.testing import make_registry_row
from app_store.views import AppOrigin
from app_store.views import UpdateKind
from app_store.views import busy_slug
from app_store.views import store_app_entry
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


def test_an_installed_shipped_app_behind_the_catalog_offers_the_catalogs_version() -> None:
    app = load_app_catalog().find("datasette")
    assert app is not None
    room = WorkspaceRoom(disk_free_mb=10_000, memory_available_mb=4_000)
    installed = InstallRecord(
        slug="datasette", version="0.1", port=8080, status=InstallStatus.INSTALLED, updated_at=now_iso()
    )

    entry = store_app_entry(app, installed, room, has_log=True, memory_now_mb=64)
    assert (entry.installed_version, entry.update_kind, entry.update_version) == (
        "0.1",
        UpdateKind.CATALOG,
        app.version,
    )
    assert (entry.app_url, entry.memory_now_mb) == ("http://localhost:8080", 64)

    current = store_app_entry(app, installed.model_copy(update={"version": app.version}), room, True, 64)
    assert current.update_version is None

    # Until it is installed it has no version, address, memory or update to show.
    queued = store_app_entry(app, _record("datasette", InstallStatus.QUEUED), room, has_log=False, memory_now_mb=64)
    assert (queued.installed_version, queued.app_url, queued.memory_now_mb, queued.update_kind) == (
        None,
        None,
        None,
        None,
    )
    assert queued.update_version is None
