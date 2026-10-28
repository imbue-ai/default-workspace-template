"""Unit tests for the Datalib launcher: its token handling, the move of a store into the default library, and the manifest copy it registers."""

import fcntl
import tomllib
from pathlib import Path

import pytest
from datalib_launcher_testing import launcher

_MANIFEST_PATH = Path(__file__).parent / "app.toml"
_TOKEN = "0123abcd-._~XYZ"


def test_the_token_file_is_where_datalib_http_publishes_it() -> None:
    assert launcher.token_file_path(Path("store")) == Path("store/system/api-token")


def test_the_default_data_root_is_the_default_library_inside_the_libraries_folder() -> (
    None
):
    assert launcher.DATA_ROOT == Path("data/.skills/datalib/Default")
    assert launcher.DATA_ROOT.parent == launcher.LIBRARIES_DIR


def _write_store(root: Path, marker: str) -> None:
    """A store as datalib leaves one: its config, datalib-http's system directory, and a source's data."""
    (root / "system").mkdir(parents=True)
    (root / "system" / "api-token").write_text(_TOKEN)
    (root / "config.toml").write_text(f'# {marker}\n[[groups]]\ntype = "slack"\n')
    (root / "slack").mkdir()
    (root / "slack" / "messages.jsonl").write_text(marker)


def test_a_store_at_the_libraries_folder_moves_into_the_default_library(
    tmp_path: Path,
) -> None:
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "flat store")
    data_root = libraries_dir / "Default"

    assert launcher.move_store_into_library(libraries_dir, data_root) is True

    assert (data_root / "slack" / "messages.jsonl").read_text() == "flat store"
    assert "flat store" in (data_root / "config.toml").read_text()
    assert launcher.read_or_mint_token(data_root) == _TOKEN
    assert sorted(path.name for path in libraries_dir.iterdir()) == ["Default"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["datalib"]


def test_a_libraries_folder_that_already_holds_libraries_is_left_alone(
    tmp_path: Path,
) -> None:
    libraries_dir = tmp_path / "datalib"
    data_root = libraries_dir / "Default"
    _write_store(data_root, "already a library")
    (libraries_dir / "Work").mkdir()

    assert launcher.move_store_into_library(libraries_dir, data_root) is False

    assert sorted(path.name for path in libraries_dir.iterdir()) == ["Default", "Work"]
    assert (data_root / "slack" / "messages.jsonl").read_text() == "already a library"


def test_an_absent_libraries_folder_is_nothing_to_move(tmp_path: Path) -> None:
    libraries_dir = tmp_path / "datalib"
    assert (
        launcher.move_store_into_library(libraries_dir, libraries_dir / "Default")
        is False
    )
    assert not libraries_dir.exists()


def test_a_data_root_outside_the_libraries_folder_never_moves_its_parent(
    tmp_path: Path,
) -> None:
    """A test's or an operator's store elsewhere is not a library; its parent may be anything, even a checkout."""
    _write_store(tmp_path, "a checkout with a system/ directory")
    data_root = tmp_path / "data-root"
    data_root.mkdir()

    assert launcher.move_store_into_library(tmp_path / "datalib", data_root) is False

    assert (tmp_path / "config.toml").exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        "config.toml",
        "data-root",
        "slack",
        "system",
    ]


