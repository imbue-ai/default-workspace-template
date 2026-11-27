import json
import stat
import time
from pathlib import Path
from typing import Any

import pytest
from app_schema.testing import CROSS_SITE_FETCH_HEADERS
from app_schema.testing import OWNER_IDENTITY_HEADERS
from app_schema.testing import assert_request_guard_refuses_unknown_provenance
from flask import Flask
from flask.testing import FlaskClient
from host_backup.config import RESTIC_ENV_PATH
from host_backup.config import parse_restic_env_file
from share_grants.document import SHARE_ENV_RELATIVE_PATH
from share_grants.document import SHARE_GRANTS_RELATIVE_PATH
from share_grants.document import SHARE_IDENTITIES_RELATIVE_PATH
from share_grants.grants import load_grants
from workspace_backup_setup.env_files import PENDING_ENV_RELATIVE_PATH
from workspace_backup_setup.env_files import env_digest
from workspace_backup_setup.env_files import kept_envs
from workspace_backup_setup.program import BackupSetupProgram
from workspace_backup_setup.state import WORKSPACE_ID_ENV
from workspace_backup_setup.testing import FAKE_RESTIC_FINDS_A_REPOSITORY
from workspace_backup_setup.testing import IMBUE_CLOUD_REPOSITORY
from workspace_backup_setup.testing import WORKSPACE_ID
from workspace_backup_setup.testing import fake_restic_calls
from workspace_backup_setup.testing import imbue_cloud_request
from workspace_backup_setup.testing import own_storage_request
from workspace_backup_setup.testing import refuse_fake_restic_token
from workspace_backup_setup.testing import set_fake_restic_mode
from workspace_backup_setup.verification import BACKUP_TOML_RELATIVE_PATH
from workspace_bare_scripts.backup_lease import HOLDER_SETUP
from workspace_bare_scripts.backup_lease import release_lease
from workspace_bare_scripts.backup_lease import take_lease
from workspace_initial_data.status import InitialDataRecord
from workspace_initial_data.status import InitialDataStatus
from workspace_initial_data.status import applied_status
from workspace_initial_data.status import failed_status
from workspace_initial_data.status import write_initial_data_record
from workspace_layout.identity import IDENTITY_HEADER
from workspace_layout.identity import OTHER_ORIGIN_REFUSAL
from workspace_layout.identity import RequestIdentity
from workspace_profile.snapshot import SNAPSHOT_RELATIVE_PATH
from workspace_profile.timezone import USER_TIMEZONE_RELATIVE_PATH

from imbue.system_interface.app_context import state_of
from imbue.system_interface.shell.testing import build_inventory
from imbue.system_interface.shell.testing import identity_headers
from imbue.system_interface.shell.testing import registry_row_toml
from imbue.system_interface.shell.testing import shell_application
from imbue.system_interface.shell.testing import write_two_app_registry
from imbue.system_interface.workspace_api_models import WORKSPACE_COMPATIBILITY
from imbue.system_interface.workspace_api_models import WORKSPACE_OPERATIONS
from imbue.system_interface.workspace_routes import BACKUPS_ROUTE
from imbue.system_interface.workspace_routes import BACKUP_CREDENTIALS_ROUTE
from imbue.system_interface.workspace_routes import BACKUP_DESTINATION_ROUTE
from imbue.system_interface.workspace_routes import BACKUP_VERIFICATION_ROUTE
from imbue.system_interface.workspace_routes import PROFILE_ROUTE
from imbue.system_interface.workspace_routes import SHARING_GRANTS_ROUTE
from imbue.system_interface.workspace_routes import SHARING_ROUTE
from imbue.system_interface.workspace_routes import STATUS_ROUTE
from imbue.system_interface.workspace_routes import TIMEZONE_ROUTE
from imbue.system_interface.workspace_routes import WORKSPACE_ROUTE_TABLE
from imbue.system_interface.ws_broadcaster import WebSocketBroadcaster

_OWNER = RequestIdentity(owner=True, user_id="user-owner-9c21", email="owner@example.com")
_GRANTEE = RequestIdentity(owner=False, user_id="user-visitor-0001", email="visitor@example.com")
# A workspace's share address, which is where a page of another origin reaches the shell through the gateway.
_SHARE_BASE_URL = "https://shell-5f3c.ws.example"
_VISITING_AGENT = RequestIdentity(owner=False, agent_id="grant-51c0", agent_name="scout", granter="user-owner-9c21")
_STORAGE_REFUSED = "The storage does not accept the credentials this workspace holds for it."
# The apps of ``write_two_app_registry`` as the sharing read lists them: neither is internal.
_TWO_SHAREABLE_APPS = [
    {"name": "terminal", "label": "", "display_name": "Terminal", "is_system": False},
    {"name": "files", "label": "", "display_name": "Files", "is_system": False},
]


