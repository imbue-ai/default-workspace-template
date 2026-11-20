"""The shell's keybindings routes: its declared actions, the catalog, and the save / discard / deliver flows."""

import json
from collections.abc import Generator
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask import request as flask_request
from flask.testing import FlaskClient
from latchkey_access.gateway import ENV_GATEWAY
from latchkey_access.gateway import ENV_GATEWAY_PASSWORD
from workspace_layout.identity import RequestIdentity
from workspace_profile.overrides import load_unsent_change_values
from workspace_profile.overrides import load_user_override_values
from workspace_profile.overrides import write_user_override_values
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH

from imbue.system_interface.shell.keybindings import save_unsent_keybinding
from imbue.system_interface.shell.keybindings import shell_keybinding_actions
from imbue.system_interface.shell.keybindings_delivery import ENV_AGENT_ID
from imbue.system_interface.shell.keybindings_delivery import KeybindingsDeliveryStore
from imbue.system_interface.shell.keybindings_delivery import settle_delivered_keybindings
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_VISITOR = RequestIdentity(owner=False, user_id="visitor-1", email="v@example.com")


# A registered app that declares a keybinding, at an address nothing answers: what an app that
# stops when no window shows it looks like while it is stopped.
_STOPPED_NOTES_ROW = """
[[apps]]
name = "notes"
url = "http://127.0.0.1:9"
program = "notes"
display_name = "Notes"
stop_when_no_windows = true

[[apps.keybindings]]
id = "save-note"
label = "Save the note"
default_chords = ["ctrl+s", "meta+s"]
"""

# A row whose default chord is off the grammar: the catalog cannot offer it for re-binding.
_OFF_GRAMMAR_ROW = """
[[apps]]
name = "sketch"
url = "http://127.0.0.1:9"
program = "sketch"

[[apps.keybindings]]
id = "undo"
label = "Undo"
default_chords = ["Control+Z"]
"""

_WORKSPACE_WIDE_ROW = """
[[apps]]
name = "sketch"
url = "http://127.0.0.1:9"
program = "sketch"

[[apps.keybindings]]
id = "open-sketch"
label = "Open sketch"
default_chords = ["alt+meta+s"]
scope = "workspace"
"""


def _client(tmp_path: Path, *extra_rows: str) -> FlaskClient:
    broadcaster = WebSocketBroadcaster()
    inventory = build_inventory(write_two_app_registry(tmp_path, *extra_rows), broadcaster)
    application = shell_application(tmp_path, inventory, broadcaster)
    return application.test_client()


def _repo_root(tmp_path: Path) -> Path:
    return tmp_path / "repo"


def _write_snapshot(tmp_path: Path, keybindings: dict[str, Any], written_at: str = "2026-10-28T00:00:00Z") -> None:
    path = _repo_root(tmp_path) / SNAPSHOT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "snapshot_format": 1,
                "account_id": "account-1",
                "owner_user_id": "owner-1",
                "written_at": written_at,
                "written_by": "web",
                "keybindings": keybindings,
            }
        ),
        encoding="utf-8",
    )


def test_the_shells_app_toml_declares_its_workspace_wide_actions_and_the_launcher_action() -> None:
    actions = [action.model_dump(mode="json") for action in shell_keybinding_actions()]

    assert [(action["id"], action["scope"]) for action in actions] == [
        ("close-window", "workspace"),
        ("next-window", "workspace"),
        ("previous-window", "workspace"),
        ("open-launcher", "workspace"),
        ("show-shortcuts", "workspace"),
        ("open-overview", "workspace"),
        ("launcher-secondary-send", "app"),
    ]
    assert actions[-1] == {
        "id": "launcher-secondary-send",
        "label": "Launcher: send the typed text to a chosen chat",
        "default_chords": ["meta+enter"],
        "scope": "app",
    }


def test_me_resolves_the_owners_snapshot_value(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, {"system_interface": {"launcher-secondary-send": ["alt+enter"]}})
    client = _client(tmp_path)

    owner = client.get("/api/keybindings/me")
    assert owner.get_json()["chords_by_action"]["launcher-secondary-send"] == ["alt+enter"]

    visitor = client.get("/api/keybindings/me", headers=identity_headers(_VISITOR))
    assert visitor.get_json()["chords_by_action"]["launcher-secondary-send"] == ["meta+enter"]


