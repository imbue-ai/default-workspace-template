from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app_store.desktop_shortcuts import AppShortcuts
from app_store.desktop_shortcuts import DesktopShortcut
from app_store.desktop_shortcuts import GridCell
from app_store.desktop_shortcuts import ShellDesktopShortcuts
from app_store.desktop_shortcuts import cell_for
from app_store.engine import InstallEngine
from app_store.errors import StoreStateError
from app_store.testing import FakeDesktopShortcuts
from app_store.testing import build_test_engine


def _shortcut(app: str, column: int, row: int = 0, desktop_id: str = "home") -> DesktopShortcut:
    return DesktopShortcut(
        desktop_id=desktop_id, app=app, launch="open", mode="focus", cell=GridCell(column=column, row=row)
    )


def _desktops(engine: InstallEngine) -> FakeDesktopShortcuts:
    assert isinstance(engine.shortcuts.desktops, FakeDesktopShortcuts)
    return engine.shortcuts.desktops


def _apps_on(desktops: FakeDesktopShortcuts, desktop_id: str = "home") -> list[str]:
    return [shortcut.app for shortcut in desktops.desktops[desktop_id]]


def _install(engine: InstallEngine, slug: str) -> None:
    engine.request_install(slug)
    assert engine.run_next_job()


def _uninstall(engine: InstallEngine, slug: str) -> None:
    engine.request_uninstall(slug, is_deleting_data=False)
    assert engine.run_next_job()


