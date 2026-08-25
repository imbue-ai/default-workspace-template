"""Tests for the agent-facing layout.py helper.

These tests exercise the behavior an agent depends on:

- ``list`` and ``inspect`` post to the unified loopback endpoint, filter
  reserved chrome services from ``list``, and emit YAML by default with
  ``--json`` as the escape hatch.
- ``open`` waits for service registration before posting and uses the
  ``service:`` ref shorthand.
- ``split`` / ``move`` enforce the direction enum and pass the
  ``--relative-to`` ref through.
- ``replace-url`` rejects URLs that aren't ``service:<name>...`` or
  ``https://...``.
- Each transport status (200/400/404/409/network) maps to a distinct
  exit code.
- The ``X-Mngr-Agent-Id`` header rides every request.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import tomlkit

_SCRIPT = Path(__file__).parent / "layout.py"
_spec = importlib.util.spec_from_file_location("layout", _SCRIPT)
assert _spec is not None and _spec.loader is not None
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


@pytest.fixture(autouse=True)
def _skip_wait_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass the wait-stable poll for tests that assert on broadcast args.

    Mutating ops in production block until the post-op layout state is
    observable via ``inspect``; the tests in this file mock ``_post_layout``
    and assert on exact broadcast args, which the extra ``inspect`` calls
    from wait-stable would distort. The CLI's contract for this env var is
    documented in ``system/scripts/layout.py``. Tests that *want* to exercise the
    wait-stable behavior explicitly remove this env var via monkeypatch.
    """
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")


def _write_apps_toml(path: Path, names: list[str]) -> None:
    doc = tomlkit.document()
    apps = tomlkit.aot()
    for name in names:
        entry = tomlkit.table()
        entry["name"] = name
        entry["url"] = f"http://localhost:9000/{name}"
        apps.append(entry)
    doc["apps"] = apps
    path.write_text(tomlkit.dumps(doc))


def _make_fake_post(
    posted: list[tuple[str, dict[str, Any]]],
    response: tuple[int, dict[str, Any] | str] = (200, {"ok": True}),
):
    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        posted.append((op, args))
        return response

    return fake_post


def test_list_emits_server_entries_as_yaml(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``list`` is a thin pass-through: the server (layout_ops.layout_list)
    is the single source of truth for which entries are user-facing, and
    the script prints whatever the server returns."""
    posted: list[tuple[str, dict[str, Any]]] = []
    entries = [
        {
            "ref": "service:web",
            "kind": "service",
            "display_name": "web",
            "is_open": True,
            "is_running": True,
        },
        {
            "ref": "chat:alice",
            "kind": "agent",
            "display_name": "alice",
            "is_open": False,
            "is_running": True,
        },
    ]
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "entries": entries})),
    )

    rc = layout.main(["list"])
    assert rc == 0
    assert posted == [("list", {})]
    out = capsys.readouterr().out
    assert "service:web" in out
    assert "chat:alice" in out


def test_list_passes_view_and_device_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--view`` (the ``--layout`` alias) and ``--device`` ride along in the args."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "entries": []})),
    )

    rc = layout.main(["list", "--view", "Everything", "--device", "mobile"])
    assert rc == 0
    assert posted == [("list", {"layout": "Everything", "device": "mobile"})]


def test_views_lists_the_machines_views(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``views`` is a thin pass-through of the server's view enumeration."""
    posted: list[tuple[str, dict[str, Any]]] = []
    views = [
        {
            "id": "project-1",
            "name": "Project 1",
            "is_everything": False,
            "members": ["service:web"],
            "has_desktop_content": True,
            "has_mobile_content": False,
            "clients_on": ["client-1"],
        },
        {
            "id": "everything",
            "name": "Everything",
            "is_everything": True,
            "members": [],
            "has_desktop_content": False,
            "has_mobile_content": False,
            "clients_on": [],
        },
    ]
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "views": views, "last_active_id": "project-1"})),
    )

    rc = layout.main(["views", "--json"])
    assert rc == 0
    assert posted == [("views", {})]
    parsed = json.loads(capsys.readouterr().out)
    assert parsed == {"views": views, "last_active_id": "project-1"}


def test_open_without_view_posts_no_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mutating op with no ``--view`` posts a None target: the server
    defaults it to the view the connected client is on."""
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:web", "new_group": False, "layout": None})]


def test_open_accepts_view_as_the_layout_alias(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web", "--view", "Project 1"])
    assert rc == 0
    assert posted == [("open", {"ref": "service:web", "new_group": False, "layout": "Project 1"})]


def test_list_json_emits_structured_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    entries = [
        {
            "ref": "service:web",
            "kind": "service",
            "display_name": "web",
            "is_open": True,
            "is_running": True,
        },
    ]
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "entries": entries})),
    )

    rc = layout.main(["list", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed == entries


def test_inspect_emits_layout_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    layout_obj = {"panels": [{"ref": "chat:alice"}], "tree": None}
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["inspect", "--json"])
    assert rc == 0
    assert posted == [("inspect", {})]
    assert json.loads(capsys.readouterr().out) == layout_obj


def test_open_waits_for_registration_then_posts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        ("open", {"ref": "service:web", "new_group": False, "layout": "desktop"})
    ]


def test_open_fails_when_service_not_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["other"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    monkeypatch.setattr(layout, "_REGISTRATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(layout, "_REGISTRATION_POLL_INTERVAL_SECONDS", 0.01)

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web", "--layout", "desktop"])
    assert rc == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "not registered" in err


def test_open_full_ref_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "service:web", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        ("open", {"ref": "service:web", "new_group": False, "layout": "desktop"})
    ]


def test_open_new_group_flag_sets_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--new-group`` opts out of the share-existing-group default."""
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "service:web", "--new-group", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        ("open", {"ref": "service:web", "new_group": True, "layout": "desktop"})
    ]


