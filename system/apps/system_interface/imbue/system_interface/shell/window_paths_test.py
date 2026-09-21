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
    assert store.set_path(_CLIENT, _WIN_1, _stored("/?chat=a", "Alpha"), lambda: _LIVE) is True
    file_path = tmp_path / WINDOW_PATHS_DIRNAME / "c1.json"
    stamp = file_path.stat().st_mtime_ns
    assert store.set_path(_CLIENT, _WIN_1, _stored("/?chat=a", "Alpha"), lambda: _LIVE) is False
    assert file_path.stat().st_mtime_ns == stamp
    assert store.read_paths(_CLIENT, _LIVE) == {_WIN_1: _stored("/?chat=a", "Alpha")}
    # Another client has paths of its own.
    assert store.read_paths(ClientId("c2"), _LIVE) == {}
    assert json.loads(file_path.read_text())["version"] == 1


def test_every_clients_paths_read_in_one_pass_with_clients_holding_none_left_out(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    assert store.read_all_paths(_LIVE) == {}
    store.set_path(_CLIENT, _WIN_1, _stored("/?chat=a", "Alpha"), lambda: _LIVE)
    store.set_path(ClientId("c2"), _WIN_1, _stored("/?chat=b"), lambda: _LIVE)
    store.set_path(ClientId("c2"), _WIN_2, _stored("/x"), lambda: _LIVE)
    # A client whose only entry names a window since gone reads as holding none; a stray file is skipped.
    store.set_path(ClientId("c3"), _WIN_2, _stored("/y"), lambda: _LIVE)
    (tmp_path / WINDOW_PATHS_DIRNAME / "not a client id.json").write_text("{}")
    assert store.read_all_paths(frozenset({_WIN_1})) == {
        _CLIENT: {_WIN_1: _stored("/?chat=a", "Alpha")},
        ClientId("c2"): {_WIN_1: _stored("/?chat=b")},
    }


def test_entries_of_windows_since_gone_are_dropped_on_read_and_on_the_next_write(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    store.set_path(_CLIENT, _WIN_1, _stored("/a"), lambda: _LIVE)
    store.set_path(_CLIENT, _WIN_2, _stored("/b"), lambda: _LIVE)
    assert set(store.read_paths(_CLIENT, frozenset({_WIN_2}))) == {_WIN_2}
    store.set_path(_CLIENT, _WIN_2, _stored("/c"), lambda: frozenset({_WIN_2}))
    assert set(json.loads((tmp_path / WINDOW_PATHS_DIRNAME / "c1.json").read_text())["windows"]) == {str(_WIN_2)}


def test_a_clients_file_goes_with_the_client_and_an_unreadable_one_reads_empty(tmp_path: Path) -> None:
    store = WindowPathStore(state_directory=tmp_path)
    assert store.delete_client_paths(_CLIENT) is False
    store.set_path(_CLIENT, _WIN_1, _stored("/a"), lambda: _LIVE)
    assert store.delete_client_paths(_CLIENT) is True
    assert store.read_paths(_CLIENT, _LIVE) == {}
    file_path = tmp_path / WINDOW_PATHS_DIRNAME / "c1.json"
    file_path.write_text(json.dumps({"version": 7, "windows": {}}))
    assert store.read_paths(_CLIENT, _LIVE) == {}
    file_path.write_text(json.dumps({"version": 1, "windows": {"nope": {"path": "/", "title": ""}}}))
    assert store.read_paths(_CLIENT, _LIVE) == {}