def test_the_catalog_lists_the_shell_alone_when_no_registered_app_declares_keybindings(tmp_path: Path) -> None:
    answered = _client(tmp_path).get("/api/keybindings/catalog")

    assert answered.status_code == 200
    catalog = answered.get_json()
    assert [app["app"] for app in catalog["apps"]] == ["system_interface"]
    assert catalog["is_owner"] is True
    assert catalog["can_save_everywhere"] is False
    assert catalog["unsent"] == []
    action = next(a for a in catalog["apps"][0]["actions"] if a["id"] == "launcher-secondary-send")
    assert action["chords"] == ["meta+enter"]
    assert action["source"] == "default"
    assert action["scope"] == "app"


def test_the_catalog_lists_a_stopped_apps_declared_actions_from_its_registry_row(tmp_path: Path) -> None:
    # Nothing answers at the notes row's address: the actions come from the registry, not the app.
    catalog = _client(tmp_path, _STOPPED_NOTES_ROW).get("/api/keybindings/catalog").get_json()

    assert [(app["app"], app["display_name"]) for app in catalog["apps"]] == [
        ("system_interface", "Workspace"),
        ("notes", "Notes"),
    ]
    assert catalog["apps"][1]["actions"] == [
        {
            "id": "save-note",
            "label": "Save the note",
            "default_chords": ["ctrl+s", "meta+s"],
            "chords": ["ctrl+s", "meta+s"],
            "reset_chords": ["ctrl+s", "meta+s"],
            "source": "default",
            "scope": "app",
        }
    ]


def _keybinding_row(name: str, launcher_rank: int | None) -> str:
    rank = "" if launcher_rank is None else f"launcher_rank = {launcher_rank}\n"
    return f"""
[[apps]]
name = "{name}"
url = "http://127.0.0.1:9"
{rank}
[[apps.keybindings]]
id = "act"
label = "Act"
default_chords = ["f6"]
"""


def test_the_catalog_lists_the_apps_in_the_launchers_order_not_the_registrys(tmp_path: Path) -> None:
    rows = (_keybinding_row("unranked", None), _keybinding_row("zeta", 20), _keybinding_row("alpha", 10))

    catalog = _client(tmp_path, *rows).get("/api/keybindings/catalog").get_json()

    assert [app["app"] for app in catalog["apps"]] == ["system_interface", "alpha", "zeta", "unranked"]


def test_the_catalog_answers_what_a_reset_restores_beside_the_override_in_force(tmp_path: Path) -> None:
    client = _client(tmp_path, _STOPPED_NOTES_ROW)
    client.post(
        "/api/keybindings",
        json={"app": "notes", "action": "save-note", "chords": ["ctrl+shift+s"], "scope": "workspace"},
    )

    (action,) = client.get("/api/keybindings/catalog").get_json()["apps"][1]["actions"]

    assert action["chords"] == ["ctrl+shift+s"]
    assert action["reset_chords"] == ["ctrl+s", "meta+s"]
    assert action["source"] == "workspace"


# Files' Go up as its manifest declares it now, with one default chord.
_FILES_ROW = """
[[apps]]
name = "files"
url = "http://127.0.0.1:9"
program = "files"
display_name = "Files"

[[apps.keybindings]]
id = "go-up"
label = "Go to the parent folder"
default_chords = ["meta+arrowup"]
"""


def test_the_catalog_reads_a_save_of_an_actions_superseded_default_list_as_unchanged(tmp_path: Path) -> None:
    # The owner's override holds Files' old Go up list and the account the shell's old launcher list, each in an
    # order of its own: both are the old defaults, not choices, so each row reads as the current default.
    _write_snapshot(tmp_path, {"system_interface": {"launcher-secondary-send": ["meta+enter", "ctrl+enter"]}})
    write_user_override_values(
        _repo_root(tmp_path),
        "owner-1",
        {"keybindings": {"files": {"go-up": ["alt+arrowup", "ctrl+arrowup", "meta+arrowup"]}}},
    )
    client = _client(tmp_path, _FILES_ROW)

    catalog = client.get("/api/keybindings/catalog").get_json()

    actions = {(app["app"], action["id"]): action for app in catalog["apps"] for action in app["actions"]}
    for key, default in (
        (("files", "go-up"), ["meta+arrowup"]),
        (("system_interface", "launcher-secondary-send"), ["meta+enter"]),
    ):
        assert actions[key]["chords"] == default, key
        assert actions[key]["reset_chords"] == default, key
        assert actions[key]["source"] == "default", key
    assert client.get("/api/keybindings/me").get_json()["chords_by_action"]["launcher-secondary-send"] == [
        "meta+enter"
    ]