def _workspace_root(app: Flask) -> Path:
    return state_of(app).shell.repo_root


def _backups(client: FlaskClient) -> dict[str, Any]:
    return client.get(BACKUPS_ROUTE).get_json()


def _run_backup_setup(app: Flask) -> None:
    """One pass of the supervised ``backup-setup`` program over the app's workspace."""
    BackupSetupProgram(
        _workspace_root(app),
        lease_path=state_of(app).backup_lease_path,
        clock=time.time,
        workspace_id=WORKSPACE_ID,
    ).run_pass()


@pytest.fixture
def backup_workspace(app: Flask, fake_restic_control: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The app's workspace as the backup routes see it: it knows its own id, and restic is a stand-in. Answers the
    stand-in's control directory."""
    monkeypatch.setenv(WORKSPACE_ID_ENV, WORKSPACE_ID)
    return fake_restic_control


# What admits a request


@pytest.mark.parametrize(("method", "path"), sorted(WORKSPACE_ROUTE_TABLE))
@pytest.mark.parametrize(
    ("stamped", "reached_at"),
    [
        ({}, None),
        # Through the share, with a session the gateway accepted, from a page it did not vouch for.
        (identity_headers(_OWNER), _SHARE_BASE_URL),
    ],
    ids=["with-no-identity", "as-an-owner-the-gateway-did-not-vouch-for"],
)
def test_every_workspace_route_is_refused_from_a_page_of_another_origin(
    client: FlaskClient, app: Flask, method: str, path: str, stamped: dict[str, str], reached_at: str | None
) -> None:
    response = client.open(
        path, method=method, headers={**CROSS_SITE_FETCH_HEADERS, **stamped}, base_url=reached_at, json={}
    )

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
    response = client.put(TIMEZONE_ROUTE, headers=identity_headers(_GRANTEE), json={"zone": "Europe/Berlin"})

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
        "initial_data": {"state": "NONE", "detail": "", "applied_at": None},
    }
    assert WORKSPACE_COMPATIBILITY == 1


@pytest.mark.parametrize(
    "recorded",
    [
        applied_status(1_795_000_000.5),
        failed_status("timezone: zone is an IANA timezone name, and is_replacing true or false"),
    ],
)
def test_the_status_says_how_the_initial_data_the_boot_found_stands(
    client: FlaskClient, app: Flask, recorded: InitialDataStatus
) -> None:
    write_initial_data_record(_workspace_root(app), InitialDataRecord(status=recorded))

    answered = client.get(STATUS_ROUTE, headers=OWNER_IDENTITY_HEADERS).get_json()

    assert answered["initial_data"] == recorded.model_dump(mode="json")
    assert answered["schema"] == 1


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


def test_the_backup_state_of_a_workspace_nothing_was_asked_of(client: FlaskClient) -> None:
    assert _backups(client) == {
        "destination": "NONE",
        "setup": {"state": "NONE", "detail": "", "started_at": None, "finished_at": None, "retry_at": None},
        "is_configured": False,
        "is_verification_enabled": True,
        "repository": "",
        "credentials_digest": "",
        "lease": None,
    }


def test_a_destination_is_answered_at_once_and_set_up_by_the_workspace_itself(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    root = _workspace_root(app)

    answered = client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))

    assert answered.status_code == 202
    asked = answered.get_json()
    assert (asked["destination"], asked["setup"]["state"]) == ("IMBUE_CLOUD", "INITIALIZING")
    assert (asked["repository"], asked["is_configured"]) == (IMBUE_CLOUD_REPOSITORY, False)
    assert "token-a" not in answered.get_data(as_text=True)
    assert asked == _backups(client)
    assert not (root / RESTIC_ENV_PATH).exists()
    assert stat.S_IMODE((root / PENDING_ENV_RELATIVE_PATH).stat().st_mode) == 0o600

    _run_backup_setup(app)

    ready = _backups(client)
    env_text = (root / RESTIC_ENV_PATH).read_text()
    assert (ready["setup"]["state"], ready["is_configured"], ready["lease"]) == ("READY", True, None)
    assert ready["credentials_digest"] == env_digest(env_text)
    assert stat.S_IMODE((root / RESTIC_ENV_PATH).stat().st_mode) == 0o600
    assert "RESTIC_REST_PASSWORD=token-a\n" in env_text
    assert not (root / PENDING_ENV_RELATIVE_PATH).exists()


