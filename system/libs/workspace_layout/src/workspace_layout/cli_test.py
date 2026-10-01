"""Tests for the agent-facing ``workspace-layout`` command.

They cover what an agent depends on: how apps and windows are named (the retired spellings
and verbs are refused with the replacement), the bodies the ops post and what they print from
the shell's answer, the read commands over the inventory, and the exit codes. The shell they post
to reads every op body as the real one does, so a body the shell would refuse fails here too.
"""

import json
from typing import Any

import pytest
from app_manifest.manifest import DefaultShortcut
from app_manifest.manifest import ShortcutMode
from app_manifest.primitives import AppName
from app_manifest.primitives import LaunchPathId
from app_manifest.primitives import LaunchPathValue
from app_manifest.registry import RegistryLaunchPath
from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.primitives import NonEmptyStr

from workspace_layout.answers import ClientActivitySummary
from workspace_layout.answers import InventoryClient
from workspace_layout.answers import InventoryDocument
from workspace_layout.cli import EXIT_CONFLICT
from workspace_layout.cli import EXIT_ERROR
from workspace_layout.cli import EXIT_OK
from workspace_layout.cli import RETIRED_VERBS
from workspace_layout.cli import LayoutCliContext
from workspace_layout.cli import app_name_argument
from workspace_layout.cli import run_layout_cli
from workspace_layout.cli import window_argument
from workspace_layout.ops import OpRequester
from workspace_layout.primitives import ClientId
from workspace_layout.primitives import DesktopId
from workspace_layout.primitives import WindowId
from workspace_layout.records import DesktopShortcut
from workspace_layout.records import WindowView
from workspace_layout.shell_url import INVENTORY_ROUTE
from workspace_layout.shell_url import LAYOUT_OP_ROUTE
from workspace_layout.testing import FAKE_TIME
from workspace_layout.testing import LoopbackShell
from workspace_layout.testing import describe_op_body_problem
from workspace_layout.testing import desktop_answer
from workspace_layout.testing import fake_app
from workspace_layout.testing import fake_desktop
from workspace_layout.testing import fake_window

_CHAT_ID = "win-0000000000000001"
_FILES_ID = "win-0000000000000002"
_CHAT_WINDOW = fake_window(_CHAT_ID, "chat", "/?chat=agent-1", "Alice")
_FILES_WINDOW = fake_window(_FILES_ID, "files", "/notes/", "notes")


def _answer(windows: list[WindowView], window_id: str | None) -> dict[str, Any]:
    return desktop_answer(fake_desktop("home", windows), "c1", window_id)


def _json_documents(text: str) -> list[Any]:
    """Every JSON document in ``text``, one per read or shortcut write that printed on stdout."""
    decoder = json.JSONDecoder()
    documents: list[Any] = []
    rest = text.lstrip()
    while rest:
        document, end = decoder.raw_decode(rest)
        documents.append(document)
        rest = rest[end:].lstrip()
    return documents


# naming apps and windows


@pytest.mark.parametrize(
    ("spelling", "expected_hint"),
    [
        ("app:chat?instance=agent-1", "workspace-layout open chat --path <path>"),
        ("app:files", "workspace-layout open files"),
        ("chat:alice", 'open chat --path "/?chat=<chat-id>"'),
        ("chat-terminal:alice", "back face of its chat"),
        ("terminal:terminal-3", 'open terminal --path "/?session=terminal-3"'),
        ("service:files", "workspace-layout open files"),
        ("service:browser?session=riley", '--path "/?session=riley"'),
        ("url:abcd1234", "workspace-layout open https://"),
        ("subagent:abcd", "page of the chat app"),
    ],
)
def test_the_retired_spellings_are_refused_with_the_form_to_use(
    spelling: str, expected_hint: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        app_name_argument(spelling)
    assert raised.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert "give an app name and a path" in err and expected_hint in err
    with pytest.raises(SystemExit):
        window_argument(spelling)


@pytest.mark.parametrize("verb", sorted(RETIRED_VERBS))
def test_the_retired_verbs_are_refused_with_the_replacement(
    verb: str, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        run_layout_cli([verb, "win-0000000000000001", "--relative-to", "self"], layout_context)
    assert raised.value.code == EXIT_ERROR
    err = capsys.readouterr().err
    assert f"'{verb}' is not a desktop verb" in err and RETIRED_VERBS[verb] in err


@pytest.mark.parametrize(
    ("bad_name", "fragment"),
    [
        ("Foo.Bar", "not an app name"),
        ("-leading", "not an app name"),
        ("a" * 33, "not an app name"),
        ("localhost", "not an app name"),
        ("agent-abc", "not an app name"),
        ("https://example.com", "is a URL"),
    ],
)
def test_a_name_the_registry_could_never_hold_is_refused_without_waiting(
    loopback_shell: LoopbackShell,
    layout_context: LayoutCliContext,
    capsys: pytest.CaptureFixture[str],
    bad_name: str,
    fragment: str,
) -> None:
    with pytest.raises(SystemExit):
        app_name_argument(bad_name)
    assert fragment in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "Foo.Bar", "--path", "/"], layout_context)
    assert loopback_shell.posted == []