def test_the_catalog_keeps_a_save_that_differs_from_the_superseded_default_list(tmp_path: Path) -> None:
    _write_snapshot(
        tmp_path, {"system_interface": {"launcher-secondary-send": ["ctrl+enter", "meta+enter", "alt+enter"]}}
    )
    write_user_override_values(
        _repo_root(tmp_path), "owner-1", {"keybindings": {"files": {"go-up": ["ctrl+arrowup", "meta+arrowup"]}}}
    )

    catalog = _client(tmp_path, _FILES_ROW).get("/api/keybindings/catalog").get_json()

    actions = {(app["app"], action["id"]): action for app in catalog["apps"] for action in app["actions"]}
    assert actions[("files", "go-up")]["chords"] == ["ctrl+arrowup", "meta+arrowup"]
    assert actions[("files", "go-up")]["source"] == "workspace"
    assert actions[("system_interface", "launcher-secondary-send")]["chords"] == [
        "ctrl+enter",
        "meta+enter",
        "alt+enter",
    ]
    assert actions[("system_interface", "launcher-secondary-send")]["source"] == "account"


def test_a_save_for_a_stopped_apps_action_is_accepted(tmp_path: Path) -> None:
    answered = _client(tmp_path, _STOPPED_NOTES_ROW).post(
        "/api/keybindings",
        json={"app": "notes", "action": "save-note", "chords": ["ctrl+shift+s"], "scope": "workspace"},
    )

    assert answered.status_code == 200
    assert load_user_override_values(_repo_root(tmp_path), "local") == {
        "keybindings": {"notes": {"save-note": ["ctrl+shift+s"]}}
    }


def test_the_catalog_skips_a_row_whose_default_chords_are_off_the_grammar(tmp_path: Path) -> None:
    catalog = _client(tmp_path, _STOPPED_NOTES_ROW, _OFF_GRAMMAR_ROW).get("/api/keybindings/catalog").get_json()

    assert [app["app"] for app in catalog["apps"]] == ["system_interface", "notes"]


def test_the_catalog_skips_a_row_that_claims_a_workspace_wide_action(tmp_path: Path) -> None:
    catalog = _client(tmp_path, _STOPPED_NOTES_ROW, _WORKSPACE_WIDE_ROW).get("/api/keybindings/catalog").get_json()

    assert [app["app"] for app in catalog["apps"]] == ["system_interface", "notes"]


def test_a_workspace_save_normalizes_the_chords_into_the_requesters_override(tmp_path: Path) -> None:
    client = _client(tmp_path)

    answered = client.post(
        "/api/keybindings",
        json={
            "app": "system_interface",
            "action": "launcher-secondary-send",
            "chords": ["Cmd+Enter"],
            "scope": "workspace",
        },
    )

    assert answered.status_code == 200
    assert answered.get_json() == {"chords": ["meta+enter"], "delivery": None}
    # The anonymous owner's override file keys "local" (no snapshot names an owner yet).
    overrides = load_user_override_values(_repo_root(tmp_path), "local")
    assert overrides == {"keybindings": {"system_interface": {"launcher-secondary-send": ["meta+enter"]}}}
    # And the resolution answers it, with the source badge naming the layer.
    catalog = client.get("/api/keybindings/catalog").get_json()
    action = next(a for a in catalog["apps"][0]["actions"] if a["id"] == "launcher-secondary-send")
    assert action["chords"] == ["meta+enter"]
    assert action["source"] == "workspace"


def test_a_visitors_save_lands_in_their_own_override_file_alone(tmp_path: Path) -> None:
    client = _client(tmp_path)

    answered = client.post(
        "/api/keybindings",
        headers=identity_headers(_VISITOR),
        json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["f9"], "scope": "workspace"},
    )

    assert answered.status_code == 200
    assert load_user_override_values(_repo_root(tmp_path), "visitor-1") == {
        "keybindings": {"system_interface": {"launcher-secondary-send": ["f9"]}}
    }
    assert load_user_override_values(_repo_root(tmp_path), "local") == {}


def test_a_visitor_cannot_save_everywhere(tmp_path: Path) -> None:
    answered = _client(tmp_path).post(
        "/api/keybindings",
        headers=identity_headers(_VISITOR),
        json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["f9"], "scope": "account"},
    )

    assert answered.status_code == 403