def test_open_chat_terminal_ref_skips_registration_and_posts_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``chat-terminal:<name>`` is a stable agent-bound ref, not a service.

    The script must accept it as a valid prefix (no service registration
    poll, no bare-name fallback to ``service:``) and post the ref through
    to the broadcast endpoint unchanged so the frontend can resolve it
    to the per-agent terminal URL.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    # No apps.toml is set up: if the script misclassified the ref
    # as ``service:chat-terminal:alice`` the registration poll would fire.

    rc = layout.main(["open", "chat-terminal:alice", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        (
            "open",
            {"ref": "chat-terminal:alice", "new_group": False, "layout": "desktop"},
        )
    ]


def test_normalize_ref_preserves_chat_terminal_prefix() -> None:
    """``chat-terminal:`` must round-trip through ``_normalize_ref`` unchanged.

    The prefix scan in ``_normalize_ref`` walks ``_REF_PREFIXES`` in
    order; if ``chat:`` came before ``chat-terminal:`` the longer form
    would never be recognized, and ``chat-terminal:alice`` would be
    accepted via the ``chat:`` branch -- silently producing a
    miscategorized ref. Ordering ``chat-terminal:`` first in the prefix
    table is the fix; this test catches a regression in that ordering.
    """
    assert layout._normalize_ref("chat-terminal:alice") == "chat-terminal:alice"
    # Sanity: the ordinary ``chat:`` form is still recognized.
    assert layout._normalize_ref("chat:alice") == "chat:alice"


def test_open_external_url_skips_registration_and_posts_bare_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bare ``https://`` target is an external-URL ref: it must NOT be
    treated as a service name (no apps.toml registration check)
    and reaches the server verbatim."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    # No apps.toml set up and no _wait_for_registration override:
    # if the URL were misclassified as a service this would fail/hang.

    rc = layout.main(["open", "https://example.com/dashboard", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        (
            "open",
            {
                "ref": "https://example.com/dashboard",
                "new_group": False,
                "layout": "desktop",
            },
        )
    ]


def test_open_terminal_prints_returned_ref_to_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``open terminal`` is the one creation path the server resolves
    synchronously: the broadcast endpoint pre-allocates the panel id and
    returns ``terminal:<hash>`` in the HTTP response so the script can
    print it. The agent then has a stable handle for follow-up ops
    without round-tripping through ``inspect``."""
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["terminal"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "ref": "terminal:abcd1234"})),
    )

    rc = layout.main(["open", "terminal", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        ("open", {"ref": "service:terminal", "new_group": False, "layout": "desktop"})
    ]
    assert capsys.readouterr().out.strip() == "terminal:abcd1234"


def test_open_without_returned_ref_emits_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Non-terminal ``open`` responses (no ``ref`` field) must leave stdout
    empty: callers parsing the script's stdout rely on it being silent
    unless the server explicitly returns a synchronously-allocated ref."""
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["web"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))

    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "web", "--layout", "desktop"])
    assert rc == 0
    assert capsys.readouterr().out == ""


def test_split_terminal_prints_returned_ref_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``split terminal`` shares the synchronous ref-return contract with
    ``open terminal`` since both go through the same allocation path."""
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post(posted, (200, {"ok": True, "ref": "terminal:beef0000"})),
    )

    rc = layout.main(
        [
            "split",
            "terminal",
            "--relative-to",
            "self",
            "--direction",
            "below",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["ref"] == "service:terminal"
    assert capsys.readouterr().out.strip() == "terminal:beef0000"


def test_open_url_prefix_alias_is_stripped(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``url:https://...`` alias normalizes to the bare URL ref."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["open", "url:https://example.com", "--layout", "desktop"])
    assert rc == 0
    assert posted == [
        (
            "open",
            {"ref": "https://example.com", "new_group": False, "layout": "desktop"},
        )
    ]


def test_split_accepts_external_url_target(monkeypatch: pytest.MonkeyPatch) -> None:
    """``split`` accepts an external ``https://`` URL as the new panel."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        ["split", "https://example.com", "--relative-to", "self", "--layout", "desktop"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["ref"] == "https://example.com"
    assert args["relative_to"] == "self"


def test_split_passes_relative_to_and_direction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    # Bypass the registration wait for this synthetic non-service ref.
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        [
            "split",
            "url:abc12345",
            "--relative-to",
            "chat:alice",
            "--direction",
            "above",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args == {
        "ref": "url:abc12345",
        "relative_to": "chat:alice",
        "direction": "above",
        "ratio": 0.6,
        "new_group": False,
        "layout": "desktop",
    }


def test_split_new_group_flag_sets_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``split --new-group`` flips the new_group payload field on."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        [
            "split",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--new-group",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["new_group"] is True


def test_move_new_group_flag_sets_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    """``move --new-group`` flips the new_group payload field on."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "right",
            "--new-group",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["new_group"] is True


def test_split_preserves_self_in_relative_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--relative-to self`` is the documented default and must reach the server verbatim."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        ["split", "service:web", "--relative-to", "self", "--layout", "desktop"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["relative_to"] == "self"


def test_split_normalizes_bare_service_in_relative_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--relative-to web`` (bare service name) must be expanded to ``service:web``."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        ["split", "service:api", "--relative-to", "web", "--layout", "desktop"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["relative_to"] == "service:web"


def test_move_preserves_self_in_relative_to(monkeypatch: pytest.MonkeyPatch) -> None:
    """``move --relative-to self`` must NOT get rewritten to ``service:self``."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "self",
            "--direction",
            "right",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["relative_to"] == "self"


def test_move_requires_known_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    with pytest.raises(SystemExit):
        layout.main(
            [
                "move",
                "service:web",
                "--relative-to",
                "chat:alice",
                "--direction",
                "diagonal",
                "--layout",
                "desktop",
            ]
        )
    assert posted == []


def test_replace_url_rejects_non_service_non_https(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    with pytest.raises(SystemExit) as exc_info:
        layout.main(
            [
                "replace-url",
                "service:web",
                "http://insecure.local/",
                "--layout",
                "desktop",
            ]
        )
    assert exc_info.value.code == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "service:<name>" in err or "https://" in err


def test_replace_url_accepts_service_shorthand(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        ["replace-url", "service:web", "service:api/health", "--layout", "desktop"]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "replace-url"
    assert args == {
        "ref": "service:web",
        "url": "service:api/health",
        "layout": "desktop",
    }


def test_refresh_posts_ref_with_service_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["refresh", "web"])
    assert rc == 0
    assert posted == [("refresh", {"ref": "service:web"})]


def test_close_normalizes_bare_service_shorthand(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["close", "web", "--layout", "desktop"])
    assert rc == 0
    assert posted == [("close", {"ref": "service:web", "layout": "desktop"})]


def test_network_failure_returns_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server-unreachable folds into the generic ``EXIT_ERROR`` -- the
    specific cause is in stderr, where wrapper scripts that care can
    surface it without needing a distinct exit code."""
    monkeypatch.setattr(
        layout, "_post_layout", lambda op, args: (-1, "Connection refused")
    )
    rc = layout.main(["refresh", "web"])
    assert rc == layout.EXIT_ERROR


def test_conflict_returns_distinct_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Mutex contention is the one error class that keeps its own exit
    code: callers may want to retry-with-backoff on conflict but not on
    any other failure, so branching has to be possible from the exit
    code alone."""
    body = {
        "detail": "Another layout op is in flight",
        "retry_after_ms": 500,
        "in_flight": {
            "agent_id": "other-agent",
            "operation": "move",
            "args": {},
            "started_at": 1700000000.0,
        },
    }
    monkeypatch.setattr(layout, "_post_layout", lambda op, args: (409, body))
    rc = layout.main(["focus", "service:web", "--layout", "desktop"])
    assert rc == layout.EXIT_CONFLICT
    assert rc != layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert "agent_id=other-agent" in err
    assert "op=move" in err


def test_not_found_folds_into_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        layout, "_post_layout", lambda op, args: (404, {"detail": "unknown ref"})
    )
    rc = layout.main(["focus", "service:nonexistent", "--layout", "desktop"])
    assert rc == layout.EXIT_ERROR


def test_bad_request_folds_into_exit_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        layout, "_post_layout", lambda op, args: (400, {"detail": "bad arg"})
    )
    rc = layout.main(["close", "service:web", "--layout", "desktop"])
    assert rc == layout.EXIT_ERROR


