"""Tests for the workspace's `mngr create` defaults file.

The forms written here are contracts with mngr's config loader and its provisioning, so most
of these pin the exact shape: a key mngr does not read, or a link to the wrong path, binds
nothing and fails silently.
"""

import tomllib
from pathlib import Path

import pytest

from imbue.chat.create_defaults import CreateDefaults
from imbue.chat.create_defaults import ENV_FILE_KEY
from imbue.chat.create_defaults import ENV_KEY
from imbue.chat.create_defaults import LABEL_KEY
from imbue.chat.create_defaults import MANAGED_KEYS
from imbue.chat.create_defaults import PROVISION_COMMAND_KEY
from imbue.chat.create_defaults import SETTING_KEY
from imbue.chat.create_defaults import TYPE_KEY
from imbue.chat.create_defaults import create_defaults_path
from imbue.chat.create_defaults import managed_create_settings
from imbue.chat.create_defaults import write_create_defaults
from imbue.chat.harnesses.antigravity.auth import GEMINI_MODE_SETTING
from imbue.chat.harnesses.antigravity.auth import gemini_env_path
from imbue.chat.harnesses.antigravity.auth import write_gemini_api_key
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.testing import read_create_defaults_type
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager

_COMMITTED_SETTINGS = Path(__file__).parents[5] / ".mngr" / "settings.toml"


def _defaults(harness: HarnessType, tmp_path: Path) -> CreateDefaults:
    return CreateDefaults(harness=harness, account_id="acct1", account_dir=tmp_path / "accounts" / "acct1")


def _create_section(path: Path) -> dict[str, object]:
    return tomllib.loads(path.read_text())["commands"]["create"]


def test_a_claude_account_binds_by_the_config_dir_variable(tmp_path: Path) -> None:
    settings = managed_create_settings(_defaults(HarnessType.CLAUDE, tmp_path))

    assert settings[TYPE_KEY] == "claude"
    assert settings[ENV_KEY] == [f"CLAUDE_CONFIG_DIR={tmp_path / 'accounts' / 'acct1'}"]
    assert settings[LABEL_KEY] == ["account=acct1"]
    assert PROVISION_COMMAND_KEY not in settings


@pytest.mark.parametrize(
    ("harness", "source", "agent_side"),
    (
        (HarnessType.CODEX, "auth.json", "plugin/codex/home/auth.json"),
        (HarnessType.PI_CODING, "auth.json", "plugin/pi_coding/auth.json"),
        (
            HarnessType.ANTIGRAVITY,
            ".gemini/antigravity-cli/antigravity-oauth-token",
            "plugin/antigravity/home/.gemini/antigravity-cli/antigravity-oauth-token",
        ),
    ),
)
def test_a_linked_harness_binds_by_a_credential_link_over_the_agents_state_dir(
    tmp_path: Path, harness: HarnessType, source: str, agent_side: str
) -> None:
    """The agent does not exist when the file is written, so the link's agent side is the shell variable
    mngr sources before running the command; only the account side is a concrete path."""
    account = tmp_path / "accounts" / "acct1"
    settings = managed_create_settings(_defaults(harness, tmp_path))

    assert settings[TYPE_KEY] == harness.value
    assert settings[LABEL_KEY] == ["account=acct1"]
    assert ENV_KEY not in settings
    (command,) = settings[PROVISION_COMMAND_KEY]
    parent = str(Path(agent_side).parent)
    assert command == (
        f'mkdir -p "$MNGR_AGENT_STATE_DIR"/{parent} && ln -sfn {account / source} "$MNGR_AGENT_STATE_DIR"/{agent_side}'
    )


def _agy_key_defaults(tmp_path: Path) -> CreateDefaults:
    defaults = _defaults(HarnessType.ANTIGRAVITY, tmp_path)
    write_gemini_api_key(defaults.account_dir, "AIzaSyValid")
    return defaults


def test_an_agy_account_on_a_pasted_key_binds_by_the_env_file_and_the_mode(tmp_path: Path) -> None:
    """agy in key mode reads no credential file, so a link binds nothing: the key comes in as an
    env file mngr merges into the agent's own, and the mode as the config override that reaches
    the per-agent settings.json."""
    defaults = _agy_key_defaults(tmp_path)

    settings = managed_create_settings(defaults)

    assert settings[TYPE_KEY] == "antigravity"
    assert settings[ENV_FILE_KEY] == [str(gemini_env_path(defaults.account_dir))]
    assert settings[SETTING_KEY] == [GEMINI_MODE_SETTING]
    assert PROVISION_COMMAND_KEY not in settings
    assert ENV_KEY not in settings


def test_switching_away_from_a_key_account_drops_its_keys(tmp_path: Path) -> None:
    """Every binding key is managed, or one left behind by a previous account binds the new one
    to a credential it has nothing to do with."""
    path = tmp_path / "settings.local.toml"
    write_create_defaults(path, _agy_key_defaults(tmp_path))
    assert ENV_FILE_KEY in _create_section(path)

    write_create_defaults(path, _defaults(HarnessType.CLAUDE, tmp_path))

    create = _create_section(path)
    assert ENV_FILE_KEY not in create
    assert SETTING_KEY not in create


def test_the_written_file_round_trips_and_names_its_type(tmp_path: Path) -> None:
    path = tmp_path / ".mngr" / "settings.local.toml"

    write_create_defaults(path, _defaults(HarnessType.CODEX, tmp_path))

    assert read_create_defaults_type(path) == "codex"
    assert _create_section(path) == managed_create_settings(_defaults(HarnessType.CODEX, tmp_path))