@pytest.mark.parametrize(
    ("body_patch", "detail_fragment"),
    [
        ({"chords": ["banana+x"]}, "does not spell"),
        ({"action": "no-such-action"}, "no app declares"),
        ({"app": "no-such-app"}, "no app declares"),
        ({"action": "open-launcher", "chords": ["shift+k"]}, "needs a modifier other than shift"),
        ({"chords": [f"meta+{n}" for n in range(10)] + list("abcdefg")}, "at most 16 chords"),
    ],
    ids=["bad-chord", "unknown-action", "unknown-app", "workspace-wide-without-modifier", "too-many-chords"],
)
def test_a_save_refuses_what_it_cannot_store(tmp_path: Path, body_patch: dict[str, Any], detail_fragment: str) -> None:
    body: dict[str, Any] = {
        "app": "system_interface",
        "action": "launcher-secondary-send",
        "chords": ["ctrl+enter"],
        "scope": "workspace",
    }
    body.update(body_patch)

    answered = _client(tmp_path).post("/api/keybindings", json=body)

    assert answered.status_code == 400
    assert detail_fragment in answered.get_json()["detail"]


def test_an_everywhere_save_without_a_gateway_stays_unsent(tmp_path: Path) -> None:
    client = _client(tmp_path)

    answered = client.post(
        "/api/keybindings",
        json={
            "app": "system_interface",
            "action": "launcher-secondary-send",
            "chords": ["alt+enter"],
            "scope": "account",
        },
    )

    assert answered.status_code == 200
    saved = answered.get_json()
    assert saved["delivery"]["status"] == "UNAVAILABLE"
    # The optimistic layers both carry the change (spec section 9.3)...
    repo_root = _repo_root(tmp_path)
    assert load_unsent_change_values(repo_root) == {
        "keybindings": {"system_interface": {"launcher-secondary-send": ["alt+enter"]}}
    }
    assert load_user_override_values(repo_root, "local") == {
        "keybindings": {"system_interface": {"launcher-secondary-send": ["alt+enter"]}}
    }
    # ...the catalog offers the discard...
    catalog = client.get("/api/keybindings/catalog").get_json()
    assert catalog["unsent"] == [
        {
            "app": "system_interface",
            "action": "launcher-secondary-send",
            "chords": ["alt+enter"],
            "restored_chords": ["meta+enter"],
        }
    ]
    # ...and discarding drops both layers.
    discarded = client.post(
        "/api/keybindings/discard", json={"app": "system_interface", "action": "launcher-secondary-send"}
    )
    assert discarded.status_code == 200
    assert load_unsent_change_values(repo_root) == {}
    assert load_user_override_values(repo_root, "local") == {}


def _fake_gateway_app(record: dict[str, Any]) -> Flask:
    """A stand-in gateway: 403s the keybindings PUT until a grant, records permission requests."""
    gateway = Flask("fake-gateway")

    @gateway.put("/minds-api-proxy/api/v1/workspaces/<workspace_id>/account-settings/keybindings")
    def put_keybindings(workspace_id: str) -> Any:
        record["puts"].append({"workspace_id": workspace_id, "body": flask_request.get_json(force=True)})
        if record["during_put"] is not None:
            record["during_put"]()
        if not record["is_granted"]:
            return {"detail": "not permitted"}, 403
        return {"account_id": "account-1", "keybindings": flask_request.get_json(force=True)["keybindings"]}, 200

    @gateway.post("/permission-requests")
    def file_request() -> Any:
        record["requests"].append(flask_request.get_json(force=True))
        return {"request_id": f"req-{len(record['requests'])}"}, 201

    return gateway


@contextmanager
def _served_fake_gateway(monkeypatch: pytest.MonkeyPatch) -> Generator[dict[str, Any], None, None]:
    """Serve the stand-in gateway and point the shell's environment at it; yields the gateway's record."""
    record: dict[str, Any] = {"puts": [], "requests": [], "is_granted": False, "during_put": None}
    with serve_app(_fake_gateway_app(record)) as served:
        monkeypatch.setenv(ENV_GATEWAY, f"http://{served.host}:{served.port}")
        monkeypatch.setenv(ENV_GATEWAY_PASSWORD, "pw")
        monkeypatch.setenv(ENV_AGENT_ID, "agent-under-test")
        yield record


