import copy
from typing import Any
from typing import Final

from app_manifest.primitives import AppName

from workspace_layout.answers import DesktopsListing
from workspace_layout.shell_url import DESKTOPS_ROUTE
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import fake_desktop
from workspace_layout.testing import fake_window
from workspace_layout.windows import read_app_window_paths
from workspace_layout.windows import window_paths_of_app
from workspace_layout.windows import window_query_value

_TERMINAL = AppName("terminal")
# Marks a window field ``_with_window_field`` drops rather than sets.
_DROPPED: Final[object] = object()

_DESKTOPS = DesktopsListing(
    desktops=(
        fake_desktop(
            "home",
            [
                fake_window("win-0000000000000001", "terminal", "/?session=terminal-1"),
                fake_window("win-0000000000000002", "browser", "/?session=browser-1"),
            ],
        ),
        fake_desktop("work", [fake_window("win-0000000000000003", "terminal", "/new?workdir=%2Fdata")]),
        fake_desktop(
            "pinned",
            [
                fake_window(
                    "win-0000000000000004",
                    "terminal",
                    "/",
                    client_paths={"c1": "/?session=terminal-7", "c2": "/?session=terminal-8"},
                ),
                fake_window("win-0000000000000005", "browser", "/", client_paths={"c1": "/?session=browser-2"}),
            ],
        ),
    )
).model_dump(mode="json")


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
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, {"desktops": [fake_desktop("home").model_dump(mode="json")]})

    assert read_app_window_paths(loopback_shell.url, _TERMINAL) == []


def test_the_reader_answers_none_rather_than_no_windows_when_the_shell_cannot_be_read(
    loopback_shell: LoopbackShell, cut_off_shell_url: str
) -> None:
    # A shell that stops mid-answer (a restart) is as unreadable as one that is down, so the sweep skips rather
    # than its thread dying on the error.
    assert read_app_window_paths(cut_off_shell_url, _TERMINAL) is None

    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, "not json")
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, {"desktops": [{"id": "home"}]})
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    loopback_shell.get_answers[DESKTOPS_ROUTE] = (503, {"detail": "restarting"})
    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None

    url = loopback_shell.url
    loopback_shell.close()
    assert read_app_window_paths(url, _TERMINAL) is None


def _with_window_field(desktop_index: int, window_index: int, field: str, value: Any) -> dict[str, Any]:
    """The desktops document with one window's ``field`` set to ``value``, or dropped for ``_DROPPED``."""
    document = copy.deepcopy(_DESKTOPS)
    window = document["desktops"][desktop_index]["windows"][window_index]
    if value is _DROPPED:
        del window[field]
    else:
        window[field] = value
    return document


def test_a_document_of_the_wrong_shape_reads_as_none() -> None:
    assert window_paths_of_app([], _TERMINAL) is None
    assert window_paths_of_app({"desktops": {}}, _TERMINAL) is None
    assert window_paths_of_app(_with_window_field(0, 0, "path", _DROPPED), _TERMINAL) is None
    assert window_paths_of_app(_with_window_field(2, 0, "client_paths", ["/?session=terminal-7"]), _TERMINAL) is None
    assert window_paths_of_app(_with_window_field(2, 0, "client_paths", {"c1": 1}), _TERMINAL) is None
    assert window_paths_of_app({"desktops": []}, _TERMINAL) == []


def test_one_unreadable_window_of_another_app_makes_the_whole_read_unknown() -> None:
    """A sweep must not act on a document it could only partly read, whichever app's window broke it."""
    assert window_paths_of_app(_with_window_field(0, 1, "path", "no-leading-slash"), _TERMINAL) is None


def test_the_reader_warns_naming_the_field_that_made_the_desktops_unreadable(
    loopback_shell: LoopbackShell, loguru_records: list[str]
) -> None:
    loopback_shell.get_answers[DESKTOPS_ROUTE] = (200, _with_window_field(0, 1, "path", "no-leading-slash"))

    assert read_app_window_paths(loopback_shell.url, _TERMINAL) is None
    assert any(
        record.startswith("WARNING") and "not a desktops listing" in record and "'desktops.0.windows.1.path'" in record
        for record in loguru_records
    ), loguru_records


def test_the_query_value_of_a_window_path_names_its_resource() -> None:
    assert window_query_value("/?session=terminal-3", "session") == "terminal-3"
    assert window_query_value("/?session=a&session=b", "session") == "a"
    assert window_query_value("/new?workdir=%2Fdata", "session") is None
    assert window_query_value("/", "session") is None