def test_a_rewrite_replaces_the_managed_keys_wholesale(tmp_path: Path) -> None:
    """Switching from a claude account to a codex one must not leave claude's env binding behind."""
    path = tmp_path / "settings.local.toml"
    write_create_defaults(path, _defaults(HarnessType.CLAUDE, tmp_path))

    write_create_defaults(path, _defaults(HarnessType.CODEX, tmp_path))

    section = _create_section(path)
    assert section[TYPE_KEY] == "codex"
    assert ENV_KEY not in section
    assert PROVISION_COMMAND_KEY in section


def test_no_usable_account_removes_the_managed_keys_and_an_otherwise_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "settings.local.toml"
    write_create_defaults(path, _defaults(HarnessType.CLAUDE, tmp_path))
    assert path.exists()

    write_create_defaults(path, None)

    assert not path.exists()
    assert read_create_defaults_type(path) is None


def test_keys_outside_the_managed_block_survive_every_rewrite(tmp_path: Path) -> None:
    """The file is mngr's local config layer, and a user may keep their own keys in it."""
    path = tmp_path / "settings.local.toml"
    path.write_text('is_allowed_in_pytest = true\n\n[commands.create]\nconnect = true\ntype = "stale"\n')

    write_create_defaults(path, _defaults(HarnessType.CLAUDE, tmp_path))
    raw = tomllib.loads(path.read_text())
    assert raw["is_allowed_in_pytest"] is True
    assert raw["commands"]["create"]["connect"] is True
    assert raw["commands"]["create"][TYPE_KEY] == "claude"

    write_create_defaults(path, None)
    raw = tomllib.loads(path.read_text())
    assert raw["is_allowed_in_pytest"] is True
    assert raw["commands"]["create"] == {"connect": True}
    assert not any(key in raw["commands"]["create"] for key in MANAGED_KEYS)


@pytest.mark.parametrize("content", (b"not = [toml\n", b'type = "\xff\xfe"\n'), ids=("unparseable", "not-utf8"))
def test_a_file_that_no_longer_reads_is_rebuilt_with_a_warning(
    tmp_path: Path, loguru_records: list[str], content: bytes
) -> None:
    """A hand edit that breaks the file must not break every account write and the boot sweep with it:
    the file is derived output, and mngr refuses a malformed local layer anyway, so it is rewritten.
    Bytes that are not UTF-8 break the read rather than the parse, and the boot sweep does not catch
    that on its way out -- it would be a supervisord crash loop with no UI left to delete an account from."""
    path = tmp_path / "settings.local.toml"
    path.write_bytes(content)

    write_create_defaults(path, _defaults(HarnessType.CODEX, tmp_path))

    assert read_create_defaults_type(path) == "codex"
    assert any(record.startswith("WARNING") and "settings.local.toml" in record for record in loguru_records)


def test_the_path_follows_mngrs_project_config_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(tmp_path / "cfg"))
    assert create_defaults_path() == tmp_path / "cfg" / "settings.local.toml"

    monkeypatch.delenv("MNGR_PROJECT_CONFIG_DIR")
    assert create_defaults_path() == Path(".mngr") / "settings.local.toml"


@pytest.mark.parametrize("harness", (HarnessType.CLAUDE, HarnessType.CODEX, HarnessType.ANTIGRAVITY))
def test_the_file_loads_beside_the_committed_settings_and_resolves_the_create_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: HarnessType
) -> None:
    """The one test that reads the file the way the workspace's mngr does.

    Loaded through the vendored mngr's own loader, over a copy of the committed `.mngr/settings.toml`,
    so a key mngr does not know, or a bare assignment that narrows a list the committed file sets
    (which mngr refuses), fails here rather than on the first create in a workspace.
    """
    config_dir = tmp_path / ".mngr"
    config_dir.mkdir()
    # The opt-in is a top-level key, so it goes ahead of the committed file's tables.
    (config_dir / "settings.toml").write_text("is_allowed_in_pytest = true\n" + _COMMITTED_SETTINGS.read_text())
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    # The agy case is the key one: it is the only harness whose keys are `env_file` and
    # `setting`, and mngr rejects a config key its command does not have.
    defaults = _agy_key_defaults(tmp_path) if harness is HarnessType.ANTIGRAVITY else _defaults(harness, tmp_path)
    # The local layer is a config file too, so it opts in the same way; the writer keeps the key.
    create_defaults_path().write_text("is_allowed_in_pytest = true\n")
    write_create_defaults(create_defaults_path(), defaults)

    with ConcurrencyGroup(name="create-defaults-test") as cg:
        context = load_config(get_or_create_plugin_manager(), cg, strict=False)

    create = context.config.commands["create"].defaults
    expected = managed_create_settings(defaults)
    assert create["type"] == harness.value
    assert list(create["label"]) == expected[LABEL_KEY]
    if harness is HarnessType.CLAUDE:
        assert list(create["env"]) == expected[ENV_KEY]
    elif harness is HarnessType.ANTIGRAVITY:
        assert list(create["env_file"]) == expected[ENV_FILE_KEY]
        assert list(create["setting"]) == expected[SETTING_KEY]
    else:
        assert list(create["extra_provision_command"]) == expected[PROVISION_COMMAND_KEY]
    # The committed file's own create defaults are still there: the local layer only added to them.
    assert create["connect"] is False