def test_the_everywhere_save_files_one_permission_request_and_delivers_once_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {"files": {"rename": ["f3"]}})

        # The first save 403s at the gateway and files exactly one request.
        first = client.post(
            "/api/keybindings",
            json={
                "app": "system_interface",
                "action": "launcher-secondary-send",
                "chords": ["alt+enter"],
                "scope": "account",
            },
        )
        assert first.get_json()["delivery"]["status"] == "PERMISSION_PENDING"
        assert len(record["requests"]) == 1
        filed = record["requests"][0]
        assert filed["type"] == "workspace"
        assert filed["payload"] == {
            "permissions": ["minds-workspaces-account-settings"],
            "target_workspace_id": "agent-under-test",
        }

        # A retry while the request is pending files no second one.
        retried = client.post("/api/keybindings/deliver", json={})
        assert retried.get_json()["status"] == "PERMISSION_PENDING"
        assert len(record["requests"]) == 1

        # Once granted, the delivery lands the merged account value (the snapshot's
        # other assignment rides along).
        record["is_granted"] = True
        delivered = client.post("/api/keybindings/deliver", json={"resolution": "granted"})
        assert delivered.get_json()["status"] == "DELIVERED"
        assert record["puts"][-1]["workspace_id"] == "agent-under-test"
        assert record["puts"][-1]["body"] == {
            "keybindings": {
                "files": {"rename": ["f3"]},
                "system_interface": {"launcher-secondary-send": ["alt+enter"]},
            }
        }
        # The owner's override copy goes: the account holds the value now.
        repo_root = _repo_root(tmp_path)
        assert load_user_override_values(repo_root, "owner-1") == {}


def test_a_denied_resolution_keeps_the_saves_and_allows_a_later_refiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        client = _client(tmp_path)

        client.post(
            "/api/keybindings",
            json={
                "app": "system_interface",
                "action": "launcher-secondary-send",
                "chords": ["alt+enter"],
                "scope": "account",
            },
        )
        assert len(record["requests"]) == 1

        denied = client.post("/api/keybindings/deliver", json={"resolution": "denied"})
        assert denied.get_json()["status"] == "PERMISSION_REQUIRED"
        # The change stays here until delivered or discarded (spec section 9.3).
        assert load_unsent_change_values(_repo_root(tmp_path)) != {}

        # A fresh save asks again: the denial cleared the pending marker.
        client.post(
            "/api/keybindings",
            json={
                "app": "system_interface",
                "action": "launcher-secondary-send",
                "chords": ["alt+enter"],
                "scope": "account",
            },
        )
        assert len(record["requests"]) == 2


def _launcher_send(client: FlaskClient) -> dict[str, Any]:
    catalog = client.get("/api/keybindings/catalog").get_json()
    shell_app = next(app for app in catalog["apps"] if app["app"] == "system_interface")
    return next(action for action in shell_app["actions"] if action["id"] == "launcher-secondary-send")


def _save_everywhere(client: FlaskClient, chords: list[str]) -> dict[str, Any]:
    answered = client.post(
        "/api/keybindings",
        json={"app": "system_interface", "action": "launcher-secondary-send", "chords": chords, "scope": "account"},
    )
    return answered.get_json()


def _after_the_delivery() -> str:
    """A written_at the desktop would stamp on a snapshot it worked out after taking a delivery sent just now."""
    return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()


def _settle(tmp_path: Path) -> None:
    """What the shell's snapshot watch runs when the desktop rewrites the snapshot."""
    settle_delivered_keybindings(_repo_root(tmp_path), KeybindingsDeliveryStore(state_directory=tmp_path / "state"))


def test_a_delivered_save_keeps_applying_until_the_snapshot_carries_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})

        assert _save_everywhere(client, ["alt+enter"])["delivery"]["status"] == "DELIVERED"

        # The desktop has not rewritten the snapshot yet: the save still resolves, reads as the
        # account's, and is not offered as undelivered.
        me = client.get("/api/keybindings/me").get_json()
        assert me["chords_by_action"]["launcher-secondary-send"] == ["alt+enter"]
        action = _launcher_send(client)
        assert (action["chords"], action["reset_chords"], action["source"]) == (
            ["alt+enter"],
            ["alt+enter"],
            "account",
        )
        assert client.get("/api/keybindings/catalog").get_json()["unsent"] == []
        # A retry has nothing to send.
        assert client.post("/api/keybindings/deliver", json={}).get_json()["status"] == "NOTHING_TO_DELIVER"
        assert len(record["puts"]) == 1

        # A snapshot the desktop worked out before it took the save can land after it: the save still applies.
        _write_snapshot(tmp_path, {"files": {"rename": ["f3"]}}, "2026-10-28T00:00:05Z")
        _settle(tmp_path)
        assert client.get("/api/keybindings/me").get_json()["chords_by_action"]["launcher-secondary-send"] == [
            "alt+enter"
        ]

        # The first snapshot written after the delivery settles it: the unsent copy goes, the snapshot answers.
        _write_snapshot(
            tmp_path, {"system_interface": {"launcher-secondary-send": ["alt+enter"]}}, _after_the_delivery()
        )
        _settle(tmp_path)
        assert load_unsent_change_values(_repo_root(tmp_path)) == {}
        assert _launcher_send(client)["chords"] == ["alt+enter"]


