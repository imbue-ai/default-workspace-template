"""Tests for the plugin install/update script that both the SessionStart hook and
the worker create template run.

The script drives the real ``claude plugin`` CLI, which needs a marketplace clone and
network, so these tests put a recording ``claude`` shim on PATH instead. What they pin
is the contract the callers rely on: every plugin's marketplace is registered when
missing, every plugin is installed at *project* scope for the current directory before
being updated, the shared plugin cache is never wiped, an install failure is reported
on stdout (the SessionStart hook's channel into the session context) and only fails
the run under ``--strict``, and an update failure alone is a warning rather than a
failure. A last test keeps the script's plugin table in step with
``.claude/settings.json``, which is what actually enables the plugins.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

_SCRIPT = Path(__file__).with_name("claude_update_plugin.sh")
_CLAUDE_SETTINGS = Path(__file__).resolve().parents[2] / ".claude" / "settings.json"

_GUARDIAN = "imbue-code-guardian@imbue-code-guardian"
_GUARDIAN_REPO = "imbue-ai/code-guardian"
_FRONTEND = "frontend-design@claude-code-plugins"
_FRONTEND_REPO = "anthropics/claude-code"
_ALL_MARKETPLACES = "imbue-code-guardian claude-code-plugins"

# The shim appends one line per invocation to $SHIM_LOG, answers `marketplace list`
# with the names in $SHIM_MARKETPLACES in the real CLI's layout, and fails the
# `<step>:<last argument>` pairs listed in $SHIM_FAIL so a test can make exactly one
# step of one plugin fail.
_CLAUDE_SHIM = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$SHIM_LOG"
step="$2"
if [[ "$step" == "marketplace" ]]; then
    step="marketplace-$3"
fi
target="${@: -1}"
if [[ "$step" == "marketplace-list" ]]; then
    echo "Configured marketplaces:"
    for name in ${SHIM_MARKETPLACES:-}; do
        echo ""
        echo "  > ${name}"
        echo "    Source: GitHub (someone/${name}-source)"
    done
    exit 0
fi
case " ${SHIM_FAIL:-} " in
    *" ${step}:${target} "*)
        echo "shim: ${step} of ${target} failed" >&2
        exit 1
        ;;
esac
echo "shim: ${step} ok ${target}"
"""


