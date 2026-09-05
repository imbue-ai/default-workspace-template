"""Tests for the agent-facing layout.py helper.

They cover what an agent depends on: the address grammar (bare names expand, the retired
spellings are refused by name), the bodies the dock ops post, the wait-stable predicates
over the ``inspect`` shape, the relay verbs, the shortcut commands, and the exit codes.
"""

from __future__ import annotations

import importlib.util
import json
import urllib.request
from pathlib import Path
from typing import Any

import pytest

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


def _make_fake_post(
    posted: list[tuple[str, dict[str, Any]]],
    response: tuple[int, dict[str, Any] | str] = (200, {"ok": True}),
):
    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        posted.append((op, args))
        return response

    return fake_post


# ---------- the address grammar ----------


@pytest.mark.parametrize(
    ("spelling", "address"),
    [
        ("files", "app:files"),
        ("app:files", "app:files"),
        ("app:terminal?instance=terminal-2", "app:terminal?instance=terminal-2"),
        ("self", "self"),
    ],
)
def test_bare_names_expand_and_addresses_pass_through(
    spelling: str, address: str
) -> None:
    assert layout._resolve_address(spelling) == address


@pytest.mark.parametrize(
    ("spelling", "expected_hint"),
    [
        ("chat:agent-1", "app:chat?instance=agent-1"),
        ("chat-terminal:alice", "back face of its chat"),
        ("terminal:terminal-3", "app:terminal?instance=terminal-3"),
        ("service:files", "use app:files"),
        ("service:files?instance=files-2", "use app:files?instance=files-2"),
        ("service:browser?session=riley", "app:browser?instance=riley"),
        ("url:abcd1234", "phase 8"),
        ("subagent:abcd", "app:chat?instance=<parent-agent-id>.<session>"),
        ("https://example.com", "phase 8"),
    ],
)
def test_the_retired_spellings_are_refused_with_the_address_to_use(
    spelling: str, expected_hint: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        layout._resolve_address(spelling)
    assert raised.value.code == layout.EXIT_ERROR
    assert expected_hint in capsys.readouterr().err


@pytest.mark.parametrize(
    "spelling",
    [
        "app:",
        "app:files?key=1",
        "app:files?instance=",
        "not an app",
        "app:files?instance=a b",
    ],
)
def test_malformed_addresses_are_refused(spelling: str) -> None:
    with pytest.raises(SystemExit):
        layout._resolve_address(spelling)


def test_address_matching_widens_a_bare_app_to_its_instances() -> None:
    assert layout._address_matches("app:files", "app:files")
    assert layout._address_matches("app:terminal", "app:terminal?instance=terminal-1")
    assert not layout._address_matches(
        "app:terminal?instance=terminal-1", "app:terminal?instance=terminal-2"
    )
    assert not layout._address_matches("app:term", "app:terminal?instance=terminal-1")


# ---------- the dock ops ----------


def test_open_waits_for_registration_then_posts_the_address(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    assert (
        layout.main(["open", "files", "--new-group", "--view", "Research"])
        == layout.EXIT_OK
    )
    assert posted == [
        ("open", {"address": "app:files", "new_group": True, "view": "Research"})
    ]


def test_a_name_the_registry_could_never_hold_is_refused_without_waiting(
    registry: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    for bad_name in ("Foo.Bar", "app:Foo.Bar", "app:-leading", "a" * 33):
        with pytest.raises(SystemExit):
            layout.main(["focus", bad_name])
        err = capsys.readouterr().err
        assert "not an address" in err or "names no app" in err, err
    assert posted == []


def test_open_of_an_unregistered_app_fails_without_posting(
    registry: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_REGISTRATION_TIMEOUT_SECONDS", 0.0)
    assert layout.main(["open", "nope"]) == layout.EXIT_ERROR
    assert posted == []
    assert "not registered" in capsys.readouterr().err


def test_open_of_an_app_with_instances_prints_the_created_address(
    registry: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE)
    posted: list[tuple[str, dict[str, Any]]] = []
    layouts = iter(
        [
            {"panels": [{"address": "app:terminal?instance=terminal-1"}], "tree": None},
            {
                "panels": [
                    {"address": "app:terminal?instance=terminal-1"},
                    {"address": "app:terminal?instance=terminal-2"},
                ],
                "tree": {
                    "type": "leaf",
                    "panels": [
                        {"address": "app:terminal?instance=terminal-2", "active": True}
                    ],
                },
            },
        ]
    )

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"layout": next(layouts)}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)
    assert layout.main(["open", "terminal"]) == layout.EXIT_OK
    captured = capsys.readouterr()
    assert captured.out == "app:terminal?instance=terminal-2\n"
    assert "created app:terminal?instance=terminal-2" in captured.err
    assert posted == [
        ("open", {"address": "app:terminal", "new_group": False, "view": None})
    ]


def test_open_of_an_instance_reports_a_noop_when_it_is_docked(
    registry: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE)
    posted: list[tuple[str, dict[str, Any]]] = []
    docked = {
        "panels": [{"address": "app:terminal?instance=terminal-1"}],
        "tree": {
            "type": "leaf",
            "panels": [{"address": "app:terminal?instance=terminal-1", "active": True}],
        },
    }

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"layout": docked}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)
    assert layout.main(["open", "app:terminal?instance=terminal-1"]) == layout.EXIT_OK
    assert posted == []
    assert (
        "no change: app:terminal?instance=terminal-1 is already open"
        in capsys.readouterr().err
    )


