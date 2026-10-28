import json
from pathlib import Path

from workspace_profile.identity import RequestIdentity
from workspace_profile.overrides import (
    UNSENT_CHANGES_RELATIVE_PATH,
    USER_OVERRIDES_RELATIVE_DIR,
    load_unsent_change_values,
    load_user_override_values,
)
from workspace_profile.resolve import resolve_profile_setting, resolve_request_user_id
from workspace_profile.snapshot import WorkspaceProfileSnapshot

_OWNER_WITH_ACCOUNT = RequestIdentity(
    owner=True, user_id="user-owner", email="owner@example.com"
)
_ANONYMOUS_OWNER = RequestIdentity(owner=True)
_VISITOR = RequestIdentity(
    owner=False, user_id="user-visitor", email="visitor@example.com"
)

_SNAPSHOT = WorkspaceProfileSnapshot(
    account_id="account-1",
    owner_user_id="user-owner",
    value_by_key={"example_setting": "from-the-account"},
)


def _write_override(
    workspace_root: Path, user_id: str, value_by_key: dict[str, object]
) -> None:
    path = workspace_root / USER_OVERRIDES_RELATIVE_DIR / f"{user_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value_by_key), encoding="utf-8")


def _write_unsent(workspace_root: Path, value_by_key: dict[str, object]) -> None:
    path = workspace_root / UNSENT_CHANGES_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value_by_key), encoding="utf-8")


def test_the_owner_resolves_the_snapshot_value_when_nothing_overrides_it(
    tmp_path: Path,
) -> None:
    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_OWNER_WITH_ACCOUNT,
        workspace_root=tmp_path,
    )

    assert value == "from-the-account"


def test_the_owners_override_wins_over_the_snapshot(tmp_path: Path) -> None:
    _write_override(tmp_path, "user-owner", {"example_setting": "from-the-override"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_OWNER_WITH_ACCOUNT,
        workspace_root=tmp_path,
    )

    assert value == "from-the-override"


def test_an_unshared_workspaces_owner_keys_overrides_by_the_snapshots_owner_id(
    tmp_path: Path,
) -> None:
    # The anonymous owner's header carries no user id; the snapshot's owner id keys their file.
    _write_override(tmp_path, "user-owner", {"example_setting": "from-the-override"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_ANONYMOUS_OWNER,
        workspace_root=tmp_path,
    )

    assert value == "from-the-override"


def test_an_owner_with_no_snapshot_keys_the_local_overrides(tmp_path: Path) -> None:
    _write_override(tmp_path, "local", {"example_setting": "from-the-local-override"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=None,
        identity=_ANONYMOUS_OWNER,
        workspace_root=tmp_path,
    )

    assert value == "from-the-local-override"


def test_the_shells_unsent_change_overlays_the_snapshot_for_the_owner(
    tmp_path: Path,
) -> None:
    _write_unsent(tmp_path, {"example_setting": "from-the-unsent-change"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_OWNER_WITH_ACCOUNT,
        workspace_root=tmp_path,
    )

    assert value == "from-the-unsent-change"


def test_the_owners_override_wins_over_an_unsent_change(tmp_path: Path) -> None:
    _write_override(tmp_path, "user-owner", {"example_setting": "from-the-override"})
    _write_unsent(tmp_path, {"example_setting": "from-the-unsent-change"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_OWNER_WITH_ACCOUNT,
        workspace_root=tmp_path,
    )

    assert value == "from-the-override"


def test_a_visitor_never_resolves_the_owners_account_value(tmp_path: Path) -> None:
    _write_unsent(tmp_path, {"example_setting": "from-the-unsent-change"})

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_VISITOR,
        workspace_root=tmp_path,
    )

    assert value == "the-default"


def test_a_visitors_own_override_wins_over_the_default(tmp_path: Path) -> None:
    _write_override(
        tmp_path, "user-visitor", {"example_setting": "from-the-visitor-override"}
    )

    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=_VISITOR,
        workspace_root=tmp_path,
    )

    assert value == "from-the-visitor-override"


def test_a_visitor_without_a_user_id_resolves_the_default(tmp_path: Path) -> None:
    value = resolve_profile_setting(
        "example_setting",
        "the-default",
        snapshot=_SNAPSHOT,
        identity=RequestIdentity(owner=False),
        workspace_root=tmp_path,
    )

    assert value == "the-default"


def test_resolve_request_user_id_prefers_the_snapshot_owner_for_the_owner() -> None:
    assert resolve_request_user_id(_ANONYMOUS_OWNER, _SNAPSHOT) == "user-owner"
    assert resolve_request_user_id(_OWNER_WITH_ACCOUNT, _SNAPSHOT) == "user-owner"
    assert resolve_request_user_id(_ANONYMOUS_OWNER, None) == "local"
    assert resolve_request_user_id(_VISITOR, _SNAPSHOT) == "user-visitor"


def test_override_loads_tolerate_missing_and_malformed_files(tmp_path: Path) -> None:
    assert load_user_override_values(tmp_path, "user-owner") == {}
    assert load_unsent_change_values(tmp_path) == {}

    override_path = tmp_path / USER_OVERRIDES_RELATIVE_DIR / "user-owner.json"
    override_path.parent.mkdir(parents=True, exist_ok=True)
    override_path.write_text("{not json", encoding="utf-8")
    assert load_user_override_values(tmp_path, "user-owner") == {}

    unsent_path = tmp_path / UNSENT_CHANGES_RELATIVE_PATH
    unsent_path.parent.mkdir(parents=True, exist_ok=True)
    unsent_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    assert load_unsent_change_values(tmp_path) == {}