def test_post_layout_sends_agent_id_header_and_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end check that _post_layout emits the right URL, headers, and body shape."""
    monkeypatch.setenv(layout.ENV_MNGR_AGENT_ID, "agent-42")
    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, "http://127.0.0.1:8000")

    captured: dict[str, Any] = {}

    class _FakeResponse:
        status = 200

        def __init__(self, text: str) -> None:
            self._text = text

        def read(self) -> bytes:
            return self._text.encode("utf-8")

        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_urlopen(req: urllib.request.Request, timeout: float) -> _FakeResponse:
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _FakeResponse('{"ok": true}')

    monkeypatch.setattr(layout.urllib.request, "urlopen", fake_urlopen)

    status, body = layout._post_layout(
        "focus", {"ref": "service:web", "layout": "desktop"}
    )
    assert status == 200
    assert body == {"ok": True}
    assert captured["url"] == "http://127.0.0.1:8000/api/layout/broadcast"
    # urllib normalizes header names to title-case in header_items().
    header_names = {k.lower(): v for k, v in captured["headers"].items()}
    assert header_names.get("x-mngr-agent-id") == "agent-42"
    parsed_body = json.loads(captured["body"].decode("utf-8"))
    assert parsed_body == {
        "op": "focus",
        "args": {"ref": "service:web", "layout": "desktop"},
        "agent_id": "agent-42",
    }


# ---------- New surface: within direction, where, wait-stable, no-op, compact ----------


def test_split_within_direction_is_accepted_and_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--direction=within`` is the single-call form of "tab into the
    anchor's own group" -- it must reach the server verbatim so the
    frontend's ``isWithinDirection`` branch can route through the
    ``referenceGroup`` placement path."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        [
            "split",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "split"
    assert args["direction"] == "within"
    assert args["relative_to"] == "chat:alice"
    assert args["ref"] == "service:web"


def test_move_within_direction_is_accepted_and_passed_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The new ``within`` direction works on ``move`` too -- relocating a
    panel into another panel's group as a tab."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    op, args = posted[0]
    assert op == "move"
    assert args["direction"] == "within"


def test_split_within_with_new_group_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--new-group`` is meaningless with ``--direction=within`` (within
    tabs into the anchor's own group; a fresh group would defeat the
    point). The CLI must reject the combination before posting."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))
    monkeypatch.setattr(layout, "_wait_for_registration", lambda *a, **kw: True)

    rc = layout.main(
        [
            "split",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--new-group",
            "--layout",
            "desktop",
        ]
    )
    assert rc == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "--new-group" in err and "within" in err


def test_move_within_with_new_group_is_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--new-group",
            "--layout",
            "desktop",
        ]
    )
    assert rc == layout.EXIT_ERROR
    assert posted == []
    err = capsys.readouterr().err
    assert "--new-group" in err and "within" in err


def test_inspect_compact_default_renders_one_line_per_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Default ``inspect`` is the compact text view -- not YAML. Each leaf
    is a single bracketed tab list; ``panel_id`` is hidden (verbose-only).
    The branch header shows ``arrangement`` (``row`` / ``column``)."""
    layout_obj = {
        "active_panel": "1",
        "panels": [],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 0.4,
                    "panels": [
                        {"ref": "chat:alice", "panel_id": "chat-1", "active": True}
                    ],
                },
                {
                    "type": "leaf",
                    "size_ratio": 0.6,
                    "panels": [
                        {"ref": "service:web", "panel_id": "p-web", "active": True}
                    ],
                },
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["inspect"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "row size=1.0" in out
    assert "[chat:alice*]" in out
    assert "[service:web*]" in out
    # ``panel_id`` is verbose-only; the compact view must not leak it.
    assert "panel_id" not in out
    assert "chat-1" not in out


def test_inspect_verbose_emits_yaml_with_panel_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--verbose`` restores the previous YAML-tree-dump rendering,
    including ``panel_id`` and ``arrangement`` (the renamed field)."""
    layout_obj = {
        "active_panel": "1",
        "panels": [{"ref": "chat:alice", "panel_id": "chat-1"}],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 1.0,
                    "panels": [
                        {"ref": "chat:alice", "panel_id": "chat-1", "active": True}
                    ],
                },
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["inspect", "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "arrangement: row" in out
    assert "panel_id: chat-1" in out


def test_where_shows_tab_mates_and_cardinal_neighbors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where <ref>`` is the focused introspection verb: it locates one
    panel's group, lists its tab-mates, and reports the cardinal-neighbor
    groups derived structurally from the inspect tree."""
    layout_obj = {
        "active_panel": "g-chat",
        "panels": [
            {"ref": "chat:alice"},
            {"ref": "terminal:abc"},
            {"ref": "service:web"},
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
                        {"ref": "chat:alice", "active": True, "title": "alice"},
                        {"ref": "terminal:abc"},
                    ],
                },
                {
                    "type": "leaf",
                    "size_ratio": 0.6,
                    "panels": [{"ref": "service:web", "active": True}],
                },
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["where", "chat:alice"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "ref:" in out and "chat:alice" in out
    # Tab-mates (active tab marked with ``*``)
    assert "chat:alice*" in out and "terminal:abc" in out
    # Right neighbor is the service:web group; no left neighbor.
    assert "service:web*" in out
    # Compact format pads direction labels to 7 chars.
    assert "left    -" in out


def test_where_missing_ref_returns_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where`` on an unknown ref must fail loudly rather than silently
    rendering an empty group view."""
    layout_obj = {"active_panel": None, "panels": [], "tree": None}
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["where", "chat:nobody"])
    assert rc == layout.EXIT_ERROR
    err = capsys.readouterr().err
    assert "not currently open" in err


def test_where_emits_json_view_with_neighbors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where --json`` emits the structured view as JSON. Locks in the
    contract programmatic callers (wrapper scripts, other agents)
    depend on: ``ref`` / ``title`` / ``group.tabs`` /
    ``neighbors.{left,right,above,below}`` keys are all present, with
    cardinal directions resolved structurally from the tree."""
    layout_obj = {
        "active_panel": "g-chat",
        "panels": [
            {"ref": "chat:alice", "title": "alice"},
            {"ref": "service:web"},
        ],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 0.4,
                    "panels": [{"ref": "chat:alice", "active": True, "title": "alice"}],
                },
                {
                    "type": "leaf",
                    "size_ratio": 0.6,
                    "panels": [{"ref": "service:web", "active": True}],
                },
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["where", "chat:alice", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["ref"] == "chat:alice"
    assert parsed["title"] == "alice"
    assert parsed["group"]["tabs"] == ["chat:alice*"]
    # Right neighbor exists; left/above/below are empty in this layout.
    assert parsed["neighbors"]["right"] == ["service:web*"]
    assert parsed["neighbors"]["left"] == []
    assert parsed["neighbors"]["above"] == []
    assert parsed["neighbors"]["below"] == []


def test_where_verbose_includes_full_layout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where --verbose`` switches to a YAML rendering that includes
    the full inspect layout under ``full_layout``. The compact text-only
    columns (the ``left  -`` / ``right -`` table) must NOT appear --
    verbose is a strict superset of the structured view, not a mix."""
    layout_obj = {
        "active_panel": "g-chat",
        "panels": [{"ref": "chat:alice", "title": "alice"}],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "size_ratio": 1.0,
            "children": [
                {
                    "type": "leaf",
                    "size_ratio": 1.0,
                    "panels": [{"ref": "chat:alice", "active": True, "title": "alice"}],
                },
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["where", "chat:alice", "--verbose"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "full_layout:" in out
    # The renamed branch field is carried through verbatim.
    assert "arrangement: row" in out
    # Compact text rendering markers must NOT appear under --verbose.
    assert "ref:    chat:alice" not in out
    assert "left    -" not in out


def test_move_within_explicit_anchor_uses_share_group_predicate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``move --direction=within --relative-to=<explicit-ref>`` uses
    ``_predicate_share_group`` rather than the relaxed any-change
    fallback. The predicate fires once the moved panel and the anchor
    appear in the same leaf. Confirms the precise post-op invariant
    "ref is now a tab-mate of relative_to" is what the success path
    actually checks."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    before_layout = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}, {"ref": "chat:alice"}],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "children": [
                {"type": "leaf", "panels": [{"ref": "chat:alice"}]},
                {"type": "leaf", "panels": [{"ref": "service:web"}]},
            ],
        },
    }
    after_layout = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}, {"ref": "chat:alice"}],
        "tree": {
            "type": "leaf",
            "panels": [{"ref": "chat:alice"}, {"ref": "service:web"}],
        },
    }
    posted_op = {"done": False}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        # Every pre-op read -- the ref-existence pre-flight (_require_open)
        # and the wait-stable ``before`` snapshot -- sees the pre-op layout;
        # the post-op poll sees the after layout once the move is POSTed.
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": after_layout if posted_op["done"] else before_layout,
            }
        posted_op["done"] = True
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    # Success diff (not a timeout); predicate matched on the after layout.
    assert "moved service:web" in err
    assert "timeout" not in err


def test_move_within_explicit_anchor_emits_noop_when_already_tab_mates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the pre-op snapshot already has both refs in the same leaf,
    ``_predicate_share_group`` matches immediately and the op is reported
    as a no-op without ever POSTing the move."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    layout_already_grouped = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}, {"ref": "chat:alice"}],
        "tree": {
            "type": "leaf",
            "panels": [{"ref": "chat:alice"}, {"ref": "service:web"}],
        },
    }
    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"ok": True, "layout": layout_already_grouped}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "chat:alice",
            "--direction",
            "within",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    # The move was NOT POSTed (only inspect snapshots ran).
    assert posted == []
    err = capsys.readouterr().err
    assert "no change" in err
    assert "service:web" in err
    assert "chat:alice" in err


def test_where_handles_column_arrangement_for_above_below(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``_neighbors_in_direction`` resolves ``above`` and ``below`` against
    a ``column`` branch (children stacked top-to-bottom). The middle
    leaf has both an ``above`` and a ``below`` neighbor."""
    layout_obj = {
        "active_panel": None,
        "panels": [
            {"ref": "chat:alice"},
            {"ref": "service:web"},
            {"ref": "terminal:abc"},
        ],
        "tree": {
            "type": "branch",
            "arrangement": "column",
            "size_ratio": 1.0,
            "children": [
                {"type": "leaf", "panels": [{"ref": "chat:alice", "active": True}]},
                {"type": "leaf", "panels": [{"ref": "service:web", "active": True}]},
                {"type": "leaf", "panels": [{"ref": "terminal:abc", "active": True}]},
            ],
        },
    }
    monkeypatch.setattr(
        layout,
        "_post_layout",
        _make_fake_post([], (200, {"ok": True, "layout": layout_obj})),
    )

    rc = layout.main(["where", "service:web", "--json"])
    assert rc == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["neighbors"]["above"] == ["chat:alice*"]
    assert parsed["neighbors"]["below"] == ["terminal:abc*"]
    # No row-arrangement branch is on the path to this leaf, so left and
    # right must be empty (and not, e.g., wrap around).
    assert parsed["neighbors"]["left"] == []
    assert parsed["neighbors"]["right"] == []


def test_where_self_is_rejected_without_inspect_round_trip(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``where self`` cannot be resolved client-side (the ``self`` sentinel
    is resolved server-side using the agent-id header). The CLI rejects
    it with an actionable error pointing at the explicit ``chat:<name>``
    form, and must do so BEFORE the ``_fetch_layout()`` round-trip --
    otherwise a downed server would surface a misleading "inspect failed"
    message rather than the actionable one."""
    posted: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(layout, "_post_layout", _make_fake_post(posted))

    rc = layout.main(["where", "self"])
    assert rc == layout.EXIT_ERROR
    # No HTTP calls at all -- the rejection short-circuits before inspect.
    assert posted == []
    err = capsys.readouterr().err
    assert "'self'" in err
    assert "chat:" in err


def test_rename_emits_diff_after_observed_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A successful mutating op prints a one-line diff to stderr after the
    new state is observable via inspect. Reuses ``_run_mutating_op``'s
    wait-stable path; the env-var bypass is removed for this test."""
    # Drop the autouse bypass so the wait-stable code path runs.
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    before_layout = {
        "active_panel": None,
        "panels": [{"ref": "chat:alice", "title": "alice"}],
        "tree": None,
    }
    after_layout = {
        "active_panel": None,
        "panels": [{"ref": "chat:alice", "title": "Alice (lead)"}],
        "tree": None,
    }
    posted_op = {"done": False}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        # Pre-op reads (ref-existence pre-flight + wait-stable ``before``
        # snapshot) see the old title; the post-op poll sees the new one.
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": after_layout if posted_op["done"] else before_layout,
            }
        posted_op["done"] = True
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["rename", "chat:alice", "Alice (lead)", "--layout", "desktop"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "renamed chat:alice" in err
    assert "'alice'" in err and "'Alice (lead)'" in err


def test_rename_emits_noop_message_when_title_already_matches(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the pre-op state already satisfies the predicate, the op is a
    no-op: stderr signals it explicitly and the op is NOT posted."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": {
                    "active_panel": None,
                    "panels": [{"ref": "chat:alice", "title": "frozen"}],
                    "tree": None,
                },
            }
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["rename", "chat:alice", "frozen", "--layout", "desktop"])
    assert rc == 0
    # No-op: the mutation op was never POSTed (only the inspect snapshot).
    assert posted == []
    err = capsys.readouterr().err
    assert "no change: chat:alice is already titled 'frozen'" in err


def test_maximize_is_unobservable_and_notes_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``maximize`` / ``restore`` / ``refresh`` do not affect
    inspect-observable state -- the wait-stable path is skipped and the
    stderr message makes that explicit."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    posted: list[tuple[str, dict[str, Any]]] = []
    # ``maximize`` runs the ref-existence pre-flight (_require_open), which
    # reads ``inspect``; serve a layout that contains the ref and record
    # only the mutating broadcast in ``posted``.
    open_layout = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}],
        "tree": {"type": "leaf", "panels": [{"ref": "service:web"}]},
    }

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"ok": True, "layout": open_layout}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["maximize", "service:web", "--layout", "desktop"])
    assert rc == 0
    # Only the broadcast went out -- the unobservable op skips the
    # wait-stable poll (the pre-flight inspect is not recorded here).
    assert posted == [("maximize", {"ref": "service:web", "layout": "desktop"})]
    err = capsys.readouterr().err
    assert "no observable layout-state change" in err


