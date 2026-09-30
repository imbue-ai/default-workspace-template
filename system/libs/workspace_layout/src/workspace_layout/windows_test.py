from app_manifest.primitives import AppName

from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.testing import LoopbackShell
from workspace_layout.windows import read_app_window_paths
from workspace_layout.windows import window_paths_of_app
from workspace_layout.windows import window_query_value

_TERMINAL = AppName("terminal")

_DESKTOPS = {
    "desktops": [
        {
            "id": "home",
            "windows": [
                {"id": "win-1", "app": "terminal", "path": "/?session=terminal-1"},
                {"id": "win-2", "app": "browser", "path": "/?session=browser-1"},
            ],
        },
        {"id": "work", "windows": [{"id": "win-3", "app": "terminal", "path": "/new?workdir=%2Fdata"}]},
        {
            "id": "pinned",
            "windows": [
                {
                    "id": "win-4",
                    "app": "terminal",
                    "path": "/",
                    "scope": "independent",
                    "client_paths": {"c1": "/?session=terminal-7", "c2": "/?session=terminal-8"},
                },
                {"id": "win-5", "app": "browser", "path": "/", "client_paths": {"c1": "/?session=browser-2"}},
            ],
        },
    ]
}


def test_the_reader_answers_the_apps_window_paths_across_every_desktop(loopback_shell: LoopbackShell) -> None:
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, _DESKTOPS)

    # An independent window's shared path stays home; what each client shows rides beside it and counts too.
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) == [
        "/?session=terminal-1",
        "/new?workdir=%2Fdata",
        "/",
        "/?session=terminal-7",
        "/?session=terminal-8",
    ]


def test_the_reader_answers_an_empty_list_for_an_app_with_no_windows(loopback_shell: LoopbackShell) -> None:
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, {"desktops": [{"id": "home", "windows": []}]})

    assert read_app_window_paths(loopback_shell.url, _TERMINAL) == []


def test_the_reader_answers_none_rather_than_no_windows_when_the_shell_cannot_be_read(
    loopback_shell: LoopbackShell,
) -> None:
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, "not json")
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, {"desktops": [{"id": "home"}]})
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    loopback_shell.get_answers[DESKTOPS_ROUTE] = (503, {"detail": "restarting"})
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    url = loopback_shell.url
    loopback_shell.close()
    assert read_app_window_paths(url, _TERMINAL) is None


def test_a_document_of_the_wrong_shape_reads_as_none() -> None:
    assert window_paths_of_app([], _TERMINAL) is None
    assert window_paths_of_app({"desktops": {}}, _TERMINAL) is None
    assert window_paths_of_app({"desktops": [{"windows": [{"app": "terminal"}]}]}, _TERMINAL) is None
    assert (
        window_paths_of_app({"desktops": [{"windows": [{"app": "t", "path": "/", "client_paths": []}]}]}, _TERMINAL)
        is None
    )
    assert (
        window_paths_of_app(
            {"desktops": [{"windows": [{"app": "t", "path": "/", "client_paths": {"c": 1}}]}]}, _TERMINAL
        )
        is None
    )
    assert window_paths_of_app({"desktops": []}, _TERMINAL) == []


def test_the_query_value_of_a_window_path_names_its_resource() -> None:
    assert window_query_value("/?session=terminal-3", "session") == "terminal-3"
    assert window_query_value("/?session=a&session=b", "session") == "a"
    assert window_query_value("/new?workdir=%2Fdata", "session") is None
    assert window_query_value("/", "session") is None