def test_a_repeat_of_the_same_destination_changes_nothing_and_answers_the_state(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    ready = _backups(client)
    calls = fake_restic_calls(backup_workspace)

    again = client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    with_no_storage = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "IMBUE_CLOUD"})
    _run_backup_setup(app)

    assert (again.status_code, with_no_storage.status_code) == (200, 200)
    assert again.get_json() == with_no_storage.get_json() == ready == _backups(client)
    # The pass a program makes when it starts checks the credentials in use, and nothing else.
    assert fake_restic_calls(backup_workspace)[len(calls) :] == [f"{IMBUE_CLOUD_REPOSITORY}|--no-lock cat config"]


def test_a_destination_with_no_storage_awaits_it_and_is_set_up_from_what_was_kept_once_there_is_some(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    awaiting = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "IMBUE_CLOUD"})

    assert awaiting.status_code == 200
    assert awaiting.get_json()["setup"] == {
        "state": "AWAITING_STORAGE",
        "detail": "This workspace holds no storage credentials for the destination.",
        "started_at": None,
        "finished_at": None,
        "retry_at": None,
    }

    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    env_text = (_workspace_root(app) / RESTIC_ENV_PATH).read_text()
    client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"})
    _run_backup_setup(app)
    assert _backups(client)["is_configured"] is False

    set_fake_restic_mode(backup_workspace, FAKE_RESTIC_FINDS_A_REPOSITORY)
    from_kept = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "IMBUE_CLOUD"})
    _run_backup_setup(app)

    assert (from_kept.status_code, from_kept.get_json()["setup"]["state"]) == (202, "INITIALIZING")
    assert _backups(client)["setup"]["state"] == "READY"
    assert (_workspace_root(app) / RESTIC_ENV_PATH).read_text() == env_text


def test_none_moves_restic_env_aside_and_keeps_it(client: FlaskClient, app: Flask, backup_workspace: Path) -> None:
    root = _workspace_root(app)
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    installed = parse_restic_env_file((root / RESTIC_ENV_PATH).read_text())

    answered = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"})
    _run_backup_setup(app)

    assert answered.status_code == 202
    assert (answered.get_json()["destination"], answered.get_json()["setup"]["state"]) == ("NONE", "NONE")
    off = _backups(client)
    assert (off["destination"], off["is_configured"], off["credentials_digest"], off["repository"]) == (
        "NONE",
        False,
        "",
        "",
    )
    assert not (root / RESTIC_ENV_PATH).exists()
    assert kept_envs(root) == [installed]
    assert client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"}).status_code == 200


def test_storage_for_the_repository_in_use_is_taken_only_when_the_storage_refuses_what_is_held(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    root = _workspace_root(app)
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    held_text = (root / RESTIC_ENV_PATH).read_text()
    set_fake_restic_mode(backup_workspace, FAKE_RESTIC_FINDS_A_REPOSITORY)

    offered = client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-b"))
    _run_backup_setup(app)

    assert offered.status_code == 202
    assert _backups(client)["setup"]["state"] == "READY"
    assert (root / RESTIC_ENV_PATH).read_text() == held_text

    refuse_fake_restic_token(backup_workspace, "token-a")
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-c"))
    _run_backup_setup(app)

    taken = parse_restic_env_file((root / RESTIC_ENV_PATH).read_text())
    held = parse_restic_env_file(held_text)
    assert _backups(client)["setup"]["state"] == "READY"
    assert taken == {**held, "RESTIC_REST_PASSWORD": "token-c"}


def test_a_storage_that_refuses_what_was_offered_leaves_the_workspace_awaiting_storage(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    refuse_fake_restic_token(backup_workspace, "token-a")

    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)

    state = _backups(client)
    assert (state["destination"], state["setup"]["state"], state["setup"]["detail"]) == (
        "IMBUE_CLOUD",
        "AWAITING_STORAGE",
        _STORAGE_REFUSED,
    )
    assert state["is_configured"] is False


@pytest.mark.parametrize(
    ("body", "code", "said"),
    [
        (
            own_storage_request("s3:https://s3.example/bucket", {"RESTIC_PASSWORD": "typed-password"}),
            "INVALID_DESTINATION",
            "RESTIC_PASSWORD must not be among the storage's variables",
        ),
        ({"destination": "TAPE"}, "INVALID_DESTINATION", "'TAPE' is not a destination this workspace knows"),
        ({"destination": "NONE", "own_storage": {"repository": "s3:b"}}, "INVALID_DESTINATION", "NONE takes neither"),
        ({}, "INVALID_REQUEST", "destination"),
        ({"destination": "NONE", "env_text": "RESTIC_PASSWORD=typed-password"}, "INVALID_REQUEST", "env_text"),
    ],
)
def test_a_destination_the_workspace_cannot_take_is_refused(
    client: FlaskClient, app: Flask, body: dict[str, object], code: str, said: str
) -> None:
    refused = client.put(BACKUP_DESTINATION_ROUTE, json=body)

    assert refused.status_code == 400
    assert refused.get_json()["code"] == code
    assert said in refused.get_json()["detail"]
    assert "typed-password" not in refused.get_data(as_text=True)
    assert not (_workspace_root(app) / "data").exists()


def test_a_change_of_destination_is_refused_while_a_backup_operation_runs_and_names_its_holder(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    lease_path = state_of(app).backup_lease_path
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    taken = take_lease(lease_path, "restore", time.time())

    refused = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"})
    same = client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))

    assert refused.status_code == 409
    assert refused.get_json() == {
        "detail": "A backup operation is running (restore); the destination cannot change until it ends.",
        "code": "BACKUP_OPERATION_RUNNING",
        "lease": {"holder": "restore", "taken_at": taken.taken_at, "expires_at": taken.expires_at},
    }
    assert same.status_code == 200
    assert same.get_json()["lease"] == {
        "holder": "restore",
        "taken_at": taken.taken_at,
        "expires_at": taken.expires_at,
    }
    assert (same.get_json()["destination"], same.get_json()["is_configured"]) == ("IMBUE_CLOUD", True)

    release_lease(lease_path, "restore", time.time())
    assert client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"}).status_code == 202
    assert _backups(client)["lease"] is None