def test_open_https_url_succeeds_when_url_panel_appears(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``open https://...`` must NOT predicate on the literal URL as a ref:
    the frontend creates ad-hoc URL panels with refs of the form
    ``url:<short_hash>``, so a ref-equality predicate would always
    time out. Wait-stable should match by ``url`` field instead and
    report success when the new url panel becomes visible in inspect."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    target_url = "https://example.com/dashboard"
    before_layout = {"active_panel": None, "panels": [], "tree": None}
    after_layout = {
        "active_panel": "p1",
        "panels": [
            {
                "ref": "url:abc12345",
                "panel_type": "iframe",
                "url": target_url,
                "title": "example",
            },
        ],
        "tree": {
            "type": "leaf",
            "size_ratio": 1.0,
            "panels": [{"ref": "url:abc12345", "active": True}],
        },
    }
    call_count = {"inspect": 0}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            call_count["inspect"] += 1
            return 200, {
                "ok": True,
                "layout": before_layout if call_count["inspect"] == 1 else after_layout,
            }
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["open", target_url, "--layout", "desktop"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "opened" in err
    assert "timeout" not in err


def test_open_https_url_emits_noop_when_url_already_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the requested ``https://`` URL is already open as an ad-hoc
    URL panel, ``_predicate_url_panel_present`` matches on the pre-op
    snapshot -- the CLI reports a no-op and does NOT post the op."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    target_url = "https://example.com/"
    layout_already_open = {
        "active_panel": "p1",
        "panels": [
            {
                "ref": "url:abc12345",
                "panel_type": "iframe",
                "url": target_url,
                "title": "example",
            },
        ],
        "tree": {
            "type": "leaf",
            "size_ratio": 1.0,
            "panels": [{"ref": "url:abc12345", "active": True}],
        },
    }
    posted: list[tuple[str, dict[str, Any]]] = []

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            return 200, {"ok": True, "layout": layout_already_open}
        posted.append((op, args))
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(["open", target_url, "--layout", "desktop"])
    assert rc == 0
    # No-op: the mutation op was never POSTed (only inspect snapshots).
    assert posted == []
    err = capsys.readouterr().err
    assert "no change" in err
    assert target_url in err


def test_split_https_url_uses_url_predicate_not_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``split https://...`` mirrors ``open`` -- the panel's actual ref is
    ``url:<hash>``, not the literal URL, so the wait-stable predicate
    must scan by ``url`` field."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    target_url = "https://example.com/page"
    # The anchor (``chat:alice``) must already be open for the ref-existence
    # pre-flight to pass; the URL panel only appears in the after layout.
    before_layout = {
        "active_panel": None,
        "panels": [{"ref": "chat:alice"}],
        "tree": {"type": "leaf", "panels": [{"ref": "chat:alice"}]},
    }
    after_layout = {
        "active_panel": "p1",
        "panels": [
            {"ref": "chat:alice"},
            {
                "ref": "url:def67890",
                "panel_type": "iframe",
                "url": target_url,
                "title": "example",
            },
        ],
        "tree": {
            "type": "branch",
            "arrangement": "row",
            "children": [
                {"type": "leaf", "panels": [{"ref": "chat:alice"}]},
                {
                    "type": "leaf",
                    "size_ratio": 1.0,
                    "panels": [{"ref": "url:def67890", "active": True}],
                },
            ],
        },
    }
    posted_op = {"done": False}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        # Pre-op reads see the anchor-only layout; the post-op poll sees the
        # added URL panel once the split is POSTed.
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": after_layout if posted_op["done"] else before_layout,
            }
        posted_op["done"] = True
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(
        ["split", target_url, "--relative-to", "chat:alice", "--layout", "desktop"]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "split" in err
    assert "timeout" not in err


def test_move_within_self_uses_any_change_predicate_not_share_group(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``move --direction=within --relative-to=self`` must NOT use
    ``_predicate_share_group`` (which would look for the literal ``self``
    sentinel in inspect output and never match -- causing a 5 s
    wait-stable timeout). The CLI cannot resolve ``self`` to a real ref
    client-side, so it falls back to ``_predicate_any_change`` -- the
    same relaxed predicate cardinal-direction moves already use.

    The fake inspect serves the same snapshot for the pre-op snapshot
    (taken in ``_cmd_move`` to build the any-change predicate) and the
    ``before`` snapshot in ``_run_mutating_op``, then a different
    snapshot for the post-op poll. The predicate fires on the second
    distinct layout -> success diff, not timeout."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    before_layout = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}],
        "tree": {"type": "leaf", "panels": [{"ref": "service:web"}]},
    }
    after_layout = {
        "active_panel": None,
        "panels": [{"ref": "service:web"}],
        "tree": {
            "type": "leaf",
            "panels": [{"ref": "service:web"}, {"ref": "chat:alice"}],
        },
    }
    call_count = {"inspect": 0}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        if op == "inspect":
            call_count["inspect"] += 1
            # First two inspect calls (snapshot in _cmd_move, then ``before``
            # in _run_mutating_op) return the pre-op layout so the predicate
            # is compared against a stable baseline. Subsequent polls return
            # the post-op layout to fire the predicate.
            if call_count["inspect"] <= 2:
                return 200, {"ok": True, "layout": before_layout}
            return 200, {"ok": True, "layout": after_layout}
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(
        [
            "move",
            "service:web",
            "--relative-to",
            "self",
            "--direction",
            "within",
            "--layout",
            "desktop",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    # Success diff, not a timeout error.
    assert "moved" in err
    assert "timeout" not in err


@pytest.mark.parametrize(
    "stored_url",
    [
        # Local shape: the service name prefixes the local workspace host.
        "http://api.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/health",
        # Shared shape: the same nesting rule on a longer base hostname.
        "https://api.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com/health",
    ],
)
def test_replace_url_predicate_matches_derived_service_origin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stored_url: str,
) -> None:
    """``replace-url <ref> service:<name>[/<path>]`` must recognize the
    absolute service-origin URL the frontend stores on the panel. The
    frontend derives that origin from its own ``location.host`` (which the
    script cannot know), so the wait-stable predicate compares service
    coordinates -- name plus path-and-query -- against the stored URL. A
    literal string comparison would never match and the CLI would time out
    after 5 s with an error even though the op actually succeeded."""
    monkeypatch.delenv(layout.ENV_NO_WAIT_STABLE, raising=False)

    layout_after = {
        "active_panel": "p1",
        "panels": [
            {
                "ref": "service:web",
                "panel_type": "iframe",
                "url": stored_url,
                "title": "web",
            },
        ],
        "tree": {
            "type": "leaf",
            "size_ratio": 1.0,
            "panels": [{"ref": "service:web", "active": True}],
        },
    }
    layout_before = {
        "active_panel": "p1",
        "panels": [
            {
                "ref": "service:web",
                "panel_type": "iframe",
                "url": "http://web.host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421/",
                "title": "web",
            },
        ],
        "tree": {
            "type": "leaf",
            "size_ratio": 1.0,
            "panels": [{"ref": "service:web", "active": True}],
        },
    }
    posted_op = {"done": False}

    def fake_post(op: str, args: dict[str, Any]) -> tuple[int, dict[str, Any] | str]:
        # Pre-op reads (ref-existence pre-flight + wait-stable ``before``
        # snapshot) see the old URL; the post-op poll sees the new one.
        if op == "inspect":
            return 200, {
                "ok": True,
                "layout": layout_after if posted_op["done"] else layout_before,
            }
        posted_op["done"] = True
        return 200, {"ok": True}

    monkeypatch.setattr(layout, "_post_layout", fake_post)

    rc = layout.main(
        ["replace-url", "service:web", "service:api/health", "--layout", "desktop"]
    )
    assert rc == 0
    err = capsys.readouterr().err
    # Success diff (with the canonical service-relative expectation), not a
    # timeout error.
    assert "replace-url" in err
    assert "service:api/health" in err
    assert "timeout" not in err


def test_resolve_replace_url_stays_service_relative() -> None:
    """``_resolve_replace_url`` never emits an absolute service URL: the
    frontend derives each service origin from its own ``location.host``,
    which the script cannot know, so ``service:`` shorthands stay in
    canonical service-relative form and ``https://`` URLs pass through."""
    assert layout._resolve_replace_url("service:web") == "service:web"
    assert layout._resolve_replace_url("service:web/") == "service:web"
    assert layout._resolve_replace_url("service:api/health") == "service:api/health"
    assert layout._resolve_replace_url("service:api/v1/users") == "service:api/v1/users"
    assert (
        layout._resolve_replace_url("service:browser?session=2")
        == "service:browser/?session=2"
    )
    assert layout._resolve_replace_url("https://example.com/") == "https://example.com/"


def test_service_coordinates_from_url_requires_the_workspace_coordinate() -> None:
    """The predicate's URL parser accepts both the local and shared service
    origin spellings (one nesting rule, different bases) and rejects
    non-service URLs (external panels, the bare workspace origin) so a
    ``service:`` expectation can never match them."""
    local_host = "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.localhost:8421"
    shared_host = "host-0af1b2c3d4e5f60718293a4b5c6d7e8f.user.us-east.imbueminds.com"
    assert layout._service_coordinates_from_url(f"http://api.{local_host}/health") == (
        "api",
        "/health",
    )
    assert layout._service_coordinates_from_url(f"http://web.{local_host}/") == (
        "web",
        "/",
    )
    assert layout._service_coordinates_from_url(
        f"https://api.{shared_host}/health"
    ) == ("api", "/health")
    assert layout._service_coordinates_from_url(
        f"http://browser.{local_host}/?session=2"
    ) == (
        "browser",
        "/?session=2",
    )
    assert layout._service_coordinates_from_url("https://example.com/") is None
    # The bare workspace origin is the shell itself, not a service.
    assert layout._service_coordinates_from_url(f"http://{local_host}/") is None
    # A hostname with a host-like label that is not host-<32hex> is external.
    assert layout._service_coordinates_from_url("https://host-abc.example.com/") is None


def test_service_name_from_ref_strips_query_and_path() -> None:
    # The registration check polls apps.toml for the SERVICE, so a
    # browser-session ref (service:browser?session=2) or a path ref must reduce
    # to the bare service name before lookup.
    assert layout._service_name_from_ref("service:browser?session=2") == "browser"
    assert layout._service_name_from_ref("service:web/health") == "web"
    assert layout._service_name_from_ref("service:web") == "web"


# ---------- shortcuts / shortcut set ----------


class _FakeWorkspaceRestHandler(BaseHTTPRequestHandler):
    """Serves the two REST endpoints the shortcut commands ride."""

    def log_message(self, format: str, *args: Any) -> None:
        # Keep request logging out of pytest output.
        return

    def _respond(self, status: int, body: dict[str, Any]) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        server: Any = self.server
        if self.path == "/api/projects":
            self._respond(200, {"projects": server.projects, "last_active_id": server.last_active_id})
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})

    def do_POST(self) -> None:
        server: Any = self.server
        body_length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(body_length) or b"{}")
        server.posted.append((self.path, body))
        if self.path.startswith("/api/projects/") and self.path.endswith("/shortcuts"):
            self._respond(200, {"project_id": self.path.split("/")[3], "shortcut_overrides": server.override_answer})
            return
        if self.path.startswith("/api/apps/") and self.path.endswith("/instances/allocate"):
            service_name = self.path.split("/")[3]
            if getattr(server, "allocate_refuses", False):
                self._respond(404, {"detail": f"No registered app named {service_name!r}"})
                return
            instance_name = f"{service_name}-{getattr(server, 'allocate_number', 1)}"
            self._respond(
                200,
                {
                    "name": service_name,
                    "instance": instance_name,
                    "ref": f"service:{service_name}?instance={instance_name}",
                },
            )
            return
        if self.path == "/api/layout/broadcast":
            self._respond(200, {"ok": True})
            return
        self._respond(404, {"detail": f"unknown path {self.path}"})