def test_a_data_root_is_known_by_where_it_is_not_by_how_it_is_spelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The launcher names the libraries folder relative to the repo root; a data root may name the same library absolutely."""
    monkeypatch.chdir(tmp_path)
    libraries_dir = Path("datalib")
    _write_store(libraries_dir, "flat store")
    data_root = tmp_path / "datalib" / "Default"

    assert launcher.move_store_into_library(libraries_dir, data_root) is True

    assert (data_root / "slack" / "messages.jsonl").read_text() == "flat store"
    assert sorted(path.name for path in libraries_dir.iterdir()) == ["Default"]


def test_a_store_a_datalib_process_still_holds_is_not_moved(tmp_path: Path) -> None:
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "being synced")
    runner_lock = libraries_dir / "system" / "runner-lock"
    runner_lock.write_text("")
    with runner_lock.open("rb") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)

        with pytest.raises(launcher.DatalibLaunchError, match="still holds"):
            launcher.move_store_into_library(libraries_dir, libraries_dir / "Default")

    assert (libraries_dir / "slack" / "messages.jsonl").read_text() == "being synced"
    assert not (tmp_path / "datalib.moving-to-Default").exists()
    # Released, the lock no longer stands in the way.
    assert (
        launcher.move_store_into_library(libraries_dir, libraries_dir / "Default")
        is True
    )


def test_a_config_naming_the_old_folder_as_its_data_root_loses_that_line(
    tmp_path: Path,
) -> None:
    """datalib-dag writes where the config's data_root says; one naming the old folder would write there again."""
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "flat store")
    (libraries_dir / "config.toml").write_text(
        f'binary_dir = "/opt/datalib"\ndata_root = "{libraries_dir}/"\n\n[[groups]]\ntype = "slack"\n'
        f'data_root = "{libraries_dir}"\n'
    )
    data_root = libraries_dir / "Default"

    assert launcher.move_store_into_library(libraries_dir, data_root) is True

    assert (data_root / "config.toml").read_text() == (
        f'binary_dir = "/opt/datalib"\n\n[[groups]]\ntype = "slack"\ndata_root = "{libraries_dir}"\n'
    )


def test_a_config_naming_another_data_root_keeps_it(tmp_path: Path) -> None:
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "flat store")
    (libraries_dir / "config.toml").write_text(
        'data_root = "/mnt/elsewhere"\n[[groups]]\ntype = "slack"\n'
    )

    assert (
        launcher.move_store_into_library(libraries_dir, libraries_dir / "Default")
        is True
    )

    assert (libraries_dir / "Default" / "config.toml").read_text() == (
        'data_root = "/mnt/elsewhere"\n[[groups]]\ntype = "slack"\n'
    )


def test_an_empty_default_library_beside_a_store_gives_way_to_it(
    tmp_path: Path,
) -> None:
    """The skill's `mkdir -p` of the data root may run before the app has started once."""
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "flat store")
    data_root = libraries_dir / "Default"
    data_root.mkdir()

    assert launcher.move_store_into_library(libraries_dir, data_root) is True

    assert (data_root / "slack" / "messages.jsonl").read_text() == "flat store"
    assert not (data_root / "Default").exists()


def test_a_store_at_the_libraries_folder_and_a_filled_default_library_refuse_to_start(
    tmp_path: Path,
) -> None:
    libraries_dir = tmp_path / "datalib"
    _write_store(libraries_dir, "flat store")
    data_root = libraries_dir / "Default"
    _write_store(data_root, "library store")

    with pytest.raises(
        launcher.DatalibLaunchError,
        match=f"{libraries_dir} holds a store and the library {data_root} is not empty",
    ):
        launcher.move_store_into_library(libraries_dir, data_root)

    # Neither store moved.
    assert (libraries_dir / "slack" / "messages.jsonl").read_text() == "flat store"
    assert (data_root / "slack" / "messages.jsonl").read_text() == "library store"


def test_an_interrupted_move_is_finished_on_the_next_start(tmp_path: Path) -> None:
    """After the first rename the folder is gone and the store sits in the staging sibling."""
    staging = tmp_path / "datalib.moving-to-Default"
    _write_store(staging, "half moved")
    libraries_dir = tmp_path / "datalib"
    data_root = libraries_dir / "Default"

    assert launcher.move_store_into_library(libraries_dir, data_root) is True

    assert (data_root / "slack" / "messages.jsonl").read_text() == "half moved"
    assert not staging.exists()


def test_an_interrupted_move_that_meets_a_filled_default_library_names_the_staging_sibling(
    tmp_path: Path,
) -> None:
    staging = tmp_path / "datalib.moving-to-Default"
    _write_store(staging, "half moved")
    libraries_dir = tmp_path / "datalib"
    data_root = libraries_dir / "Default"
    _write_store(data_root, "library store")

    with pytest.raises(
        launcher.DatalibLaunchError,
        match=f"{staging} holds a store and the library {data_root} is not empty",
    ):
        launcher.move_store_into_library(libraries_dir, data_root)

    assert (staging / "slack" / "messages.jsonl").read_text() == "half moved"