def test_a_newer_snapshot_with_another_value_settles_a_delivered_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})
        _save_everywhere(client, ["alt+enter"])

        # Another computer changed the action after this delivery: the account's newer value wins.
        _write_snapshot(
            tmp_path, {"system_interface": {"launcher-secondary-send": ["ctrl+enter"]}}, _after_the_delivery()
        )
        _settle(tmp_path)

        assert load_unsent_change_values(_repo_root(tmp_path)) == {}
        assert _launcher_send(client)["chords"] == ["ctrl+enter"]


def test_saving_a_delivered_value_everywhere_again_delivers_it_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})
        _save_everywhere(client, ["alt+enter"])

        again = _save_everywhere(client, ["alt+enter"])

        assert again["delivery"]["status"] == "DELIVERED"
        assert len(record["puts"]) == 2
        # The owner's override copy is cleared again, so nothing reads as changed only here.
        assert _launcher_send(client)["source"] == "account"


def test_a_save_made_while_a_delivery_is_out_is_left_to_deliver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})
        repo_root = _repo_root(tmp_path)
        record["during_put"] = lambda: save_unsent_keybinding(
            repo_root, "owner-1", "system_interface", "launcher-secondary-send", ["ctrl+enter"]
        )

        _save_everywhere(client, ["alt+enter"])

        # The account took alt+enter, but ctrl+enter came after it: that one still waits to be delivered.
        catalog = client.get("/api/keybindings/catalog").get_json()
        assert [(entry["action"], entry["chords"]) for entry in catalog["unsent"]] == [
            ("launcher-secondary-send", ["ctrl+enter"])
        ]
        assert _launcher_send(client)["chords"] == ["ctrl+enter"]


def _me_launcher_send(client: FlaskClient) -> list[str]:
    return client.get("/api/keybindings/me").get_json()["chords_by_action"]["launcher-secondary-send"]


def test_a_snapshot_carrying_the_delivered_chords_settles_it_whatever_its_clock_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})
        _save_everywhere(client, ["alt+enter"])

        # A desktop clock running behind this machine's stamps the rewrite before the delivery.
        _write_snapshot(
            tmp_path, {"system_interface": {"launcher-secondary-send": ["alt+enter"]}}, "2026-10-28T00:00:03Z"
        )
        _settle(tmp_path)

        assert load_unsent_change_values(_repo_root(tmp_path)) == {}


def test_the_snapshot_a_delivery_was_built_on_never_settles_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        # The snapshot already carries the chords saved: a pass already under way may still land another value.
        _write_snapshot(tmp_path, {"system_interface": {"launcher-secondary-send": ["alt+enter"]}})
        _save_everywhere(client, ["alt+enter"])
        assert load_unsent_change_values(_repo_root(tmp_path)) != {}

        _write_snapshot(
            tmp_path, {"system_interface": {"launcher-secondary-send": ["ctrl+enter"]}}, "2026-10-28T00:00:05Z"
        )
        _settle(tmp_path)

        assert _me_launcher_send(client) == ["alt+enter"]


def test_a_snapshot_built_while_the_delivery_was_on_its_way_does_not_settle_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _served_fake_gateway(monkeypatch) as record:
        record["is_granted"] = True
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {})
        # The desktop works a snapshot out (without the save) before it answers the delivery.
        record["during_put"] = lambda: _write_snapshot(tmp_path, {}, datetime.now(timezone.utc).isoformat())

        _save_everywhere(client, ["alt+enter"])

        assert _me_launcher_send(client) == ["alt+enter"]
        assert load_unsent_change_values(_repo_root(tmp_path)) != {}