def test_backups_are_turned_off_while_the_workspaces_own_setup_holds_the_backup_lease(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    taken = take_lease(state_of(app).backup_lease_path, HOLDER_SETUP, time.time())

    answered = client.put(BACKUP_DESTINATION_ROUTE, json={"destination": "NONE"})

    assert answered.status_code == 202
    off = answered.get_json()
    assert (off["destination"], off["setup"]["state"]) == ("NONE", "NONE")
    assert off["lease"] == {"holder": "setup", "taken_at": taken.taken_at, "expires_at": taken.expires_at}


def test_the_backup_credentials_are_answered_to_the_owner_with_the_digest_the_state_answers(
    client: FlaskClient, app: Flask, backup_workspace: Path
) -> None:
    assert client.get(BACKUP_CREDENTIALS_ROUTE, headers=OWNER_IDENTITY_HEADERS).get_json() == {
        "env_text": "",
        "digest": "",
    }
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)
    env_text = (_workspace_root(app) / RESTIC_ENV_PATH).read_text()

    through_a_proxy = client.get(BACKUP_CREDENTIALS_ROUTE, headers=OWNER_IDENTITY_HEADERS)
    from_inside_the_workspace = client.get(BACKUP_CREDENTIALS_ROUTE)

    assert through_a_proxy.status_code == 200
    assert through_a_proxy.headers["Cache-Control"] == "no-store"
    assert through_a_proxy.get_json() == from_inside_the_workspace.get_json()
    assert through_a_proxy.get_json() == {"env_text": env_text, "digest": _backups(client)["credentials_digest"]}
    assert through_a_proxy.get_json()["digest"] == env_digest(env_text) != ""


@pytest.mark.parametrize("identity", [_GRANTEE, _VISITING_AGENT], ids=["grantee", "visiting-agent"])
def test_the_backup_credentials_are_refused_to_anyone_but_the_owner(
    client: FlaskClient, app: Flask, backup_workspace: Path, identity: RequestIdentity
) -> None:
    client.put(BACKUP_DESTINATION_ROUTE, json=imbue_cloud_request("token-a"))
    _run_backup_setup(app)

    refused = client.get(BACKUP_CREDENTIALS_ROUTE, headers=identity_headers(identity))

    assert refused.status_code == 403
    assert refused.get_json() == {
        "detail": "Only the workspace's owner may read its backup credentials.",
        "code": "OWNER_ONLY",
    }
    assert client.get(BACKUPS_ROUTE, headers=identity_headers(identity)).status_code == 200


def test_backup_verification_is_turned_off_and_on_in_the_workspaces_own_config(
    client: FlaskClient, app: Flask
) -> None:
    backup_toml = _workspace_root(app) / BACKUP_TOML_RELATIVE_PATH
    backup_toml.parent.mkdir(parents=True)
    backup_toml.write_text("# mine\ninterval_seconds = 900\n")

    assert client.put(BACKUP_VERIFICATION_ROUTE, json={"is_enabled": False}).get_json() == {"is_enabled": False}
    assert backup_toml.read_text() == "verification_disabled = true\n# mine\ninterval_seconds = 900\n"
    assert _backups(client)["is_verification_enabled"] is False

    assert client.put(BACKUP_VERIFICATION_ROUTE, json={"is_enabled": True}).get_json() == {"is_enabled": True}
    assert backup_toml.read_text() == "verification_disabled = false\n# mine\ninterval_seconds = 900\n"
