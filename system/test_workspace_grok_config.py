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
"""

from __future__ import annotations

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


def test_grok_block_never_turns_shared_auth_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _load_grok_agent_config(tmp_path, monkeypatch)["share_auth"] is True


def test_grok_block_refuses_sends_while_a_turn_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        _load_grok_agent_config(tmp_path, monkeypatch)["busy_send_policy"] == "REFUSE"
    )


def test_grok_launches_through_the_oom_band_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (
        _load_grok_agent_config(tmp_path, monkeypatch)["command"]
        == _OOM_LAUNCHER_COMMAND
    )


def test_grok_block_pins_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _load_grok_agent_config(tmp_path, monkeypatch)["version"] is None


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