def test_an_uninstall_takes_the_apps_shortcuts_off_every_desktop_and_leaves_the_others(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    _install(engine, "datasette")
    # The shell offers the shortcut when the installed app registers.
    desktops.desktops = {
        "home": (_shortcut("chat", 0), _shortcut("datasette", 1)),
        "work": (_shortcut("datasette", 4, 2, "work"),),
    }

    _uninstall(engine, "datasette")

    assert _apps_on(desktops) == ["chat"]
    assert _apps_on(desktops, "work") == []


def test_a_reinstall_puts_the_shortcuts_back_where_they_were(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    _install(engine, "datasette")
    desktops.desktops = {"home": (_shortcut("chat", 0), _shortcut("datasette", 3, 1))}
    _uninstall(engine, "datasette")

    _install(engine, "datasette")

    assert desktops.desktops["home"][-1] == _shortcut("datasette", 3, 1)
    assert not engine.shortcuts.path.read_text().count("datasette")


def test_a_shortcut_put_back_onto_a_taken_cell_goes_to_the_first_free_one(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    _install(engine, "datasette")
    desktops.desktops = {"home": (_shortcut("chat", 0), _shortcut("datasette", 1))}
    _uninstall(engine, "datasette")
    desktops.desktops = {"home": (_shortcut("chat", 0), _shortcut("files", 1))}

    _install(engine, "datasette")

    assert desktops.desktops["home"][-1].cell == GridCell(column=2, row=0)


def test_a_reinstall_adds_nothing_where_the_shell_already_offered_one_or_the_desktop_is_gone(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    _install(engine, "datasette")
    desktops.desktops = {
        "home": (_shortcut("datasette", 1),),
        "work": (_shortcut("datasette", 0, 0, "work"),),
    }
    _uninstall(engine, "datasette")
    desktops.desktops = {"home": (_shortcut("datasette", 5),)}

    _install(engine, "datasette")

    assert desktops.desktops == {"home": (_shortcut("datasette", 5),)}


def test_an_unreachable_desktop_never_fails_an_install_or_uninstall(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    desktops.is_reachable = False

    _install(engine, "datasette")
    _uninstall(engine, "datasette")

    assert engine.records.read().records == ()
    assert not engine.shortcuts.path.exists()


def test_shortcuts_kept_while_the_shell_is_down_are_put_back_by_a_later_install(tmp_path: Path) -> None:
    engine = build_test_engine(tmp_path)
    desktops = _desktops(engine)
    _install(engine, "datasette")
    desktops.desktops = {"home": (_shortcut("chat", 0), _shortcut("datasette", 3))}
    _uninstall(engine, "datasette")
    desktops.is_reachable = False
    _install(engine, "datasette")
    _uninstall(engine, "datasette")
    desktops.is_reachable = True

    _install(engine, "datasette")

    assert desktops.desktops["home"] == (_shortcut("chat", 0), _shortcut("datasette", 3))


def test_an_unreadable_kept_file_counts_as_nothing_kept(tmp_path: Path) -> None:
    path = tmp_path / "desktop-shortcuts.json"
    path.write_text('{"format": 1, "apps": {"demo": "not a list"}}')
    desktops = FakeDesktopShortcuts()
    shortcuts = AppShortcuts(desktops=desktops, path=path)

    shortcuts.put_back("demo")

    assert desktops.desktops == {"home": ()}


def test_cell_for_keeps_a_free_cell_and_wraps_rows_over_eight_columns() -> None:
    wanted = GridCell(column=3, row=4)
    assert cell_for(wanted, set()) == wanted
    taken = {GridCell(column=column, row=0) for column in range(8)} | {wanted}
    assert cell_for(wanted, taken) == GridCell(column=0, row=1)


def _shell(monkeypatch: pytest.MonkeyPatch, handler: httpx.MockTransport) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def request(method: str, url: str, **kwargs: object) -> httpx.Response:
        with httpx.Client(transport=handler) as client:
            response = client.request(method, url, json=kwargs.get("json"))
        seen.append(response.request)
        return response

    monkeypatch.setattr(httpx, "request", request)
    return seen


def test_the_shell_client_reads_launch_shortcuts_and_posts_removals_and_additions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = {
        "desktops": [
            {
                "id": "home",
                "shortcuts": [
                    {
                        "target": {"kind": "launch", "app": "chat", "launch": "root"},
                        "mode": "new",
                        "cell": {"column": 0, "row": 0},
                    },
                    {
                        "target": {"kind": "url", "url": "https://example.com"},
                        "mode": "new",
                        "cell": {"column": 1, "row": 0},
                    },
                ],
            }
        ]
    }

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=document if request.method == "GET" else {"id": "home"})

    seen = _shell(monkeypatch, httpx.MockTransport(answer))
    client = ShellDesktopShortcuts()

    read = client.read()
    client.remove(_shortcut("datasette", 2))
    client.add(_shortcut("datasette", 2))

    assert read == {
        "home": (
            DesktopShortcut(desktop_id="home", app="chat", launch="root", mode="new", cell=GridCell(column=0, row=0)),
        )
    }
    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", "/api/desktops"),
        ("POST", "/api/desktops/home/shortcuts/remove"),
        ("POST", "/api/desktops/home/shortcuts"),
    ]
    assert b'"target":{"kind":"launch","app":"datasette","launch":"open"}' in seen[2].content.replace(b" ", b"")


@pytest.mark.parametrize(
    "answer",
    [
        lambda request: httpx.Response(400, json={"error": "no such desktop"}),
        lambda request: httpx.Response(200, content=b"not json"),
        lambda request: httpx.Response(200, json={"desktops": [{"id": "home"}]}),
    ],
    ids=["refused", "not-json", "wrong-shape"],
)
def test_the_shell_client_raises_a_store_state_error_for_a_bad_answer(
    monkeypatch: pytest.MonkeyPatch, answer: Callable[[httpx.Request], httpx.Response]
) -> None:
    _shell(monkeypatch, httpx.MockTransport(answer))
    with pytest.raises(StoreStateError):
        ShellDesktopShortcuts().read()


def test_the_shell_client_raises_a_store_state_error_when_the_shell_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    _shell(monkeypatch, httpx.MockTransport(refuse))
    with pytest.raises(StoreStateError, match="could not be reached"):
        ShellDesktopShortcuts().remove(_shortcut("datasette", 0))
