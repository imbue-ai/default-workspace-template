import json

from app_manifest.primitives import AppName
from app_manifest.shell_windows import read_app_window_paths
from app_manifest.shell_windows import window_paths_of_app
from app_manifest.shell_windows import window_query_value
from app_manifest.testing import ShellStub

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


def test_the_reader_answers_the_apps_window_paths_across_every_desktop(shell_stub: ShellStub) -> None:
    shell_stub.answer(200, json.dumps(_DESKTOPS))

    # An independent window's shared path stays home; what each client shows rides beside it and counts too.
    assert read_app_window_paths(shell_stub.url, _TERMINAL) == [
        "/?session=terminal-1",
        "/new?workdir=%2Fdata",
        "/",
        "/?session=terminal-7",
        "/?session=terminal-8",
    ]


def test_the_reader_answers_an_empty_list_for_an_app_with_no_windows(shell_stub: ShellStub) -> None:
    shell_stub.answer(200, json.dumps({"desktops": [{"id": "home", "windows": []}]}))

    assert read_app_window_paths(shell_stub.url, _TERMINAL) == []


def test_the_reader_answers_none_rather_than_no_windows_when_the_shell_cannot_be_read(
    shell_stub: ShellStub,
) -> None:
    shell_stub.answer(200, "not json")
    assert read_app_window_paths(shell_stub.url, _TERMINAL) is None

    shell_stub.answer(200, json.dumps({"desktops": [{"id": "home"}]}))
    assert read_app_window_paths(shell_stub.url, _TERMINAL) is None

    shell_stub.answer(503, json.dumps({"detail": "restarting"}))
    assert read_app_window_paths(shell_stub.url, _TERMINAL) is None

    url = shell_stub.url
    shell_stub.close()
    assert read_app_window_paths(url, _TERMINAL) is None


def test_a_document_of_the_wrong_shape_reads_as_none() -> None:
    assert window_paths_of_app([], _TERMINAL) is None
    assert window_paths_of_app({"desktops": {}}, _TERMINAL) is None
    assert window_paths_of_app({"desktops": [{"windows": [{"app": "terminal"}]}]}, _TERMINAL) is None
    assert window_paths_of_app({"desktops": [{"windows": [{"app": "t", "path": "/", "client_paths": []}]}]}, _TERMINAL) is None
    assert (
        window_paths_of_app({"desktops": [{"windows": [{"app": "t", "path": "/", "client_paths": {"c": 1}}]}]}, _TERMINAL)
        is None
    )
    assert window_paths_of_app({"desktops": []}, _TERMINAL) == []


def test_the_query_value_of_a_window_path_names_its_resource() -> None:
    assert window_query_value("/?session=terminal-3", "session") == "terminal-3"
    assert window_query_value("/?session=a&session=b", "session") == "a"
    assert window_query_value("/new?workdir=%2Fdata", "session") is None
    assert window_query_value("/", "session") is None
