"""The shell's keybindings routes: the manifest, the catalog, and the save / discard / deliver flows."""

import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask
from flask import request as flask_request
from flask.testing import FlaskClient
from workspace_profile.overrides import load_unsent_change_values
from workspace_profile.overrides import load_user_override_values
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH

from imbue.system_interface.shell.gateway_access import ENV_GATEWAY
from imbue.system_interface.shell.gateway_access import ENV_GATEWAY_PASSWORD
from workspace_layout.identity import RequestIdentity
from imbue.system_interface.shell.keybindings_delivery import ENV_AGENT_ID
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.testing import serve_app
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_VISITOR = RequestIdentity(owner=False, user_id="visitor-1", email="v@example.com")


def _client(tmp_path: Path) -> FlaskClient:
    broadcaster = WebSocketBroadcaster()
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster)
    application = shell_application(tmp_path, inventory, broadcaster)
    return application.test_client()


def _repo_root(tmp_path: Path) -> Path:
    return tmp_path / "repo"


def _write_snapshot(tmp_path: Path, keybindings: dict[str, Any]) -> None:
    path = _repo_root(tmp_path) / SNAPSHOT_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "snapshot_format": 1,
                "account_id": "account-1",
                "owner_user_id": "owner-1",
                "written_at": "2026-10-28T00:00:00Z",
                "written_by": "web",
                "keybindings": keybindings,
            }
        ),
        encoding="utf-8",
    )


def test_the_shells_manifest_declares_the_launcher_action(tmp_path: Path) -> None:
    answered = _client(tmp_path).get("/api/keybindings/actions")

    assert answered.status_code == 200
    assert answered.get_json() == {
        "app": "system_interface",
        "actions": [
            {
                "id": "launcher-secondary-send",
                "label": "Launcher: send the typed text to a chosen chat",
                "default_chords": ["ctrl+enter", "meta+enter"],
            }
        ],
    }


def test_me_resolves_the_owners_snapshot_value(tmp_path: Path) -> None:
    _write_snapshot(tmp_path, {"system_interface": {"launcher-secondary-send": ["alt+enter"]}})
    client = _client(tmp_path)

    owner = client.get("/api/keybindings/me")
    assert owner.get_json()["chords_by_action"] == {"launcher-secondary-send": ["alt+enter"]}

    visitor = client.get("/api/keybindings/me", headers=identity_headers(_VISITOR))
    assert visitor.get_json()["chords_by_action"] == {"launcher-secondary-send": ["ctrl+enter", "meta+enter"]}


def test_the_catalog_lists_the_shell_and_skips_apps_that_serve_no_manifest(tmp_path: Path) -> None:
    # The two-app registry's rows point at addresses that answer nothing, so only
    # the shell's own actions appear; an app's manifest joins when it answers.
    answered = _client(tmp_path).get("/api/keybindings/catalog")

    assert answered.status_code == 200
    catalog = answered.get_json()
    assert [app["app"] for app in catalog["apps"]] == ["system_interface"]
    assert catalog["is_owner"] is True
    assert catalog["can_save_everywhere"] is False
    assert catalog["unsent"] == []
    (action,) = catalog["apps"][0]["actions"]
    assert action["chords"] == ["ctrl+enter", "meta+enter"]
    assert action["source"] == "default"


def test_a_workspace_save_normalizes_the_chords_into_the_requesters_override(tmp_path: Path) -> None:
    client = _client(tmp_path)

    answered = client.post(
        "/api/keybindings",
        json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["Cmd+Enter"], "scope": "workspace"},
    )

    assert answered.status_code == 200
    assert answered.get_json() == {"chords": ["meta+enter"], "delivery": None}
    # The anonymous owner's override file keys "local" (no snapshot names an owner yet).
    overrides = load_user_override_values(_repo_root(tmp_path), "local")
    assert overrides == {"keybindings": {"system_interface": {"launcher-secondary-send": ["meta+enter"]}}}
    # And the resolution answers it, with the source badge naming the layer.
    catalog = client.get("/api/keybindings/catalog").get_json()
    (action,) = catalog["apps"][0]["actions"]
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
    ],
    ids=["bad-chord", "unknown-action", "unknown-app"],
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
        json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["alt+enter"], "scope": "account"},
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
        {"app": "system_interface", "action": "launcher-secondary-send", "chords": ["alt+enter"]}
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
        if not record["is_granted"]:
            return {"detail": "not permitted"}, 403
        return {"account_id": "account-1", "keybindings": flask_request.get_json(force=True)["keybindings"]}, 200

    @gateway.post("/permission-requests")
    def file_request() -> Any:
        record["requests"].append(flask_request.get_json(force=True))
        return {"request_id": f"req-{len(record['requests'])}"}, 201

    return gateway


def test_the_everywhere_save_files_one_permission_request_and_delivers_once_granted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record: dict[str, Any] = {"puts": [], "requests": [], "is_granted": False}
    with serve_app(_fake_gateway_app(record)) as served:
        monkeypatch.setenv(ENV_GATEWAY, f"http://{served.host}:{served.port}")
        monkeypatch.setenv(ENV_GATEWAY_PASSWORD, "pw")
        monkeypatch.setenv(ENV_AGENT_ID, "agent-under-test")
        client = _client(tmp_path)
        _write_snapshot(tmp_path, {"files": {"rename": ["f3"]}})

        # The first save 403s at the gateway and files exactly one request.
        first = client.post(
            "/api/keybindings",
            json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["alt+enter"], "scope": "account"},
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
        # other assignment rides along) and clears the optimistic layers.
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
        repo_root = _repo_root(tmp_path)
        assert load_unsent_change_values(repo_root) == {}
        # The owner's override copy of the delivered value goes too: the account answers now.
        assert load_user_override_values(repo_root, "owner-1") == {}


def test_a_denied_resolution_keeps_the_saves_and_allows_a_later_refiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record: dict[str, Any] = {"puts": [], "requests": [], "is_granted": False}
    with serve_app(_fake_gateway_app(record)) as served:
        monkeypatch.setenv(ENV_GATEWAY, f"http://{served.host}:{served.port}")
        monkeypatch.setenv(ENV_GATEWAY_PASSWORD, "pw")
        monkeypatch.setenv(ENV_AGENT_ID, "agent-under-test")
        client = _client(tmp_path)

        client.post(
            "/api/keybindings",
            json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["alt+enter"], "scope": "account"},
        )
        assert len(record["requests"]) == 1

        denied = client.post("/api/keybindings/deliver", json={"resolution": "denied"})
        assert denied.get_json()["status"] == "PERMISSION_REQUIRED"
        # The change stays here until delivered or discarded (spec section 9.3).
        assert load_unsent_change_values(_repo_root(tmp_path)) != {}

        # A fresh save asks again: the denial cleared the pending marker.
        client.post(
            "/api/keybindings",
            json={"app": "system_interface", "action": "launcher-secondary-send", "chords": ["alt+enter"], "scope": "account"},
        )
        assert len(record["requests"]) == 2