@pytest.fixture
def fake_workspace_rest(monkeypatch: pytest.MonkeyPatch) -> Any:
    """A workspace-server stand-in for the REST-riding shortcut commands.

    Yields the server object; tests pose registries by assigning ``projects``
    / ``last_active_id`` and read what was posted from ``posted``.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeWorkspaceRestHandler)
    server.projects = []
    server.last_active_id = None
    server.posted = []
    server.override_answer = {}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(layout.ENV_WORKSPACE_URL, f"http://127.0.0.1:{server.server_address[1]}")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _research_project(**overrides: Any) -> dict[str, Any]:
    return {
        "project_id": "research",
        "name": "Research",
        "members": ["chat:agent-1", "service:docs", "service:browser?session=2", "service:terminal"],
        "shortcut_overrides": {},
        **overrides,
    }


def test_shortcuts_lists_the_effective_rows_with_defaults(
    fake_workspace_rest: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    """Built-ins in rail order with the code-side defaults (chat -> new mode),
    then the project's pinned apps in member order -- fleets excluded, since
    the browser and terminal rows already stand for those."""
    fake_workspace_rest.projects = [_research_project()]
    fake_workspace_rest.last_active_id = "research"

    exit_code = layout.main(["shortcuts", "--json"])

    assert exit_code == 0
    data = json.loads(capsys.readouterr().out)
    assert data == {
        "view": "research",
        "shortcuts": [
            {"id": "chat", "pinned": True, "mode": "new"},
            {"id": "files", "pinned": True, "mode": "focus"},
            {"id": "browser", "pinned": True, "mode": "focus"},
            {"id": "terminal", "pinned": True, "mode": "focus"},
            {"id": "app:docs", "pinned": True, "mode": "focus"},
        ],
    }


def test_shortcuts_reads_the_stored_overrides(
    fake_workspace_rest: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_workspace_rest.projects = [
        _research_project(
            shortcut_overrides={
                "terminal": {"is_pinned": False, "mode": None},
                "chat": {"is_pinned": None, "mode": "focus"},
                "app:docs": {"mode": "new"},
            }
        )
    ]

    exit_code = layout.main(["shortcuts", "--view", "Research", "--json"])

    assert exit_code == 0
    rows = {row["id"]: row for row in json.loads(capsys.readouterr().out)["shortcuts"]}
    assert rows["terminal"] == {"id": "terminal", "pinned": False, "mode": "focus"}
    assert rows["chat"] == {"id": "chat", "pinned": True, "mode": "focus"}
    assert rows["app:docs"] == {"id": "app:docs", "pinned": True, "mode": "new"}


def test_shortcut_set_posts_to_the_ui_endpoint(fake_workspace_rest: Any) -> None:
    """The agent surface and the UI share one write path (and so one validator)."""
    fake_workspace_rest.projects = [_research_project()]

    exit_code = layout.main(["shortcut", "set", "terminal", "--unpin", "--mode", "new", "--view", "Research"])

    assert exit_code == 0
    assert fake_workspace_rest.posted == [
        ("/api/projects/research/shortcuts", {"shortcut": "terminal", "is_pinned": False, "mode": "new"})
    ]


def test_shortcut_set_refuses_everything_and_bad_ids(fake_workspace_rest: Any) -> None:
    fake_workspace_rest.projects = [_research_project()]
    fake_workspace_rest.last_active_id = "everything"

    # Everything has no project entry to store shortcut state against.
    assert layout.main(["shortcut", "set", "terminal", "--unpin"]) == 1
    # A bad id fails before any network round trip records it.
    assert layout.main(["shortcut", "set", "not-a-shortcut", "--unpin", "--view", "Research"]) == 1
    # No field to set is refused too.
    assert layout.main(["shortcut", "set", "terminal", "--view", "Research"]) == 1
    assert fake_workspace_rest.posted == []


def test_shortcut_grammar_and_defaults_stay_in_step_with_the_server_and_frontend() -> None:
    """Drift guard: the shortcut grammar and mode defaults exist in three places
    -- the server (projects.py, the validator of record), the frontend
    (models/Projects.ts), and this script's friendlier pre-validation -- and a
    flipped default that lands in only some of them would silently disagree.
    The py/py pair is compared directly; the frontend copy is pinned as text,
    the way a stub cannot be imported but can still be held to its contract.
    """
    projects = importlib.import_module("imbue.system_interface.projects")

    assert set(layout._BUILTIN_SHORTCUT_IDS) == set(projects.SHORTCUT_NAMES)
    assert tuple(layout._SHORTCUT_MODES) == tuple(projects.SHORTCUT_MODES)
    assert layout._APP_SHORTCUT_PREFIX == projects.APP_SHORTCUT_PREFIX
    for shortcut_id in (*layout._BUILTIN_SHORTCUT_IDS, "app:docs"):
        assert layout._default_shortcut_mode(shortcut_id) == projects.default_shortcut_mode(shortcut_id), shortcut_id

    frontend_projects = (
        Path(__file__).parents[1] / "apps/system_interface/frontend/src/models/Projects.ts"
    ).read_text()
    # The frontend's default rule: chat -> new, everything else -> focus.
    assert 'return shortcutId === "chat" ? "new" : "focus";' in frontend_projects
    # And its copy of the built-in shortcut list, in rail order.
    assert 'export const SHORTCUT_NAMES = ["chat", "files", "browser", "terminal"] as const;' in frontend_projects


# ---------- App instances: bare-ref matching and ``open --new`` ----------


def test_ref_matches_widens_a_bare_app_ref_to_its_instances() -> None:
    """A bare ``service:<name>`` ref is satisfied by any of the app's
    instance panels -- an app's panes are numbered instances now -- while
    everything else stays an exact match."""
    assert layout._ref_matches("service:files", "service:files?instance=files-2")
    assert layout._ref_matches("service:files", "service:files")
    assert not layout._ref_matches("service:files", "service:files2?instance=files2-1")
    assert not layout._ref_matches("service:files?instance=files-1", "service:files?instance=files-2")
    assert not layout._ref_matches("service:browser?session=2", "service:browser?session=3")
    assert not layout._ref_matches("chat:alice", "chat:alice2")
    assert layout._ref_matches("chat:alice", "chat:alice")


def test_open_new_mints_an_instance_and_opens_its_ref(
    fake_workspace_rest: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``open files --new`` mints through the allocator endpoint, opens the
    minted instance ref, and prints it to stdout so the caller can address
    the new instance without an ``inspect``."""
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["files"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")
    fake_workspace_rest.allocate_number = 2

    exit_code = layout.main(["open", "files", "--new"])

    assert exit_code == 0
    assert capsys.readouterr().out.strip() == "service:files?instance=files-2"
    posted_paths = [path for path, _body in fake_workspace_rest.posted]
    assert posted_paths == ["/api/apps/files/instances/allocate", "/api/layout/broadcast"]
    broadcast_body = fake_workspace_rest.posted[1][1]
    assert broadcast_body["op"] == "open"
    assert broadcast_body["args"]["ref"] == "service:files?instance=files-2"


def test_open_new_refuses_the_fleets_and_instance_refs(
    fake_workspace_rest: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["files", "terminal", "browser"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")

    assert layout.main(["open", "terminal", "--new"]) == layout.EXIT_ERROR
    assert layout.main(["open", "browser", "--new"]) == layout.EXIT_ERROR
    assert layout.main(["open", "service:files?instance=files-1", "--new"]) == layout.EXIT_ERROR
    assert fake_workspace_rest.posted == []


def test_open_new_surfaces_an_allocator_refusal(
    fake_workspace_rest: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    apps_file = tmp_path / "apps.toml"
    _write_apps_toml(apps_file, ["files"])
    monkeypatch.setenv(layout.ENV_APPS_FILE, str(apps_file))
    monkeypatch.setenv(layout.ENV_NO_WAIT_STABLE, "1")
    fake_workspace_rest.allocate_refuses = True

    exit_code = layout.main(["open", "files", "--new"])

    assert exit_code == layout.EXIT_ERROR
    assert "could not mint an instance" in capsys.readouterr().err