def test_a_minted_token_is_64_hex_characters_and_unique() -> None:
    first, second = launcher.mint_token(), launcher.mint_token()
    assert len(first) == 64 and int(first, 16) >= 0
    assert first != second


def test_the_published_token_is_reused(tmp_path: Path) -> None:
    token_path = launcher.token_file_path(tmp_path)
    token_path.parent.mkdir(parents=True)
    token_path.write_text(f"{_TOKEN}\n")
    assert launcher.read_or_mint_token(tmp_path) == _TOKEN


def test_a_missing_token_file_mints_one(tmp_path: Path) -> None:
    assert launcher.TOKEN_PATTERN.fullmatch(launcher.read_or_mint_token(tmp_path))


def test_an_unusable_token_file_is_replaced_by_a_minted_one(tmp_path: Path) -> None:
    token_path = launcher.token_file_path(tmp_path)
    token_path.parent.mkdir(parents=True)
    token_path.write_text("not a token: has spaces & symbols")
    minted = launcher.read_or_mint_token(tmp_path)
    assert minted != token_path.read_text()
    assert len(minted) == 64


def test_the_shipped_manifest_gains_the_token_as_its_launch_paths_preset() -> None:
    with_token = tomllib.loads(
        launcher.manifest_text_with_token(_MANIFEST_PATH.read_text(), _TOKEN)
    )
    shipped = tomllib.loads(_MANIFEST_PATH.read_text())
    assert with_token["launch_paths"] == [
        {**shipped["launch_paths"][0], "presets": {"token": _TOKEN}}
    ]
    # Nothing else moves: the copy registers the same app.
    assert {
        key: value for key, value in with_token.items() if key != "launch_paths"
    } == {key: value for key, value in shipped.items() if key != "launch_paths"}


@pytest.mark.parametrize("token", ["", "has space", 'quote"break', "new\nline", "a/b"])
def test_a_token_datalib_http_would_refuse_is_refused_here(token: str) -> None:
    with pytest.raises(launcher.DatalibLaunchError, match="invalid datalib API token"):
        launcher.manifest_text_with_token(_MANIFEST_PATH.read_text(), token)


def test_a_manifest_that_does_not_end_with_its_launch_path_is_refused() -> None:
    reordered = _MANIFEST_PATH.read_text() + "\n[scope]\nexclude = []\n"
    with pytest.raises(launcher.DatalibLaunchError, match="must end with its one"):
        launcher.manifest_text_with_token(reordered, _TOKEN)


def test_the_registered_copy_is_written_with_its_icon_beside_it(tmp_path: Path) -> None:
    registered_path = launcher.write_registered_manifest(
        _MANIFEST_PATH, _TOKEN, tmp_path / "state"
    )
    assert registered_path == tmp_path / "state" / "app.toml"
    assert tomllib.loads(registered_path.read_text())["launch_paths"][0]["presets"] == {
        "token": _TOKEN
    }
    assert (tmp_path / "state" / "icon.svg").read_text() == (
        _MANIFEST_PATH.parent / "icon.svg"
    ).read_text()


def test_datalib_http_is_told_the_data_root_and_not_to_open_a_browser() -> None:
    assert launcher.build_datalib_http_argv(
        Path("/bin/datalib-http"), Path("store")
    ) == [
        "/bin/datalib-http",
        "--no-open",
        "store",
    ]


def test_datalib_http_binds_loopback_requires_the_token_and_may_be_framed_by_studio() -> (
    None
):
    assert launcher.child_environment(8731, _TOKEN) == {
        "DATALIB_BIND": "127.0.0.1:8731",
        "DATALIB_TOKEN": _TOKEN,
        "DATALIB_FRAME_ANCESTORS": launcher.FRAME_ANCESTORS,
    }


def test_frame_ancestors_cover_studio_desktop_and_every_share_tier() -> None:
    sources = launcher.FRAME_ANCESTORS.split()
    for expected in (
        "http://localhost:*",
        "https://*.localhost:*",
        "https://*.personal-imbue.com",
        "https://studio.imbue.com",
    ):
        assert expected in sources
    # datalib-http refuses to start on anything but CSP sources; a ';' would end the directive.
    assert all(";" not in s and "," not in s and "'" not in s for s in sources)
    assert len(sources) == len(set(sources))
