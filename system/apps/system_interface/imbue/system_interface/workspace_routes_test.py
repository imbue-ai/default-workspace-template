import json
import stat
from pathlib import Path

import pytest
from app_schema.testing import CROSS_SITE_FETCH_HEADERS
from app_schema.testing import OWNER_IDENTITY_HEADERS
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from flask import Flask
from flask.testing import FlaskClient
from host_backup.config import RESTIC_ENV_PATH
from share_grants.document import SHARE_ENV_RELATIVE_PATH
from share_grants.document import SHARE_GRANTS_RELATIVE_PATH
from share_grants.document import SHARE_IDENTITIES_RELATIVE_PATH
from share_grants.grants import load_grants
from workspace_backup_setup.setup import PENDING_ENV_RELATIVE_PATH
from workspace_backup_setup.testing import FAKE_RESTIC_HOLDS
from workspace_backup_setup.testing import fake_restic_calls
from workspace_backup_setup.testing import release_fake_restic
from workspace_backup_setup.testing import restic_env_text
from workspace_backup_setup.testing import set_fake_restic_mode
from workspace_backup_setup.verification import BACKUP_TOML_RELATIVE_PATH
from workspace_layout.identity import IDENTITY_HEADER
from workspace_layout.identity import OTHER_ORIGIN_REFUSAL
from workspace_layout.identity import RequestIdentity
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH
from workspace_profile.timezone import USER_TIMEZONE_RELATIVE_PATH

from imbue.mngr.utils.polling import wait_for
from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.workspace_api_models import WORKSPACE_COMPATIBILITY
from imbue.system_interface.workspace_api_models import WORKSPACE_OPERATIONS
from imbue.system_interface.workspace_routes import BACKUP_CREDENTIALS_ROUTE
from imbue.system_interface.workspace_routes import BACKUP_SETUP_ROUTE
from imbue.system_interface.workspace_routes import BACKUP_VERIFICATION_ROUTE
from imbue.system_interface.workspace_routes import PROFILE_ROUTE
from imbue.system_interface.workspace_routes import SHARING_GRANTS_ROUTE
from imbue.system_interface.workspace_routes import SHARING_ROUTE
from imbue.system_interface.workspace_routes import STATUS_ROUTE
from imbue.system_interface.workspace_routes import TIMEZONE_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_TABLE
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_ENV_TEXT = restic_env_text("rest:https://backups.example/workspace-5f3c", "correct-horse-battery-staple-91f2")
# The apps of ``write_two_app_registry`` as the sharing read lists them: neither is internal.
_TWO_SHAREABLE_APPS = [
    {"name": "terminal", "label": "", "display_name": "Terminal", "is_system": False},
    {"name": "files", "label": "", "display_name": "Files", "is_system": False},
]


def _workspace_root(app: Flask) -> Path:
    return state_of(app).shell.repo_root


def _backup_state(client: FlaskClient) -> str:
    return client.get(BACKUP_SETUP_ROUTE).get_json()["state"]


# What admits a request


@pytest.mark.parametrize(("method", "path"), sorted(WORKSPACE_ROUTE_TABLE))
def test_every_workspace_route_is_refused_from_a_page_of_another_origin(
    client: FlaskClient, app: Flask, method: str, path: str
) -> None:
    response = client.open(path, method=method, headers=CROSS_SITE_FETCH_HEADERS, json={})

    assert response.status_code == 403
    assert response.get_json() == {"detail": OTHER_ORIGIN_REFUSAL}
    assert not (_workspace_root(app) / "data").exists()


def test_the_request_guard_is_ahead_of_the_status_route(client: FlaskClient) -> None:
    assert_request_guard_refuses_unknown_provenance(client, "GET", STATUS_ROUTE)


@pytest.mark.parametrize(("method", "path"), sorted(WORKSPACE_ROUTE_TABLE))
def test_every_workspace_route_refuses_an_identity_header_that_does_not_parse(
    client: FlaskClient, method: str, path: str
) -> None:
    response = client.open(path, method=method, headers={IDENTITY_HEADER: "not json"}, json={})

    assert response.status_code == 400
    assert IDENTITY_HEADER in response.get_json()["detail"]


def test_a_grantee_of_the_whole_workspace_is_admitted_as_the_owner_is(client: FlaskClient) -> None:
    grantee = RequestIdentity(owner=False, user_id="user-visitor-0001", email="visitor@example.com")

    response = client.put(TIMEZONE_ROUTE, headers=identity_headers(grantee), json={"zone": "Europe/Berlin"})

    assert response.status_code == 200
    assert response.get_json() == {"zone": "Europe/Berlin", "is_written": True}