def test_windows_are_named_by_id_self_or_app(capsys: pytest.CaptureFixture[str]) -> None:
    assert window_argument("win-0123456789abcdef") == "win-0123456789abcdef"
    assert window_argument("self") == "self"
    assert window_argument("pinned") == "pinned"
    assert window_argument("files") == "files"
    with pytest.raises(SystemExit):
        window_argument("win 12")
    assert "not a window" in capsys.readouterr().err


# open


def test_open_waits_for_registration_then_posts_the_app_and_prints_the_window_id(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.op_answer = _answer([_FILES_WINDOW], _FILES_ID)
    assert (
        run_layout_cli(
            ["open", "files", "--path", "/notes/", "--desktop", "Research", "--client", "c9"], layout_context
        )
        == EXIT_OK
    )
    assert loopback_shell.posted_ops() == [
        ("open", {"app": "files", "path": "/notes/", "desktop": "Research", "client": "c9"})
    ]
    captured = capsys.readouterr()
    assert captured.out == f"{_FILES_ID}\n"
    assert captured.err == f"opened window {_FILES_ID} (files at /notes/) on desktop home for client c1\n"


def test_open_of_a_launch_path_or_a_url_posts_the_launch_and_its_params(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    # The shell answers an open with the window's id whether it opened the window or focused the one
    # already at that path; both are printed alike.
    loopback_shell.op_answer = _answer([_CHAT_WINDOW], _CHAT_ID)
    assert (
        run_layout_cli(
            ["open", "terminal", "--launch", "new", "--param", "workdir=/data", "--if-present", "new"], layout_context
        )
        == 0
    )
    assert run_layout_cli(["open", "https://example.com/docs"], layout_context) == 0
    assert run_layout_cli(["open", "chat"], layout_context) == 0
    assert run_layout_cli(["open", "chat", "--minimized"], layout_context) == 0
    assert loopback_shell.posted_ops() == [
        ("open", {"app": "terminal", "launch": "new", "params": {"workdir": "/data"}, "if_present": "new"}),
        ("open", {"app": "browser", "launch": "new", "params": {"url": "https://example.com/docs"}}),
        ("open", {"app": "chat"}),
        ("open", {"app": "chat", "minimized": True}),
    ]
    assert capsys.readouterr().out == f"{_CHAT_ID}\n" * 4


def test_open_beside_posts_the_window_it_pairs_with_and_says_so(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.op_answer = _answer([_FILES_WINDOW], _FILES_ID)
    assert run_layout_cli(["open", "files", "--beside"], layout_context) == EXIT_OK
    assert run_layout_cli(["open", "files", "--beside", _CHAT_ID], layout_context) == EXIT_OK
    assert run_layout_cli(["open", "files", "--beside", "terminal"], layout_context) == EXIT_OK
    assert loopback_shell.posted_ops() == [
        ("open", {"app": "files", "beside": "self"}),
        ("open", {"app": "files", "beside": _CHAT_ID}),
        ("open", {"app": "files", "beside": "terminal"}),
    ]
    assert f"opened window {_FILES_ID} (files at /notes/) beside self on" in capsys.readouterr().err


def test_open_beside_refuses_a_non_window_and_refuses_minimized(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "files", "--beside", "not a window"], layout_context)
    assert "is not a window" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "files", "--beside", "--minimized"], layout_context)
    assert "one or the other" in capsys.readouterr().err
    assert loopback_shell.posted == []