def test_split_and_move_pass_the_anchor_and_direction_through(
    registry: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    assert (
        layout.main(
            [
                "split",
                "files",
                "--relative-to",
                "app:chat?instance=agent-1",
                "--direction",
                "within",
            ]
        )
        == 0
    )
    assert (
        layout.main(
            [
                "move",
                "app:files",
                "--relative-to",
                "self",
                "--direction",
                "below",
                "--new-group",
            ]
        )
        == 0
    )
    assert posted == [
        (
            "split",
            {
                "address": "app:files",
                "relative_to": "app:chat?instance=agent-1",
                "direction": "within",
                "ratio": 0.6,
                "new_group": False,
                "view": None,
            },
        ),
        (
            "move",
            {
                "address": "app:files",
                "relative_to": "self",
                "direction": "below",
                "new_group": True,
                "view": None,
            },
        ),
    ]


def test_within_with_new_group_is_rejected(
    registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        layout.main(["split", "files", "--direction", "within", "--new-group"])
        == layout.EXIT_ERROR
    )
    assert "--new-group is meaningless" in capsys.readouterr().err
    assert (
        layout.main(
            [
                "move",
                "files",
                "--relative-to",
                "self",
                "--direction",
                "within",
                "--new-group",
            ]
        )
        == 1
    )


def test_focus_close_maximize_restore_and_refresh_post_addresses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    assert layout.main(["focus", "app:files"]) == 0
    assert layout.main(["close", "files", "--view", "Everything"]) == 0
    assert layout.main(["maximize", "app:chat?instance=agent-1"]) == 0
    assert layout.main(["restore"]) == 0
    assert layout.main(["refresh", "files"]) == 0
    assert posted == [
        ("focus", {"address": "app:files", "view": None}),
        ("close", {"address": "app:files", "view": "Everything"}),
        ("maximize", {"address": "app:chat?instance=agent-1", "view": None}),
        ("restore", {"view": None}),
        ("refresh", {"address": "app:files"}),
    ]


def test_read_ops_pass_the_view_and_emit_the_servers_answer(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    answers = {
        "list": {
            "ok": True,
            "view_id": "alpha",
            "apps": [{"name": "files", "instances": []}],
        },
        "views": {"ok": True, "views": [{"id": "everything"}]},
        "context": {"ok": True, "clients": [{"client_id": "c1"}]},
        "load": {"ok": True, "view_id": "alpha", "target_client_id": "c1"},
    }

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        posted.append((op, args))
        return 200, answers[op]

    monkeypatch.setattr(layout, "_post_layout", fake_post)
    assert layout.main(["list", "--view", "Alpha", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"name": "files", "instances": []}]
    assert layout.main(["views", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [{"id": "everything"}]
    assert layout.main(["context"]) == 0
    assert "client_id: c1" in capsys.readouterr().out
    assert layout.main(["load", "Alpha", "--client", "c1"]) == 0
    assert "requested load of view 'alpha' on client c1" in capsys.readouterr().err
    assert posted == [
        ("list", {"view": "Alpha"}),
        ("views", {}),
        ("context", {}),
        ("load", {"view": "Alpha", "client": "c1"}),
    ]


_TREE_LAYOUT = {
    "active_panel": "g1",
    "panels": [
        {"address": "app:chat?instance=agent-1", "tab_id": "tab-1", "title": "Alice"},
        {
            "address": "app:terminal?instance=terminal-1",
            "tab_id": "tab-2",
            "title": "Terminal 1",
        },
        {"address": "app:files", "tab_id": "tab-3", "title": "Files"},
    ],
    "tree": {
        "type": "branch",
        "arrangement": "row",
        "size_ratio": 1.0,
        "children": [
            {
                "type": "leaf",
                "size_ratio": 0.4,
                "panels": [
                    {"address": "app:chat?instance=agent-1", "active": True},
                    {"address": "app:terminal?instance=terminal-1", "active": False},
                ],
            },
            {
                "type": "leaf",
                "size_ratio": 0.6,
                "panels": [{"address": "app:files", "active": True}],
            },
        ],
    },
}


def test_inspect_renders_one_line_per_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(
            [],
            (200, {"view_id": "everything", "client_id": "c1", "layout": _TREE_LAYOUT}),
        ),
    )
    assert layout.main(["inspect"]) == 0
    captured = capsys.readouterr()
    assert "(view: everything, client: c1)" in captured.err
    assert captured.out == (
        "active_panel: g1\n"
        "row size=1.0\n"
        "  [app:chat?instance=agent-1* app:terminal?instance=terminal-1] size=0.4\n"
        "  [app:files*] size=0.6\n"
    )


def test_where_shows_tab_mates_and_neighbors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        layout, "_post_layout", _make_fake_post([], (200, {"layout": _TREE_LAYOUT}))
    )
    assert layout.main(["where", "app:chat?instance=agent-1", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["title"] == "Alice"
    assert view["group"]["tabs"] == [
        "app:chat?instance=agent-1*",
        "app:terminal?instance=terminal-1",
    ]
    assert view["neighbors"] == {
        "left": [],
        "right": ["app:files*"],
        "above": [],
        "below": [],
    }
    assert layout.main(["where", "app:browser?instance=x"]) == layout.EXIT_ERROR
    assert "not currently open" in capsys.readouterr().err


def test_move_within_an_anchor_is_a_noop_when_already_tab_mates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE)
    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"layout": _TREE_LAYOUT}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)
    assert (
        layout.main(
            [
                "move",
                "app:terminal?instance=terminal-1",
                "--relative-to",
                "app:chat?instance=agent-1",
                "--direction",
                "within",
            ]
        )
        == 0
    )
    assert posted == []
    assert "already in the same group" in capsys.readouterr().err


def test_focus_requires_the_panel_to_be_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE)
    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"layout": _TREE_LAYOUT}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)
    assert layout.main(["focus", "app:browser?instance=x"]) == layout.EXIT_ERROR
    assert posted == []
    assert "is not open in the current layout" in capsys.readouterr().err