# The status


def test_the_status_names_every_operation_and_no_version_for_a_tree_that_names_none(client: FlaskClient) -> None:
    response = client.get(STATUS_ROUTE, headers=OWNER_IDENTITY_HEADERS)

    assert response.status_code == 200
    assert response.get_json() == {
        "schema": 1,
        "template_ref": "",
        "operations": list(WORKSPACE_OPERATIONS),
        "compatibility": 1,
    }
    assert WORKSPACE_COMPATIBILITY == 1


def test_every_operation_named_is_named_once() -> None:
    assert len(set(WORKSPACE_OPERATIONS)) == len(WORKSPACE_OPERATIONS)


def test_a_preview_shell_names_no_operation_and_refuses_to_change_the_workspace(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    inventory = build_inventory(write_two_app_registry(tmp_path), broadcaster)
    preview = shell_application(tmp_path, inventory, broadcaster, is_preview=True)
    client = preview.test_client()

    status = client.get(STATUS_ROUTE).get_json()
    assert (status["operations"], status["compatibility"]) == ([], WORKSPACE_COMPATIBILITY)
    refused = client.put(TIMEZONE_ROUTE, json={"zone": "Europe/Berlin"})

    assert refused.status_code == 403
    assert refused.get_json()["code"] == "PREVIEW"
    assert not (_workspace_root(preview) / USER_TIMEZONE_RELATIVE_PATH).exists()


# Sharing


def test_reading_the_sharing_state_of_a_workspace_that_never_shared(client: FlaskClient) -> None:
    assert client.get(SHARING_ROUTE).get_json() == {
        "is_shared": False,
        "grants": None,
        "grants_error": "",
        "shareable_apps": _TWO_SHAREABLE_APPS,
    }


def test_the_sharing_state_lists_the_apps_that_can_be_shared_and_no_internal_one(
    tmp_path: Path, broadcaster: WebSocketBroadcaster
) -> None:
    registry = write_two_app_registry(
        tmp_path,
        registry_row_toml(
            "system_interface", "http://localhost:8000", is_internal=True, label="system-interface-k2j9"
        ),
        registry_row_toml(
            "getting-started",
            "http://localhost:7400",
            is_system=True,
            display_name="Getting Started",
            label="getting-started-x7q2",
        ),
        registry_row_toml("terminal-pty", "http://localhost:7682", is_internal=True, is_system=True),
        registry_row_toml("my_notes", "http://localhost:9100", display_name="My notes", label="my-notes-p4m1"),
    )
    client = shell_application(tmp_path, build_inventory(registry, broadcaster), broadcaster).test_client()

    assert client.get(SHARING_ROUTE).get_json()["shareable_apps"] == [
        *_TWO_SHAREABLE_APPS,
        {
            "name": "getting-started",
            "label": "getting-started-x7q2",
            "display_name": "Getting Started",
            "is_system": True,
        },
        {"name": "my_notes", "label": "my-notes-p4m1", "display_name": "My notes", "is_system": False},
    ]


def test_reading_the_sharing_state_answers_the_grants_and_whether_the_workspace_is_shared(
    client: FlaskClient, app: Flask
) -> None:
    root = _workspace_root(app)
    (root / SHARE_ENV_RELATIVE_PATH).parent.mkdir(parents=True)
    (root / SHARE_ENV_RELATIVE_PATH).write_text("export SHARE_WORKSPACE_DOMAIN=x\n")
    (root / SHARE_GRANTS_RELATIVE_PATH).write_text(
        '[workspace]\nusers = ["user-b", "user-a"]\nemails = []\nemail_domains = ["example.org"]\n\n'
        '[services.web]\nusers = []\nemails = ["carol@example.com"]\nemail_domains = []\n'
    )

    assert client.get(SHARING_ROUTE).get_json() == {
        "is_shared": True,
        "grants": {
            "workspace": {"users": ["user-a", "user-b"], "emails": [], "email_domains": ["example.org"]},
            "services": {"web": {"users": [], "emails": ["carol@example.com"], "email_domains": []}},
        },
        "grants_error": "",
        "shareable_apps": _TWO_SHAREABLE_APPS,
    }


def test_a_grants_file_that_cannot_be_read_is_reported_and_is_not_replaced(client: FlaskClient, app: Flask) -> None:
    (_workspace_root(app) / SHARE_GRANTS_RELATIVE_PATH).mkdir(parents=True)

    assert client.get(SHARING_ROUTE).get_json() == {
        "is_shared": False,
        "grants": None,
        "grants_error": "UNREADABLE",
        "shareable_apps": _TWO_SHAREABLE_APPS,
    }
    refused = client.put(SHARING_GRANTS_ROUTE, json={"grants": {"workspace": {"users": ["user-1"]}}})

    assert refused.status_code == 500
    assert refused.get_json()["code"] == "GRANTS_UNREADABLE"
    assert (_workspace_root(app) / SHARE_GRANTS_RELATIVE_PATH).is_dir()


def test_replacing_the_grants_stores_the_normalized_document_and_the_identities(
    client: FlaskClient, app: Flask
) -> None:
    response = client.put(
        SHARING_GRANTS_ROUTE,
        json={
            "grants": {
                "workspace": {"users": [" user-1 "], "emails": ["Bob@Example.com"], "email_domains": ["@Example.org"]},
                "services": {"web": {"emails": ["carol@example.com"]}},
            },
            "identities": {
                "user-1": {"email": "erin@example.com", "display_name": "Erin"},
                "user-2": {"email": None, "display_name": None},
            },
        },
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "grants": {
            "workspace": {"users": ["user-1"], "emails": ["bob@example.com"], "email_domains": ["example.org"]},
            "services": {"web": {"users": [], "emails": ["carol@example.com"], "email_domains": []}},
        }
    }
    root = _workspace_root(app)
    stored = load_grants(root / SHARE_GRANTS_RELATIVE_PATH)
    assert stored.workspace.emails == {"bob@example.com"}
    assert stored.services["web"].emails == {"carol@example.com"}
    assert json.loads((root / SHARE_IDENTITIES_RELATIVE_PATH).read_text())["identities"] == {
        "user-1": {"email": "erin@example.com", "display_name": "Erin"},
        "user-2": {"email": None, "display_name": None},
    }
    assert stat.S_IMODE((root / SHARE_GRANTS_RELATIVE_PATH).stat().st_mode) == 0o600
    assert client.get(SHARING_ROUTE).get_json()["grants"] == response.get_json()["grants"]


def test_a_grants_document_whose_shape_is_wrong_is_refused_entry_by_entry_and_changes_no_file(
    client: FlaskClient, app: Flask
) -> None:
    root = _workspace_root(app)
    client.put(SHARING_GRANTS_ROUTE, json={"grants": {"workspace": {"emails": ["bob@example.com"]}}})
    grants_before = (root / SHARE_GRANTS_RELATIVE_PATH).read_text()
    identities_before = (root / SHARE_IDENTITIES_RELATIVE_PATH).read_text()

    refused = client.put(
        SHARING_GRANTS_ROUTE,
        json={
            "grants": {"workspace": {"emails": ["bob@example.com", "not an address"], "email_domains": ["nodot"]}},
            "identities": {"user-9": {"email": "new@example.com", "display_name": "New"}},
        },
    )

    assert refused.status_code == 400
    assert refused.get_json() == {
        "detail": "The grants document carries entries that cannot be granted.",
        "code": "INVALID_GRANTS",
        "refusals": [
            {
                "scope": "workspace",
                "kind": "email",
                "value": "not an address",
                "message": "not an address is not an email address.",
            },
            {"scope": "workspace", "kind": "email_domain", "value": "nodot", "message": "nodot is not a domain."},
        ],
    }
    assert (root / SHARE_GRANTS_RELATIVE_PATH).read_text() == grants_before
    assert (root / SHARE_IDENTITIES_RELATIVE_PATH).read_text() == identities_before


def test_who_an_account_may_share_with_is_not_this_routes_to_decide(client: FlaskClient) -> None:
    response = client.put(
        SHARING_GRANTS_ROUTE, json={"grants": {"workspace": {"email_domains": ["gmail.com", "someone-elses.example"]}}}
    )

    assert response.status_code == 200
    assert response.get_json()["grants"]["workspace"]["email_domains"] == ["gmail.com", "someone-elses.example"]


def test_a_grant_in_force_is_not_refused_by_a_later_save(client: FlaskClient, app: Flask) -> None:
    grants_path = _workspace_root(app) / SHARE_GRANTS_RELATIVE_PATH
    grants_path.parent.mkdir(parents=True)
    grants_path.write_text('[workspace]\nusers = []\nemails = ["legacy-entry"]\nemail_domains = []\n')

    response = client.put(
        SHARING_GRANTS_ROUTE, json={"grants": {"workspace": {"emails": ["legacy-entry", "new@example.com"]}}}
    )

    assert response.status_code == 200
    assert response.get_json()["grants"]["workspace"]["emails"] == ["legacy-entry", "new@example.com"]


def test_a_body_of_another_shape_is_refused_with_the_routes_own_error_object(client: FlaskClient) -> None:
    refused = client.put(SHARING_GRANTS_ROUTE, json={"grants": {"workspace": {"users": "user-1"}}})

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert "users" in refused.get_json()["detail"]


# The profile snapshot and the timezone


def test_the_profile_snapshot_is_written_as_it_was_sent(client: FlaskClient, app: Flask) -> None:
    document = {
        "snapshot_format": 7,
        "account_id": "account-123",
        "owner_user_id": "account-123",
        "written_at": "2026-11-24T00:00:00+00:00",
        "written_by": "web",
        "keybindings": {"shell": {"open": ["meta+k"]}},
        "a_setting_this_template_does_not_know": [1, 2, 3],
    }

    response = client.put(PROFILE_ROUTE, json=document)

    assert response.status_code == 200
    assert response.get_json() == {"is_written": True}
    assert json.loads((_workspace_root(app) / SNAPSHOT_RELATIVE_PATH).read_text()) == document


def test_a_profile_snapshot_that_only_differs_in_when_it_was_written_leaves_the_stored_one_alone(
    client: FlaskClient, app: Flask
) -> None:
    """A client sends the snapshot whenever it opens the workspace, and the shell watches the file."""
    document = {
        "snapshot_format": 1,
        "account_id": "account-123",
        "owner_user_id": "account-123",
        "written_at": "2026-11-24T00:00:00+00:00",
        "written_by": "web",
        "keybindings": {"shell": {"open": ["meta+k"]}},
    }
    snapshot_path = _workspace_root(app) / SNAPSHOT_RELATIVE_PATH
    client.put(PROFILE_ROUTE, json=document)
    written = snapshot_path.stat()

    reopened = client.put(PROFILE_ROUTE, json={**document, "written_at": "2026-11-25T09:30:00+00:00"})

    assert reopened.status_code == 200
    assert reopened.get_json() == {"is_written": False}
    assert (snapshot_path.stat().st_ino, snapshot_path.stat().st_mtime_ns) == (written.st_ino, written.st_mtime_ns)
    assert json.loads(snapshot_path.read_text())["written_at"] == "2026-11-24T00:00:00+00:00"

    changed = {**document, "written_at": "2026-11-25T09:31:00+00:00", "keybindings": {"shell": {"open": ["meta+j"]}}}
    rebound = client.put(PROFILE_ROUTE, json=changed)

    assert rebound.get_json() == {"is_written": True}
    assert json.loads(snapshot_path.read_text()) == changed


@pytest.mark.parametrize("snapshot_format", [None, "1", 1.5, True])
def test_a_profile_snapshot_without_a_whole_number_format_is_refused(
    client: FlaskClient, app: Flask, snapshot_format: object
) -> None:
    refused = client.put(PROFILE_ROUTE, json={"snapshot_format": snapshot_format, "account_id": "account-123"})

    assert refused.status_code == 400
    assert refused.get_json()["code"] == "INVALID_REQUEST"
    assert not (_workspace_root(app) / SNAPSHOT_RELATIVE_PATH).exists()


def test_the_timezone_is_written_once_unless_the_request_replaces_it(client: FlaskClient, app: Flask) -> None:
    assert client.put(TIMEZONE_ROUTE, json={"zone": "Europe/Berlin"}).get_json() == {
        "zone": "Europe/Berlin",
        "is_written": True,
    }
    assert client.put(TIMEZONE_ROUTE, json={"zone": "America/New_York"}).get_json() == {
        "zone": "Europe/Berlin",
        "is_written": False,
    }
    assert client.put(TIMEZONE_ROUTE, json={"zone": "America/New_York", "is_replacing": True}).get_json() == {
        "zone": "America/New_York",
        "is_written": True,
    }
    assert (_workspace_root(app) / USER_TIMEZONE_RELATIVE_PATH).read_text() == "America/New_York\n"


def test_a_name_that_is_not_a_timezone_is_refused(client: FlaskClient, app: Flask) -> None:
    refused = client.put(TIMEZONE_ROUTE, json={"zone": "Mars/Olympus_Mons"})

    assert refused.status_code == 400
    assert refused.get_json() == {"detail": "'Mars/Olympus_Mons' is not a timezone", "code": "INVALID_TIMEZONE"}
    assert not (_workspace_root(app) / USER_TIMEZONE_RELATIVE_PATH).exists()


# Backups


def test_backup_credentials_are_answered_at_once_and_written_when_the_repository_is_ready(
    client: FlaskClient, app: Flask, fake_restic_control: Path
) -> None:
    root = _workspace_root(app)
    set_fake_restic_mode(fake_restic_control, FAKE_RESTIC_HOLDS)

    answered = client.put(BACKUP_CREDENTIALS_ROUTE, json={"env_text": _ENV_TEXT})

    assert answered.status_code == 202
    assert answered.get_json()["state"] == "INITIALIZING"
    assert "env_text" not in answered.get_data(as_text=True)
    wait_for(lambda: len(fake_restic_calls(fake_restic_control)) == 1, error_message="restic was never run")
    assert _backup_state(client) == "INITIALIZING"
    assert not (root / RESTIC_ENV_PATH).exists()
    refused = client.put(BACKUP_CREDENTIALS_ROUTE, json={"env_text": _ENV_TEXT})
    assert refused.status_code == 409
    assert refused.get_json()["code"] == "SETUP_RUNNING"
    assert client.delete(BACKUP_CREDENTIALS_ROUTE).status_code == 409

    release_fake_restic(fake_restic_control)
    wait_for(lambda: _backup_state(client) == "READY", error_message="the backup setup never became ready")

    assert (root / RESTIC_ENV_PATH).read_text() == _ENV_TEXT
    assert stat.S_IMODE((root / RESTIC_ENV_PATH).stat().st_mode) == 0o600
    assert not (root / PENDING_ENV_RELATIVE_PATH).exists()
    again = client.put(BACKUP_CREDENTIALS_ROUTE, json={"env_text": _ENV_TEXT})
    assert again.status_code == 200
    assert again.get_json()["state"] == "READY"
    assert len(fake_restic_calls(fake_restic_control)) == 1


def test_backup_credentials_that_name_no_repository_are_refused(
    client: FlaskClient, fake_restic_control: Path
) -> None:
    refused = client.put(BACKUP_CREDENTIALS_ROUTE, json={"env_text": "RESTIC_PASSWORD=only-a-password\n"})

    assert refused.status_code == 400
    assert refused.get_json() == {
        "detail": "The credentials do not set RESTIC_REPOSITORY.",
        "code": "INVALID_CREDENTIALS",
    }
    assert fake_restic_calls(fake_restic_control) == []
    assert _backup_state(client) == "NONE"


def test_removing_the_backup_credentials_moves_them_aside(
    client: FlaskClient, app: Flask, fake_restic_control: Path
) -> None:
    root = _workspace_root(app)
    client.put(BACKUP_CREDENTIALS_ROUTE, json={"env_text": _ENV_TEXT})
    wait_for(lambda: _backup_state(client) == "READY", error_message="the backup setup never became ready")

    assert client.delete(BACKUP_CREDENTIALS_ROUTE).get_json() == {"is_removed": True}

    assert not (root / RESTIC_ENV_PATH).exists()
    moved = list((root / RESTIC_ENV_PATH).parent.glob("restic.env.2*"))
    assert [path.read_text() for path in moved] == [_ENV_TEXT]
    assert _backup_state(client) == "NONE"
    assert client.delete(BACKUP_CREDENTIALS_ROUTE).get_json() == {"is_removed": False}


def test_backup_verification_is_turned_off_and_on_in_the_workspaces_own_config(
    client: FlaskClient, app: Flask
) -> None:
    backup_toml = _workspace_root(app) / BACKUP_TOML_RELATIVE_PATH
    backup_toml.parent.mkdir(parents=True)
    backup_toml.write_text("# mine\ninterval_seconds = 900\n")

    assert client.put(BACKUP_VERIFICATION_ROUTE, json={"is_enabled": False}).get_json() == {"is_enabled": False}
    assert backup_toml.read_text() == "verification_disabled = true\n# mine\ninterval_seconds = 900\n"

    assert client.put(BACKUP_VERIFICATION_ROUTE, json={"is_enabled": True}).get_json() == {"is_enabled": True}
    assert backup_toml.read_text() == "verification_disabled = false\n# mine\ninterval_seconds = 900\n"