def _install_shim(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "claude"
    shim.write_text(_CLAUDE_SHIM)
    shim.chmod(0o755)
    return bin_dir


def _fake_config_dir(tmp_path: Path) -> tuple[Path, Path]:
    """A config dir holding a cached plugin, as the shared workspace config dir does."""
    config_dir = tmp_path / f"config-{uuid4().hex}"
    cached = (
        config_dir
        / "plugins"
        / "cache"
        / "imbue-code-guardian"
        / "imbue-code-guardian"
        / "0.4.0"
    )
    cached.mkdir(parents=True)
    marker = cached / "skills" / "autofix" / "SKILL.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("---\nname: autofix\n---\n")
    return config_dir, marker


def _run(
    tmp_path: Path,
    *args: str,
    fail: str = "",
    marketplaces: str = _ALL_MARKETPLACES,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    shim_log = tmp_path / f"shim-{uuid4().hex}.log"
    bin_dir = _install_shim(tmp_path)
    workdir = tmp_path / "project"
    workdir.mkdir(exist_ok=True)
    result = subprocess.run(
        ["bash", str(_SCRIPT), *args],
        cwd=workdir,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "SHIM_LOG": str(shim_log),
            "SHIM_FAIL": fail,
            "SHIM_MARKETPLACES": marketplaces,
            **(extra_env or {}),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    calls = shim_log.read_text().splitlines() if shim_log.exists() else []
    return result, calls


def test_installs_every_plugin_at_project_scope_before_updating_it(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert calls == [
        "plugin marketplace list",
        f"plugin install --scope project {_GUARDIAN}",
        f"plugin update --scope project {_GUARDIAN}",
        f"plugin install --scope project {_FRONTEND}",
        f"plugin update --scope project {_FRONTEND}",
    ]
    assert "warning" not in result.stdout


def test_registers_a_missing_marketplace_before_installing_from_it(
    tmp_path: Path,
) -> None:
    # Only the guardian marketplace is known, as in a config dir that has never
    # started a session in this project (extraKnownMarketplaces not yet applied).
    result, calls = _run(tmp_path, marketplaces="imbue-code-guardian")

    assert result.returncode == 0, result.stderr
    assert calls == [
        "plugin marketplace list",
        f"plugin install --scope project {_GUARDIAN}",
        f"plugin update --scope project {_GUARDIAN}",
        f"plugin marketplace add {_FRONTEND_REPO}",
        f"plugin install --scope project {_FRONTEND}",
        f"plugin update --scope project {_FRONTEND}",
    ]


def test_registers_every_marketplace_in_an_empty_config_dir(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, marketplaces="")

    assert result.returncode == 0, result.stderr
    assert f"plugin marketplace add {_GUARDIAN_REPO}" in calls
    assert f"plugin marketplace add {_FRONTEND_REPO}" in calls
    assert calls.index(f"plugin marketplace add {_GUARDIAN_REPO}") < calls.index(
        f"plugin install --scope project {_GUARDIAN}"
    )


def test_marketplace_registration_failure_counts_as_an_install_failure(
    tmp_path: Path,
) -> None:
    result, calls = _run(
        tmp_path,
        "--strict",
        marketplaces="imbue-code-guardian",
        fail=f"marketplace-add:{_FRONTEND_REPO}",
    )

    assert result.returncode == 1
    assert f"failed to install plugin(s) {_FRONTEND}" in result.stdout
    assert f"plugin install --scope project {_FRONTEND}" not in calls
    # The other plugin was still installed.
    assert f"plugin update --scope project {_GUARDIAN}" in calls


def test_never_wipes_the_shared_plugin_cache(tmp_path: Path) -> None:
    config_dir, cached_skill = _fake_config_dir(tmp_path)

    result, _ = _run(
        tmp_path,
        fail=f"install:{_GUARDIAN} update:{_GUARDIAN}",
        extra_env={"CLAUDE_CONFIG_DIR": str(config_dir)},
    )

    assert result.returncode == 0, result.stderr
    # Even when every step for the cached plugin fails, the cached copy -- the one
    # running sessions already loaded -- is still there for them and for the next
    # session.
    assert cached_skill.is_file()


def test_install_failure_is_reported_on_stdout_and_does_not_fail_the_hook(
    tmp_path: Path,
) -> None:
    result, calls = _run(tmp_path, fail=f"install:{_GUARDIAN}")

    assert result.returncode == 0
    assert f"failed to install plugin(s) {_GUARDIAN}" in result.stdout
    assert f"shim: install of {_GUARDIAN} failed" in result.stderr
    # The failed plugin is not updated (nothing to update), the other one still is.
    assert calls == [
        "plugin marketplace list",
        f"plugin install --scope project {_GUARDIAN}",
        f"plugin install --scope project {_FRONTEND}",
        f"plugin update --scope project {_FRONTEND}",
    ]


def test_install_failure_fails_the_run_under_strict(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "--strict", fail=f"install:{_FRONTEND}")

    assert result.returncode == 1
    assert f"failed to install plugin(s) {_FRONTEND}" in result.stdout
    # The failure is reported after every plugin has been attempted, not on the
    # first miss, so one broken plugin never blocks installing the others.
    assert f"plugin install --scope project {_GUARDIAN}" in calls
    assert f"plugin update --scope project {_GUARDIAN}" in calls


def test_update_failure_alone_is_a_warning_even_under_strict(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "--strict", fail=f"update:{_GUARDIAN}")

    assert result.returncode == 0, result.stderr
    assert (
        f"could not update plugin {_GUARDIAN}; using the cached version"
        in result.stdout
    )
    assert "failed to install" not in result.stdout
    assert f"plugin update --scope project {_GUARDIAN}" in calls


def test_missing_claude_binary_is_a_noop_for_the_hook_but_an_error_under_strict(
    tmp_path: Path,
) -> None:
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = {
        **os.environ,
        "PATH": str(empty_bin),
        "SHIM_LOG": str(tmp_path / "unused.log"),
    }

    hook_run = subprocess.run(
        ["/bin/bash", str(_SCRIPT)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    strict_run = subprocess.run(
        ["/bin/bash", str(_SCRIPT), "--strict"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert hook_run.returncode == 0
    assert hook_run.stdout == ""
    assert strict_run.returncode == 1
    assert "claude is not on PATH" in strict_run.stderr


def test_unknown_argument_is_rejected(tmp_path: Path) -> None:
    result, calls = _run(tmp_path, "--loud")

    assert result.returncode == 2
    assert "usage:" in result.stderr
    assert calls == []


def test_plugin_table_matches_the_plugins_enabled_in_claude_settings() -> None:
    """The script installs what .claude/settings.json enables; if the two drift, a
    plugin is either enabled but never installed (missing skills) or installed but
    never enabled (dead weight)."""
    settings = json.loads(_CLAUDE_SETTINGS.read_text())
    enabled = {plugin_id for plugin_id, on in settings["enabledPlugins"].items() if on}
    marketplaces = {
        name: f"{entry['source']['repo']}"
        for name, entry in settings["extraKnownMarketplaces"].items()
        if entry["source"]["source"] == "github"
    }

    table = dict(
        re.findall(r'^\s*"([^"|]+@[^"|]+)\|([^"]+)"', _SCRIPT.read_text(), re.MULTILINE)
    )

    assert set(table) == enabled
    for plugin_id, repo in table.items():
        assert marketplaces[plugin_id.split("@", 1)[1]] == repo