# ---------- exit codes ----------


@pytest.mark.parametrize(
    ("response", "exit_code", "fragment"),
    [
        ((-1, "connection refused"), layout.EXIT_ERROR, "could not reach"),
        (
            (
                409,
                {
                    "detail": "busy",
                    "in_flight": {"agent_id": "a", "operation": "open"},
                    "retry_after_ms": 500,
                },
            ),
            layout.EXIT_CONFLICT,
            "409",
        ),
        (
            (404, {"detail": "No registered app named 'x'"}),
            layout.EXIT_ERROR,
            "not found",
        ),
        ((400, {"detail": "bad"}), layout.EXIT_ERROR, "400"),
        ((412, {"detail": "no client"}), layout.EXIT_ERROR, "412"),
    ],
)
def test_transport_failures_map_to_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response: tuple[int, dict[str, Any] | str],
    exit_code: int,
    fragment: str,
) -> None:
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post([], response))
    assert layout.main(["focus", "app:files"]) == exit_code
    assert fragment in capsys.readouterr().err


def test_post_layout_sends_the_agent_id_in_body_and_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Response:
        status = 200

        def read(self) -> bytes:
            return b'{"ok": true}'

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

    def fake_urlopen(request: urllib.request.Request, timeout: float) -> _Response:
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data or b"{}")
        seen["header"] = request.get_header("X-mngr-agent-id")
        return _Response()

    monkeypatch.setenv(layout.ENV_MNGR_AGENT_ID, "agent-42")
    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, "http://127.0.0.1:1/")
    monkeypatch.setattr(layout.urllib.request, "urlopen", fake_urlopen)
    assert layout._post_layout("focus", {"address": "app:files"}) == (200, {"ok": True})
    assert seen == {
        "url": "http://127.0.0.1:1/api/layout/broadcast",
        "body": {
            "op": "focus",
            "args": {"address": "app:files"},
            "agent_id": "agent-42",
        },
        "header": "agent-42",
    }


