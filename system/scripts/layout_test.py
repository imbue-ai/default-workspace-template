"""Tests for the agent-facing layout.py helper.

They cover what an agent depends on: how apps and windows are named (the retired spellings
and verbs are refused with the replacement), the bodies the ops post and what they print from
the shell's answer, the read commands over the inventory, and the exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from layout_testing import desktop_answer, window_json

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)

_CHAT_WINDOW = window_json("win-0000000000000001", "chat", "/?chat=agent-1", "Alice")
_FILES_WINDOW = window_json("win-0000000000000002", "files", "/notes/", "notes")


def _posted_ops(fake_shell: Any) -> list[tuple[str, dict[str, Any]]]:
    return [(body["op"], body["args"]) for path, body in fake_shell.posted if path == "/api/layout/broadcast"]


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


def test_the_requester_is_the_callers_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(layout.ENV_MNGR_AGENT_ID, raising=False)
    assert layout._requester() is None
    monkeypatch.setenv(layout.ENV_MNGR_AGENT_ID, "agent-42")
    assert layout._requester() == {"app": "chat", "marker": "agent-42"}
    # An agent the chat app created carries its chat's id, which is not its own id once a
    # chat has handed off between agents.
    monkeypatch.setenv(layout.ENV_MINDS_CHAT_ID, "agent-41")
    assert layout._requester() == {"app": "chat", "marker": "agent-41"}


@pytest.mark.parametrize(
    ("spelling", "expected_hint"),
    [
        ("app:chat?instance=agent-1", "layout.py open chat --path <path>"),
        ("app:files", "layout.py open files"),
        ("chat:alice", 'open chat --path "/?chat=<chat-id>"'),
        ("chat-terminal:alice", "back face of its chat"),
        ("terminal:terminal-3", 'open terminal --path "/?session=terminal-3"'),
        ("service:files", "layout.py open files"),
        ("service:browser?session=riley", '--path "/?session=riley"'),
        ("url:abcd1234", "layout.py open https://"),
        ("subagent:abcd", "page of the chat app"),
    ],
)
def test_the_retired_spellings_are_refused_with_the_form_to_use(
    spelling: str, expected_hint: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        layout._app_name(spelling)
    assert raised.value.code == layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert "give an app name and a path" in err and expected_hint in err
    with pytest.raises(SystemExit):
        layout._window_ref(spelling)


@pytest.mark.parametrize("verb", sorted(layout._RETIRED_VERBS))
def test_the_retired_verbs_are_refused_with_the_replacement(verb: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as raised:
        layout.main([verb, "win-0000000000000001", "--relative-to", "self"])
    assert raised.value.code == layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert f"'{verb}' is not a desktop verb" in err and layout._RETIRED_VERBS[verb] in err


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
    fake_shell: Any, capsys: pytest.CaptureFixture[str], bad_name: str, fragment: str
) -> None:
    with pytest.raises(SystemExit):
        layout._app_name(bad_name)
    assert fragment in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["open", "Foo.Bar", "--path", "/"])
    assert fake_shell.posted == []


def test_windows_are_named_by_id_self_or_app(capsys: pytest.CaptureFixture[str]) -> None:
    assert layout._window_ref("win-0123456789abcdef") == "win-0123456789abcdef"
    assert layout._window_ref("self") == "self"
    assert layout._window_ref("files") == "files"
    with pytest.raises(SystemExit):
        layout._window_ref("win 12")
    assert "not a window" in capsys.readouterr().err


# open


def test_open_waits_for_registration_then_posts_the_app_and_prints_the_window_id(
    registry: Path, fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_shell.op_answer = desktop_answer(windows=[_FILES_WINDOW], window_id=_FILES_WINDOW["id"])
    assert (
        layout.main(["open", "files", "--path", "/notes/", "--desktop", "Research", "--client", "c9"]) == layout.EXIT_OK
    )
    assert _posted_ops(fake_shell) == [
        ("open", {"app": "files", "path": "/notes/", "desktop": "Research", "client": "c9"})
    ]
    captured = capsys.readouterr()
    assert captured.out == f"{_FILES_WINDOW['id']}\n"
    assert captured.err == f"opened window {_FILES_WINDOW['id']} (files at /notes/) on desktop home for client c1\n"


def test_open_of_a_launch_path_or_a_url_posts_the_launch_and_its_params(
    registry: Path, fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # The shell answers an open with the window's id whether it opened the window or focused the one
    # already at that path; both are printed alike.
    fake_shell.op_answer = desktop_answer(windows=[_CHAT_WINDOW], window_id=_CHAT_WINDOW["id"])
    assert layout.main(["open", "terminal", "--launch", "new", "--param", "workdir=/data", "--if-present", "new"]) == 0
    assert layout.main(["open", "https://example.com/docs"]) == 0
    assert layout.main(["open", "chat"]) == 0
    assert _posted_ops(fake_shell) == [
        ("open", {"app": "terminal", "launch": "new", "params": {"workdir": "/data"}, "if_present": "new"}),
        ("open", {"app": "browser", "launch": "new", "params": {"url": "https://example.com/docs"}}),
        ("open", {"app": "chat"}),
    ]
    assert capsys.readouterr().out == f"{_CHAT_WINDOW['id']}\n" * 3


def test_open_arguments_are_refused_where_they_make_no_sense(
    registry: Path, fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit):
        layout.main(["open", "files", "--path", "/notes/", "--launch", "new"])
    assert "one or the other" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["open", "files", "--path", "notes"])
    assert "starting with '/'" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["open", "https://example.com", "--param", "url=x"])
    assert "do not apply" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["open", "terminal", "--param", "novalue"])
    assert "name=value" in capsys.readouterr().err
    assert fake_shell.posted == []


def test_open_of_an_unregistered_app_fails_without_posting(
    registry: Path, fake_shell: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(layout, "_REGISTRATION_TIMEOUT_SECONDS", 0.0)
    assert layout.main(["open", "nope"]) == layout.EXIT_ERROR
    assert fake_shell.posted == []
    assert "not registered" in capsys.readouterr().err


# the window verbs


def test_the_window_verbs_post_the_window_and_the_target(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    fake_shell.op_answer = desktop_answer(windows=[_CHAT_WINDOW], window_id=_CHAT_WINDOW["id"])
    assert layout.main(["focus", _CHAT_WINDOW["id"]]) == 0
    assert layout.main(["minimize", "self", "--client", "c2"]) == 0
    assert layout.main(["restore", "chat", "--desktop", "Research"]) == 0
    assert layout.main(["maximize", "self"]) == 0
    assert layout.main(["place", "self", "--zone", "left"]) == 0
    assert layout.main(["place", "chat", "--frame", "0,0,0.5,1"]) == 0
    assert layout.main(["navigate", "self", "/?chat=agent-2"]) == 0
    assert layout.main(["close", _CHAT_WINDOW["id"]]) == 0
    assert _posted_ops(fake_shell) == [
        ("focus", {"window": _CHAT_WINDOW["id"]}),
        ("minimize", {"window": "self", "client": "c2"}),
        ("restore", {"window": "chat", "desktop": "Research"}),
        ("maximize", {"window": "self"}),
        ("place", {"window": "self", "zone": "left"}),
        ("place", {"window": "chat", "frame": "0,0,0.5,1"}),
        ("navigate", {"window": "self", "path": "/?chat=agent-2"}),
        ("close", {"window": _CHAT_WINDOW["id"]}),
    ]
    err = capsys.readouterr().err
    assert f"focused window {_CHAT_WINDOW['id']} (chat at /?chat=agent-1) on desktop home for client c1" in err
    assert "placed window" in err and "in the left zone" in err and "at frame 0,0,0.5,1" in err
    assert "pointed window" in err and "at /?chat=agent-2" in err


def test_close_names_a_window_the_answer_no_longer_lists_by_its_id_alone(
    fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    # The desktop the shell answers a close with has the window gone, so the summary has only its id to give.
    fake_shell.op_answer = desktop_answer(windows=[], window_id=_CHAT_WINDOW["id"])
    assert layout.main(["close", _CHAT_WINDOW["id"]]) == 0
    assert capsys.readouterr().err == f"closed window {_CHAT_WINDOW['id']} on desktop home for client c1\n"


def test_place_and_navigate_check_their_arguments(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        layout.main(["place", "self"])
    assert "exactly one of" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["place", "self", "--zone", "left", "--frame", "0,0,1,1"])
    with pytest.raises(SystemExit):
        layout.main(["navigate", "self", "notes"])
    assert "starting with '/'" in capsys.readouterr().err
    assert fake_shell.posted == []


def test_refresh_reaches_one_window_or_every_page_of_an_app(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    assert layout.main(["refresh", "self"]) == 0
    fake_shell.refresh_target = None
    assert layout.main(["refresh", "--app", "files"]) == 0
    assert _posted_ops(fake_shell) == [("refresh", {"window": "self"}), ("refresh", {"app": "files"})]
    err = capsys.readouterr().err
    assert "(sent refresh to client c1)" in err and "(sent refresh to every client)" in err
    with pytest.raises(SystemExit):
        layout.main(["refresh"])
    with pytest.raises(SystemExit):
        layout.main(["refresh", "self", "--app", "files"])
    # A whole-app refresh reaches every client, so a --client or --desktop beside it is refused rather
    # than silently dropped.
    with pytest.raises(SystemExit):
        layout.main(["refresh", "--app", "files", "--client", "c2"])
    assert "do not apply" in capsys.readouterr().err


# the read commands


def test_context_and_load_ride_the_op_route(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    fake_shell.context_clients = [{"client_id": "c1", "active_desktop": "home", "is_connected": True}]
    assert layout.main(["context"]) == 0
    assert json.loads(capsys.readouterr().out) == fake_shell.context_clients
    fake_shell.op_answer = desktop_answer(desktop_id="research")
    assert layout.main(["load", "Research", "--client", "c1"]) == 0
    assert "switched client c1 onto desktop research" in capsys.readouterr().err
    assert _posted_ops(fake_shell) == [("context", {}), ("load", {"desktop": "Research", "client": "c1"})]


def test_desktops_and_list_read_the_inventory_document(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    fake_shell.inventory_apps = [
        {
            "name": "files",
            "display_name": "Files",
            "internal": False,
            "is_running": True,
            "launch_paths": [{"id": "new", "label": "New File Viewer", "path": "/", "params": []}],
            "default_shortcut": {"launch": "new", "mode": "focus"},
        },
        {"name": "owner-exec", "internal": True, "is_running": True, "launch_paths": []},
    ]
    fake_shell.inventory_desktops = [
        {
            "id": "home",
            "name": "Home",
            "wallpaper": None,
            "shortcuts": [{"target": {"kind": "launch", "app": "files", "launch": "new"}, "mode": "focus", "cell": {"column": 0, "row": 0}}],
            "windows": [_FILES_WINDOW],
            "color": "#000000",
        }
    ]
    fake_shell.inventory_clients = [
        {"id": "c1", "active_desktop": "home", "is_connected": True, "shown": [_FILES_WINDOW["id"]], "last_seen": "t"},
        {"id": "c2", "active_desktop": None, "is_connected": False, "shown": [], "last_seen": "t"},
    ]
    assert layout.main(["desktops", "--json"]) == 0
    desktops = json.loads(capsys.readouterr().out)
    assert desktops["desktops"] == [
        {
            "id": "home",
            "name": "Home",
            "wallpaper": None,
            "shortcuts": fake_shell.inventory_desktops[0]["shortcuts"],
            "windows": [
                {"id": _FILES_WINDOW["id"], "app": "files", "path": "/notes/", "title": "notes", "is_settling": False}
            ],
        }
    ]
    assert [(client["id"], client["active_desktop"], client["shown"]) for client in desktops["clients"]] == [
        ("c1", "home", [_FILES_WINDOW["id"]]),
        ("c2", None, []),
    ]
    assert layout.main(["list", "--json"]) == 0
    listing = json.loads(capsys.readouterr().out)
    # Internal apps are the shell's own; they are not listed.
    assert [app["name"] for app in listing["apps"]] == ["files"]
    assert listing["apps"][0]["launch_paths"] == fake_shell.inventory_apps[0]["launch_paths"]
    assert listing["apps"][0]["windows"] == [
        {"id": _FILES_WINDOW["id"], "app": "files", "path": "/notes/", "title": "notes", "is_settling": False, "desktop": "home"}
    ]
    assert [desktop["id"] for desktop in listing["desktops"]] == ["home"]


# shortcuts and the wallpaper


def test_shortcut_verbs_post_to_the_desktop_and_print_its_shortcuts(fake_shell: Any, capsys: pytest.CaptureFixture[str]) -> None:
    row = {"target": {"kind": "launch", "app": "docs", "launch": "open"}, "mode": "new", "cell": {"column": 1, "row": 0}}
    fake_shell.op_answer = desktop_answer(desktop_id="research", shortcuts=[row])
    assert layout.main(["shortcuts", "--desktop", "Research", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"desktop": "research", "shortcuts": [row]}
    assert layout.main(["shortcut", "set", "docs", "open", "--mode", "new", "--cell", "1,0", "--desktop", "Research"]) == 0
    assert layout.main(["shortcut", "move", "docs", "open", "--cell", "2,0"]) == 0
    assert layout.main(["shortcut", "remove", "docs", "open"]) == 0
    assert layout.main(["wallpaper", "bundled", "dunes"]) == 0
    assert layout.main(["wallpaper", "none"]) == 0
    assert _posted_ops(fake_shell) == [
        ("shortcuts", {"desktop": "Research"}),
        ("shortcut_set", {"app": "docs", "launch": "open", "mode": "new", "cell": "1,0", "desktop": "Research"}),
        ("shortcut_move", {"app": "docs", "launch": "open", "cell": "2,0"}),
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
        layout.main(["wallpaper", "sky"])
    with pytest.raises(SystemExit):
        layout.main(["wallpaper", "none", "dunes"])


# exit codes and the wire


@pytest.mark.parametrize(
    ("response", "exit_code", "fragment"),
    [
        ((409, {"detail": "a save is in flight"}), layout.EXIT_CONFLICT, "409"),
        ((503, {"detail": "the app is still starting"}), layout.EXIT_CONFLICT, "503"),
        ((404, {"detail": "No window win-x"}), layout.EXIT_ERROR, "not found"),
        ((400, {"detail": "bad"}), layout.EXIT_ERROR, "400"),
        ((412, {"detail": "no client"}), layout.EXIT_ERROR, "412"),
        # A proxy's error page rather than the shell's JSON: reported as it came.
        ((500, "<html>boom</html>"), layout.EXIT_ERROR, "failed (HTTP 500): <html>boom</html>"),
    ],
)
def test_refusals_map_to_exit_codes(
    fake_shell: Any,
    capsys: pytest.CaptureFixture[str],
    response: tuple[int, dict[str, Any] | str],
    exit_code: int,
    fragment: str,
) -> None:
    fake_shell.op_refusal = response
    assert layout.main(["focus", "self"]) == exit_code
    assert fragment in capsys.readouterr().err


def test_an_unreachable_shell_is_an_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, "http://127.0.0.1:1/")
    assert layout.main(["focus", "self"]) == layout.EXIT_ERROR
    assert "could not reach" in capsys.readouterr().err
    assert layout.main(["desktops"]) == layout.EXIT_ERROR
    assert "could not read the inventory" in capsys.readouterr().err


def test_post_layout_sends_the_requester_in_the_body(fake_shell: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The requester rides in the body as ``{app, marker}``, posted as JSON to the op route; no header names the
    agent (the shell reads none)."""
    monkeypatch.setenv(layout.ENV_MNGR_AGENT_ID, "agent-42")

    status, answer = layout._post_layout("focus", {"window": "self"})

    assert status == 200 and isinstance(answer, dict) and answer["ok"] is True
    assert fake_shell.posted == [
        (
            "/api/layout/broadcast",
            {"op": "focus", "args": {"window": "self"}, "requester": {"app": "chat", "marker": "agent-42"}},
        )
    ]
    assert fake_shell.posted_content_types == ["application/json"]


def test_a_read_timeout_is_an_unreachable_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    def timing_out_urlopen(request: urllib.request.Request, timeout: float) -> None:
        raise TimeoutError("timed out")

    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, "http://127.0.0.1:1/")
    monkeypatch.setattr(layout.urllib.request, "urlopen", timing_out_urlopen)
    assert layout._post_layout("focus", {"window": "self"}) == (-1, "timed out")
