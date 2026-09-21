import json
from pathlib import Path

from imbue.system_interface.shell.data_types import StoredWindowPath
from imbue.system_interface.shell.primitives import ClientId
from imbue.system_interface.shell.primitives import WindowId
from imbue.system_interface.shell.primitives import WindowPath
from imbue.system_interface.shell.primitives import WindowTitle
from imbue.system_interface.shell.window_paths import WINDOW_PATHS_DIRNAME
from imbue.system_interface.shell.window_paths import WindowPathStore

_WIN_1 = WindowId("win-0000000000000001")
_WIN_2 = WindowId("win-0000000000000002")
_LIVE = frozenset({_WIN_1, _WIN_2})
_CLIENT = ClientId("c1")


def _stored(path: str, title: str = "") -> StoredWindowPath:
    return StoredWindowPath(path=WindowPath(path), title=WindowTitle(title))


def test_paths_read_empty_until_written_and_a_report_that_changes_nothing_writes_nothing(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    assert store.read_paths(_CLIENT, _LIVE) == {}
    assert store.set_path(_CLIENT, _WIN_1, _stored("/?chat=a", "Alpha"), _LIVE) is True
    file_path = tmp_path / WINDOW_PATHS_DIRNAME / "c1.json"
    stamp = file_path.stat().st_mtime_ns
    assert store.set_path(_CLIENT, _WIN_1, _stored("/?chat=a", "Alpha"), _LIVE) is False
    assert file_path.stat().st_mtime_ns == stamp
    assert store.read_paths(_CLIENT, _LIVE) == {_WIN_1: _stored("/?chat=a", "Alpha")}
    # Another client has paths of its own.
    assert store.read_paths(ClientId("c2"), _LIVE) == {}
    assert json.loads(file_path.read_text())["version"] == 1


def test_entries_of_windows_since_gone_are_dropped_on_read_and_on_the_next_write(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    store.set_path(_CLIENT, _WIN_1, _stored("/a"), _LIVE)
    store.set_path(_CLIENT, _WIN_2, _stored("/b"), _LIVE)
    assert set(store.read_paths(_CLIENT, frozenset({_WIN_2}))) == {_WIN_2}
    store.set_path(_CLIENT, _WIN_2, _stored("/c"), frozenset({_WIN_2}))
    assert set(json.loads((tmp_path / WINDOW_PATHS_DIRNAME / "c1.json").read_text())["windows"]) == {str(_WIN_2)}


def test_a_clients_file_goes_with_the_client_and_an_unreadable_one_reads_empty(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    assert store.delete_client_paths(_CLIENT) is False
    store.set_path(_CLIENT, _WIN_1, _stored("/a"), _LIVE)
    assert store.delete_client_paths(_CLIENT) is True
    assert store.read_paths(_CLIENT, _LIVE) == {}
    file_path = tmp_path / WINDOW_PATHS_DIRNAME / "c1.json"
    file_path.write_text(json.dumps({"version": 7, "windows": {}}))
    assert store.read_paths(_CLIENT, _LIVE) == {}
    file_path.write_text(json.dumps({"version": 1, "windows": {"nope": {"path": "/", "title": ""}}}))
    assert store.read_paths(_CLIENT, _LIVE) == {}