# ---------- the REST-riding commands: the relay verbs and the shortcuts ----------


def test_rename_delete_and_replace_url_ride_the_relay(
    fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    assert layout.main(["rename", "app:terminal?instance=terminal-1", "Build"]) == 0
    assert layout.main(["replace-url", "app:files?instance=files-1", "/notes"]) == 0
    # The browser's location is a URL; which form an app takes is the app's own rule.
    assert (
        layout.main(["replace-url", "app:browser?instance=b1", "https://example.com"])
        == 0
    )
    assert layout.main(["delete", "app:terminal?instance=terminal-1"]) == 0
    assert fake_shell.posted == [
        ("/api/apps/terminal/instances/terminal-1/rename", {"title": "Build"}),
        ("/api/apps/files/instances/files-1/location", {"path": "/notes"}),
        ("/api/apps/browser/instances/b1/location", {"path": "https://example.com"}),
        ("/api/apps/terminal/instances/terminal-1/delete", {}),
    ]
    err = capsys.readouterr().err
    assert (
        "renamed app:terminal?instance=terminal-1 to 'Build'" in err
        and "deleted app:terminal?instance=terminal-1" in err
    )

    fake_shell.relay_refuses = True
    assert (
        layout.main(["rename", "app:terminal?instance=terminal-9", "x"])
        == layout.EXIT_ERROR
    )
    assert (
        "rename app:terminal?instance=terminal-9 refused (HTTP 404): no such instance"
        in capsys.readouterr().err
    )


def test_the_relay_verbs_need_an_instance_address(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit):
        layout.main(["rename", "files", "Docs"])
    assert "needs an instance address" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        layout.main(["replace-url", "app:files?instance=files-1", ""])
    assert "needs a path" in capsys.readouterr().err


def test_shortcuts_list_a_projects_rail_and_everythings_fixed_rows(
    fake_shell: Any, registry: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_shell.projects = [
        {
            "id": "research",
            "name": "Research",
            "tabs": [],
            "shortcuts": [{"app": "terminal", "action": "new", "mode": "new"}],
        }
    ]
    assert layout.main(["shortcuts", "--view", "Research", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "view": "research",
        "shortcuts": [{"app": "terminal", "action": "new", "mode": "new"}],
    }
    # One row per app, running its primary action: the synthesized ``open`` of a
    # single-instance app, the one action of a one-action app, and the ``default_shortcut``
    # action of an app declaring several (the chat's ``new``, not its first-declared ``subagent``).
    assert layout.main(["shortcuts", "--view", "everything", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["shortcuts"] == [
        {"app": "files", "action": "open", "mode": "focus"},
        {"app": "terminal", "action": "new", "mode": "focus"},
        {"app": "chat", "action": "new", "mode": "focus"},
    ]
    assert layout.main(["shortcuts", "--view", "Nowhere"]) == layout.EXIT_ERROR
    assert "no project named 'Nowhere'" in capsys.readouterr().err


def test_shortcuts_default_to_the_connected_clients_view(
    fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_shell.projects = [
        {"id": "alpha", "name": "Alpha", "tabs": [], "shortcuts": []}
    ]
    fake_shell.context_clients = [
        {"client_id": "c1", "is_connected": True, "active_view": "alpha"}
    ]
    assert layout.main(["shortcuts", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["view"] == "alpha"
    fake_shell.context_clients = []
    assert layout.main(["shortcuts"]) == layout.EXIT_ERROR
    assert "pass --view" in capsys.readouterr().err


def test_shortcut_set_and_remove_post_to_the_project(
    fake_shell: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_shell.projects = [
        {"id": "research", "name": "Research", "tabs": [], "shortcuts": []}
    ]
    fake_shell.shortcuts_answer = [{"app": "docs", "action": "open", "mode": "new"}]
    assert (
        layout.main(
            ["shortcut", "set", "docs", "open", "--mode", "new", "--view", "Research"]
        )
        == 0
    )
    assert (
        layout.main(["shortcut", "remove", "docs", "open", "--view", "research"]) == 0
    )
    assert fake_shell.posted == [
        (
            "/api/projects/research/shortcuts",
            {"app": "docs", "action": "open", "mode": "new"},
        ),
        ("/api/projects/research/shortcuts/remove", {"app": "docs", "action": "open"}),
    ]
    assert (
        layout.main(["shortcut", "set", "docs", "open", "--view", "Everything"])
        == layout.EXIT_ERROR
    )
    assert "Everything's rail is fixed" in capsys.readouterr().err
