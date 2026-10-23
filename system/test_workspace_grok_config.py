"""Pin the invariants of the ``[agent_types.grok]`` block in ``.mngr/settings.toml``.

The block is read the way the workspace's mngr reads it: through mngr's own config loader, over a copy
of the committed file, resolved to the grok plugin's config.

1. ``share_auth`` stays true: an account binding reaches grok as ``GROK_AUTH_PATH`` in the agent's env
   file, and with ``share_auth = false`` mngr_grok removes that variable from grok's environment.
2. ``busy_send_policy`` is ``REFUSE``: a send that finds a turn in flight fails rather than waiting
   inside grok, so the chat app's queue is the only one.
3. ``command`` is the OOM band launcher running ``grok``.
4. No ``version`` is pinned: ``system/scripts/install_grok.sh`` is the only grok version authority.
5. ``settings_overrides`` folds onto mngr's rendered ``config.toml`` without dropping any key mngr
   writes there.
6. grok's file tools are denied the secrets directory.
7. No workspace-wide export names a grok credential or home variable: one inherited by every agent
   would override each chat's own account binding and grok home.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.mngr.config.agent_config_registry import resolve_agent_type
from imbue.mngr.config.external_settings import apply_settings_patch
from imbue.mngr.config.loader import load_config
from imbue.mngr.main import get_or_create_plugin_manager
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr_grok.api import render_pinned_grok_home

_REPO_ROOT = Path(__file__).parents[1]
_SETTINGS_PATH = _REPO_ROOT / ".mngr" / "settings.toml"
_DOCKERFILE_PATH = _REPO_ROOT / "system" / "Dockerfile"
_SUPERVISORD_CONF_DIR = _REPO_ROOT / "system" / "supervisord.conf.d"
_GROK_ACCOUNT_VARIABLES = frozenset({"GROK_AUTH_PATH", "XAI_API_KEY", "GROK_HOME"})
_EXPORT_KEYS = frozenset({"env", "host_env", "pass_env", "pass_host_env"})
_DOCKERFILE_EXPORT_RE = re.compile(
    r"^\s*(?:ENV|ARG)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE
)
_SUPERVISORD_ENVIRONMENT_RE = re.compile(r"^\s*environment\s*=(.*)$", re.MULTILINE)
_SUPERVISORD_VARIABLE_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*=")
_OOM_LAUNCHER_COMMAND = "python3 ${MNGR_AGENT_WORK_DIR:-.}/system/services/oom_priority/bin/agent_oom_launch.py grok"


def _load_grok_agent_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    config_dir = tmp_path / ".mngr"
    config_dir.mkdir()
    # The opt-in is a top-level key, so it goes ahead of the committed file's tables.
    (config_dir / "settings.toml").write_text(
        "is_allowed_in_pytest = true\n" + _SETTINGS_PATH.read_text()
    )
    monkeypatch.setenv("MNGR_PROJECT_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("MNGR_HOST_DIR", str(tmp_path / "host"))
    with ConcurrencyGroup(name="grok-settings-test") as cg:
        context = load_config(get_or_create_plugin_manager(), cg, strict=True)
    resolved = resolve_agent_type(AgentTypeName("grok"), context.config)
    return resolved.agent_config.model_dump(mode="json")


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        pytest.param("share_auth", True, id="never_turns_shared_auth_off"),
        pytest.param(
            "busy_send_policy", "REFUSE", id="refuses_sends_while_a_turn_runs"
        ),
        pytest.param(
            "command",
            _OOM_LAUNCHER_COMMAND,
            id="launches_through_the_oom_band_launcher",
        ),
        pytest.param("version", None, id="pins_no_version"),
    ],
)
def test_grok_block_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, expected: object
) -> None:
    assert _load_grok_agent_config(tmp_path, monkeypatch)[field] == expected


def test_grok_settings_overrides_keep_every_key_mngr_renders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_overrides = _load_grok_agent_config(tmp_path, monkeypatch)[
        "settings_overrides"
    ]
    home_dir = tmp_path / "grok_home"
    home_dir.mkdir()
    render_pinned_grok_home(home_dir)
    rendered = tomllib.loads((home_dir / "config.toml").read_text())

    patched = apply_settings_patch(
        rendered,
        settings_overrides,
        allow_narrowing=False,
        base_description="the rendered grok config.toml",
    )

    assert patched["features"] == {**rendered["features"], "backend_tools": False}
    assert patched["disable_web_search"] is True
    assert {
        key: value
        for key, value in patched.items()
        if key not in ("features", "disable_web_search", "permission")
    } == {key: value for key, value in rendered.items() if key != "features"}


def test_grok_file_tools_are_denied_the_secrets_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_overrides = _load_grok_agent_config(tmp_path, monkeypatch)[
        "settings_overrides"
    ]
    assert settings_overrides["permission"] == {"deny": ["Read(**/.secrets/**)"]}


def _exported_names(value: Any) -> list[str]:
    """The variable names an export value sets: a table's keys, or each list entry up to its `=`."""
    if isinstance(value, dict):
        return list(value.keys())
    if isinstance(value, list):
        return [entry.split("=", 1)[0] for entry in value if isinstance(entry, str)]
    return []


def _settings_export_names(value: Any) -> list[str]:
    """Every variable name an env, host_env, pass_env or pass_host_env key (or its `__extend`) sets, at any depth."""
    if isinstance(value, dict):
        names: list[str] = []
        for key, child in value.items():
            if key.removesuffix("__extend") in _EXPORT_KEYS:
                names.extend(_exported_names(child))
            names.extend(_settings_export_names(child))
        return names
    if isinstance(value, list):
        return [name for child in value for name in _settings_export_names(child)]
    return []


def test_no_workspace_wide_export_names_a_grok_account_variable() -> None:
    settings_names = _settings_export_names(tomllib.loads(_SETTINGS_PATH.read_text()))
    dockerfile_names = _DOCKERFILE_EXPORT_RE.findall(_DOCKERFILE_PATH.read_text())
    supervisord_names = [
        name
        for conf_path in sorted(_SUPERVISORD_CONF_DIR.glob("*.conf"))
        for environment in _SUPERVISORD_ENVIRONMENT_RE.findall(conf_path.read_text())
        for name in _SUPERVISORD_VARIABLE_RE.findall(environment)
    ]

    assert "MNGR_AGENT_ROLE" in settings_names
    assert "XDG_CACHE_HOME" in dockerfile_names
    assert "MALLOC_ARENA_MAX" in supervisord_names
    assert _GROK_ACCOUNT_VARIABLES.isdisjoint(
        [*settings_names, *dockerfile_names, *supervisord_names]
    )