def test_open_with_no_client_says_the_window_landed_for_nobody(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.op_answer = desktop_answer(fake_desktop("home", [_CHAT_WINDOW]), None, _CHAT_ID)
    assert run_layout_cli(["open", "chat"], layout_context) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == f"{_CHAT_ID}\n"
    assert captured.err.endswith("on desktop home for no client (minimized everywhere)\n")


def test_open_arguments_are_refused_where_they_make_no_sense(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "files", "--path", "/notes/", "--launch", "new"], layout_context)
    assert "one or the other" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "files", "--path", "notes"], layout_context)
    assert "starts with a single '/'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "https://example.com", "--param", "url=x"], layout_context)
    assert "do not apply" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["open", "terminal", "--param", "novalue"], layout_context)
    assert "name=value" in capsys.readouterr().err
    assert loopback_shell.posted == []


def test_open_of_an_unregistered_app_fails_without_posting(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_layout_cli(["open", "nope"], layout_context) == EXIT_ERROR
    assert loopback_shell.posted == []
    assert "not registered" in capsys.readouterr().err


# show


def test_show_posts_the_page_and_what_counts_as_showing_it_and_prints_the_window_id(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.op_answer = {**_answer([_FILES_WINDOW], _FILES_ID), "shown": "raised"}
    command = ["show", "files", "--path", "/notes/", "--showing", "/notes", "--showing", "/notes/?view"]
    assert run_layout_cli([*command, "--repoint", "/", "--client", "c9"], layout_context) == EXIT_OK
    assert run_layout_cli(["show", "files", "--path", "/a?view"], layout_context) == EXIT_OK
    assert loopback_shell.posted_ops() == [
        (
            "show",
            {
                "app": "files",
                "path": "/notes/",
                "showing": ["/notes", "/notes/?view"],
                "repoint": ["/"],
                "client": "c9",
            },
        ),
        ("show", {"app": "files", "path": "/a?view"}),
    ]
    captured = capsys.readouterr()
    assert captured.out == f"{_FILES_ID}\n" * 2
    assert f"raised window {_FILES_ID} (files at /notes/) on desktop home for client c1\n" in captured.err


@pytest.mark.parametrize(
    ("arguments", "fragment"),
    [
        (["--path", "notes"], "--path: invalid window path"),
        (["--path", "/a", "--showing", "a"], "--showing: invalid window path"),
        (["--path", "/a", "--repoint", "/?x=1"], "--repoint: invalid page"),
        (["--path", "/a", "--repoint", "notes/"], "--repoint: invalid window path"),
    ],
)
def test_show_refuses_a_path_or_a_page_the_shell_would_refuse(
    loopback_shell: LoopbackShell,
    layout_context: LayoutCliContext,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    fragment: str,
) -> None:
    with pytest.raises(SystemExit):
        run_layout_cli(["show", "files", *arguments], layout_context)
    assert fragment in capsys.readouterr().err
    assert loopback_shell.posted == []


# the window verbs


def test_the_window_verbs_post_the_window_and_the_target(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.op_answer = _answer([_CHAT_WINDOW], _CHAT_ID)
    assert run_layout_cli(["focus", _CHAT_ID], layout_context) == 0
    assert run_layout_cli(["minimize", "self", "--client", "c2"], layout_context) == 0
    assert run_layout_cli(["restore", "chat", "--desktop", "Research"], layout_context) == 0
    assert run_layout_cli(["maximize", "self"], layout_context) == 0
    assert run_layout_cli(["place", "self", "--state", "snapped-left"], layout_context) == 0
    assert run_layout_cli(["place", "chat", "--frame", "0,0,0.5,1"], layout_context) == 0
    assert run_layout_cli(["navigate", "self", "/?chat=agent-2"], layout_context) == 0
    assert run_layout_cli(["close", _CHAT_ID], layout_context) == 0
    assert loopback_shell.posted_ops() == [
        ("focus", {"window": _CHAT_ID}),
        ("minimize", {"window": "self", "client": "c2"}),
        ("restore", {"window": "chat", "desktop": "Research"}),
        ("maximize", {"window": "self"}),
        ("place", {"window": "self", "state": "SNAPPED_LEFT"}),
        ("place", {"window": "chat", "frame": {"x": 0.0, "y": 0.0, "width": 0.5, "height": 1.0}}),
        ("navigate", {"window": "self", "path": "/?chat=agent-2"}),
        ("close", {"window": _CHAT_ID}),
    ]
    err = capsys.readouterr().err
    assert f"focused window {_CHAT_ID} (chat at /?chat=agent-1) on desktop home for client c1" in err
    assert "placed window" in err and "as snapped-left" in err and "at frame 0,0,0.5,1" in err
    assert "pointed window" in err and "at /?chat=agent-2" in err


def test_close_names_a_window_the_answer_no_longer_lists_by_its_id_alone(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    # The desktop the shell answers a close with has the window gone, so the summary has only its id to give.
    loopback_shell.op_answer = _answer([], _CHAT_ID)
    assert run_layout_cli(["close", _CHAT_ID], layout_context) == 0
    assert capsys.readouterr().err == f"closed window {_CHAT_ID} on desktop home for client c1\n"


def test_place_and_navigate_check_their_arguments(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        run_layout_cli(["place", "self"], layout_context)
    assert "exactly one of" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["place", "self", "--state", "maximized", "--frame", "0,0,1,1"], layout_context)
    assert "exactly one of" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["place", "self", "--frame", "0,0,1.5,1"], layout_context)
    assert "inside the unit square" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        run_layout_cli(["navigate", "self", "notes"], layout_context)
    assert "starts with a single '/'" in capsys.readouterr().err
    assert loopback_shell.posted == []


def test_the_retired_zone_flag_names_the_state_flag(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        run_layout_cli(["place", "self", "--zone", "left"], layout_context)
    err = capsys.readouterr().err
    assert "--zone is retired" in err and "--state (snapped-left, snapped-right, maximized)" in err
    assert loopback_shell.posted == []
    assert loopback_shell.posted == []


def test_refresh_reaches_one_window_or_every_page_of_an_app(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_layout_cli(["refresh", "self"], layout_context) == 0
    loopback_shell.refresh_target = None
    assert run_layout_cli(["refresh", "--app", "files"], layout_context) == 0
    assert loopback_shell.posted_ops() == [("refresh", {"window": "self"}), ("refresh", {"app": "files"})]
    err = capsys.readouterr().err
    assert "(sent refresh to client c1)" in err and "(sent refresh to every client)" in err
    with pytest.raises(SystemExit):
        run_layout_cli(["refresh"], layout_context)
    with pytest.raises(SystemExit):
        run_layout_cli(["refresh", "self", "--app", "files"], layout_context)
    # A whole-app refresh reaches every client, so a --client or --desktop beside it is refused rather
    # than silently dropped.
    with pytest.raises(SystemExit):
        run_layout_cli(["refresh", "--app", "files", "--client", "c2"], layout_context)
    assert "do not apply" in capsys.readouterr().err


# the read commands


def test_context_and_load_ride_the_op_route(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = ClientActivitySummary(
        client_id=ClientId("c1"),
        active_desktop=DesktopId("home"),
        last_seen="2026-09-04T00:00:00Z",
        is_connected=True,
        recent_messages=(),
    )
    loopback_shell.context_clients = [summary]
    assert run_layout_cli(["context"], layout_context) == 0
    assert json.loads(capsys.readouterr().out) == [summary.model_dump(mode="json")]
    loopback_shell.op_answer = desktop_answer(fake_desktop("research"), "c1", None)
    assert run_layout_cli(["load", "Research", "--client", "c1"], layout_context) == 0
    assert "switched client c1 onto desktop research" in capsys.readouterr().err
    assert loopback_shell.posted_ops() == [("context", {}), ("load", {"desktop": "Research", "client": "c1"})]


def test_desktops_and_list_read_the_inventory_document(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    new_viewer = RegistryLaunchPath(
        id=LaunchPathId("new"), label=NonEmptyStr("New File Viewer"), path=LaunchPathValue("/")
    )
    files = fake_app(
        "files",
        launch_paths=[new_viewer],
        default_shortcut=DefaultShortcut(launch=LaunchPathId("new"), mode=ShortcutMode.FOCUS),
    )
    shortcuts = [
        {
            "target": {"kind": "launch", "app": "files", "launch": "new"},
            "mode": "focus",
            "cell": {"column": 0, "row": 0},
        }
    ]
    home = fake_desktop(
        "home", [_FILES_WINDOW], shortcuts=[DesktopShortcut.model_validate(shortcut) for shortcut in shortcuts]
    )
    clients = (
        InventoryClient(
            id=ClientId("c1"),
            active_desktop=DesktopId("home"),
            last_seen=FAKE_TIME,
            shown_history=("home", _FILES_ID),
            is_connected=True,
            shown=(WindowId(_FILES_ID),),
        ),
        InventoryClient(id=ClientId("c2"), last_seen=FAKE_TIME, is_connected=False, shown=()),
    )
    inventory = InventoryDocument(
        is_preview=False,
        workspace_name="workspace",
        desktops=(home,),
        apps=(files, fake_app("owner-exec", is_internal=True)),
        clients=clients,
    )
    loopback_shell.get_answers[INVENTORY_ROUTE] = (200, inventory.model_dump(mode="json"))
    assert run_layout_cli(["desktops", "--json"], layout_context) == 0
    desktops = json.loads(capsys.readouterr().out)
    assert desktops["desktops"] == [
        {
            "id": "home",
            "name": "Home",
            "wallpaper": None,
            "shortcuts": shortcuts,
            "windows": [
                {
                    "id": _FILES_ID,
                    "app": "files",
                    "path": "/notes/",
                    "title": "notes",
                    "is_pinned": False,
                    "scope": "linked",
                    "client_paths": {},
                }
            ],
        }
    ]
    assert [
        (client["id"], client["active_desktop"], client["shown"], client["shown_history"])
        for client in desktops["clients"]
    ] == [
        ("c1", "home", [_FILES_ID], ["home", _FILES_ID]),
        ("c2", None, [], []),
    ]
    assert run_layout_cli(["list", "--json"], layout_context) == 0
    listing = json.loads(capsys.readouterr().out)
    # Internal apps are the shell's own; they are not listed.
    assert [app["name"] for app in listing["apps"]] == ["files"]
    assert listing["apps"][0]["launch_paths"] == [new_viewer.model_dump(mode="json")]
    assert listing["apps"][0]["windows"] == [
        {
            "id": _FILES_ID,
            "app": "files",
            "path": "/notes/",
            "title": "notes",
            "is_pinned": False,
            "scope": "linked",
            "client_paths": {},
            "desktop": "home",
        }
    ]
    assert [desktop["id"] for desktop in listing["desktops"]] == ["home"]


# shortcuts and the wallpaper


def test_shortcut_verbs_post_to_the_desktop_and_print_its_shortcuts(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    row = {
        "target": {"kind": "launch", "app": "docs", "launch": "open"},
        "mode": "new",
        "cell": {"column": 1, "row": 0},
    }
    loopback_shell.op_answer = desktop_answer(
        fake_desktop("research", shortcuts=[DesktopShortcut.model_validate(row)]), "c1", None
    )
    assert run_layout_cli(["shortcuts", "--desktop", "Research", "--json"], layout_context) == 0
    assert json.loads(capsys.readouterr().out) == {"desktop": "research", "shortcuts": [row]}
    assert (
        run_layout_cli(
            ["shortcut", "set", "docs", "open", "--mode", "new", "--cell", "1,0", "--desktop", "Research"],
            layout_context,
        )
        == 0
    )
    assert run_layout_cli(["shortcut", "move", "docs", "open", "--cell", "2,0"], layout_context) == 0
    assert run_layout_cli(["shortcut", "remove", "docs", "open"], layout_context) == 0
    assert run_layout_cli(["wallpaper", "bundled", "dunes"], layout_context) == 0
    assert run_layout_cli(["wallpaper", "none"], layout_context) == 0
    assert loopback_shell.posted_ops() == [
        ("shortcuts", {"desktop": "Research"}),
        (
            "shortcut_set",
            {"app": "docs", "launch": "open", "mode": "new", "cell": {"column": 1, "row": 0}, "desktop": "Research"},
        ),
        ("shortcut_move", {"app": "docs", "launch": "open", "cell": {"column": 2, "row": 0}}),
        ("shortcut_remove", {"app": "docs", "launch": "open"}),
        ("wallpaper", {"wallpaper": {"kind": "bundled", "name": "dunes"}}),
        ("wallpaper", {"wallpaper": None}),
    ]
    captured = capsys.readouterr()
    assert "set shortcut docs open (new) on desktop research" in captured.err
    assert "moved shortcut docs open to cell 2,0" in captured.err
    assert "removed shortcut docs open" in captured.err
    assert "set the wallpaper to bundled dunes" in captured.err and "cleared the wallpaper" in captured.err
    assert [document["desktop"] for document in _json_documents(captured.out)] == ["research"] * 3
    with pytest.raises(SystemExit):
        run_layout_cli(["wallpaper", "sky"], layout_context)
    with pytest.raises(SystemExit):
        run_layout_cli(["wallpaper", "none", "dunes"], layout_context)


# exit codes and the wire


@pytest.mark.parametrize(
    ("response", "exit_code", "fragment"),
    [
        ((409, {"detail": "a save is in flight"}), EXIT_CONFLICT, "'focus' rejected (HTTP 409)"),
        ((503, {"detail": "the app is still starting"}), EXIT_CONFLICT, "'focus' rejected (HTTP 503)"),
        ((404, {"detail": "No window win-x"}), EXIT_ERROR, "'focus' target not found"),
        ((400, {"detail": "bad"}), EXIT_ERROR, "'focus' rejected (HTTP 400)"),
        ((412, {"detail": "no client"}), EXIT_ERROR, "'focus' has no client"),
        # A proxy's error page rather than the shell's JSON: reported as it came.
        ((500, "<html>boom</html>"), EXIT_ERROR, "'focus' failed (HTTP 500): <html>boom</html>"),
    ],
)
def test_refusals_map_to_exit_codes(
    loopback_shell: LoopbackShell,
    layout_context: LayoutCliContext,
    capsys: pytest.CaptureFixture[str],
    response: tuple[int, dict[str, Any] | str],
    exit_code: int,
    fragment: str,
) -> None:
    loopback_shell.op_refusal = response
    assert run_layout_cli(["focus", "self"], layout_context) == exit_code
    assert fragment in capsys.readouterr().err


def test_an_unreachable_shell_is_an_error(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, capsys: pytest.CaptureFixture[str]
) -> None:
    loopback_shell.close()
    assert run_layout_cli(["focus", "self"], layout_context) == EXIT_ERROR
    assert "could not reach" in capsys.readouterr().err
    assert run_layout_cli(["desktops"], layout_context) == EXIT_ERROR
    assert "could not read the inventory" in capsys.readouterr().err


def test_every_op_carries_the_requester_in_its_body(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext
) -> None:
    """The requester rides in the body as ``{app, marker}``, posted as JSON to the op route; no header names the
    agent (the shell reads none)."""
    asking = layout_context.model_copy_update(
        to_update(layout_context.field_ref().requester, OpRequester(app=AppName("chat"), marker="agent-42"))
    )

    assert run_layout_cli(["focus", "self"], asking) == EXIT_OK

    assert loopback_shell.posted == [
        (
            LAYOUT_OP_ROUTE,
            {"op": "focus", "args": {"window": "self"}, "requester": {"app": "chat", "marker": "agent-42"}},
        )
    ]
    assert loopback_shell.posted_content_types == ["application/json"]


def test_a_read_timeout_is_an_unreachable_shell(
    layout_context: LayoutCliContext, silent_shell_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    wedged = LayoutCliContext(
        shell_url=silent_shell_url,
        apps_file=layout_context.apps_file,
        requester=None,
        registration_timeout_seconds=0.0,
        read_timeout_seconds=0.2,
        op_timeout_seconds=0.2,
    )

    assert run_layout_cli(["focus", "self"], wedged) == EXIT_ERROR
    assert "could not reach the workspace shell" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        ["context"],
        ["load", "Research"],
        ["open", "files", "--path", "/notes/", "--if-present", "new", "--desktop", "Research", "--client", "c1"],
        ["open", "terminal", "--launch", "new", "--param", "workdir=/data", "--minimized"],
        ["open", "https://example.com"],
        ["open", "files", "--beside", "self"],
        ["show", "files", "--path", "/a?view", "--showing", "/a", "--repoint", "/"],
        ["focus", "self"],
        ["minimize", "pinned"],
        ["restore", "files"],
        ["maximize", "win-0000000000000001"],
        ["close", "self"],
        ["place", "self", "--state", "snapped-right"],
        ["place", "self", "--frame", "0.1,0.1,0.5,0.5"],
        ["navigate", "self", "/b"],
        ["refresh", "self", "--client", "c1"],
        ["refresh", "--app", "files"],
        ["shortcuts"],
        ["shortcut", "set", "files", "new", "--mode", "new", "--cell", "0,1"],
        ["shortcut", "move", "files", "new", "--cell", "1,1"],
        ["shortcut", "remove", "files", "new"],
        ["wallpaper", "file", "beach"],
        ["wallpaper", "none"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_every_subcommand_posts_a_body_the_shells_own_request_models_take(
    loopback_shell: LoopbackShell, layout_context: LayoutCliContext, argv: list[str]
) -> None:
    """The command and the shell read one set of request models, so the two cannot drift apart unnoticed."""
    loopback_shell.op_answer = {**_answer([_FILES_WINDOW], _FILES_ID), "shown": "raised"}

    assert run_layout_cli(argv, layout_context) == EXIT_OK

    assert len(loopback_shell.posted) == 1
    assert describe_op_body_problem(loopback_shell.posted[0][1]) is None
