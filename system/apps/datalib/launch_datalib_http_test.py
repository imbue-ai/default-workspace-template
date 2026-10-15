"""Unit tests for the Datalib launcher's token handling and the manifest copy it registers."""

import tomllib
from pathlib import Path

import pytest
from datalib_launcher_testing import launcher

_MANIFEST_PATH = Path(__file__).parent / "app.toml"
_TOKEN = "0123abcd-._~XYZ"


def test_the_token_file_is_where_datalib_http_publishes_it() -> None:
    assert launcher.token_file_path(Path("store")) == Path("store/system/api-token")


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


def test_datalib_http_binds_loopback_and_requires_the_token() -> None:
    assert launcher.child_environment(8731, _TOKEN) == {
        "DATALIB_BIND": "127.0.0.1:8731",
        "DATALIB_TOKEN": _TOKEN,
    }
