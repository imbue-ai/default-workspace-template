"""Pin what ``system/vendor/mngr`` must and must not contain.

This repo is public and ``system/vendor/mngr`` is a committed artifact in it, so the
tree may only ever hold mngr's public subset -- the same tree the Copybara mirror
publishes to ``imbue-ai/mngr``. mngr's own tests guard a *freshly materialized* tree;
nothing else guards the one that is actually checked in here and pushed.

The second test is the other half of the contract: every path this repo's own manifests
name inside the vendored tree has to exist. Import checks cannot cover that -- dropping
``libs/mngr_ttyd`` from the allowlist leaves the tree perfectly import-closed while
silently breaking the terminal app -- so the manifests are the source of truth.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_VENDOR = _REPO_ROOT / "system" / "vendor" / "mngr"

# Directories and files the mirror keeps private. Mirrored from mirror/copy.bara.sky in
# mngr-internal; if that allowlist gains a public path this list may shrink, never grow.
_MUST_BE_ABSENT = (
    "mirror",
    "specs",
    "blueprint",
    "litellm_proxy",
    "dev",
    "apps/minds/docs/deploy",
    "apps/minds/deployment_tests",
    "apps/minds/CLAUDE.md",
    "apps/analytics",
    "apps/minds_admin",
    "apps/observability",
    "apps/remote_service_connector",
    "apps/share_relay",
    "apps/modal_litellm",
    "apps/oauth_redirector",
    "apps/apt_mirror",
    "apps/slack_exporter",
    "apps/minds_evals",
    "apps/mngr_minds_eval",
    "libs/mngr_tmr",
    "libs/mngr_mapreduce",
    "libs/mngr_claude_subagent_proxy",
    "libs/mngr_behaviors",
    "libs/modal_app_kit",
)


def test_vendored_mngr_carries_no_path_the_mirror_keeps_private() -> None:
    assert _VENDOR.is_dir(), f"no vendored mngr at {_VENDOR}"
    leaked = [path for path in _MUST_BE_ABSENT if (_VENDOR / path).exists()]
    assert not leaked, (
        "private mngr paths are committed in this PUBLIC repo: "
        + ", ".join(leaked)
        + ". Re-sync with `just sync-vendor-mngr` from a mngr checkout; never hand-copy the tree."
    )
    # apps/ and .github/ are the two trees where a leak is most likely and cheapest to pin exactly.
    assert sorted(p.name for p in (_VENDOR / "apps").iterdir()) == ["minds"]
    workflows = _VENDOR / ".github" / "workflows"
    assert sorted(p.name for p in workflows.iterdir()) == ["ci.yml"]


def _paths_named_by_manifests() -> list[tuple[str, str]]:
    """Collect (manifest, repo-relative path) for every vendored path a manifest names.

    Each parser asserts it matched something: one that silently stops matching turns this
    test into a no-op, which is the failure mode a manifest-driven test has to guard.
    """
    named: list[tuple[str, str]] = []

    dockerfile = (_REPO_ROOT / "system" / "Dockerfile").read_text()
    copied = re.findall(r"^COPY\s+(system/vendor/mngr/\S+)", dockerfile, re.MULTILINE)
    assert copied, "no system/vendor/mngr COPY lines found in system/Dockerfile"
    named += [("system/Dockerfile", path) for path in copied]

    plugins = tomllib.loads((_REPO_ROOT / "system" / "config" / "mngr_plugins.toml").read_text())
    plugin_paths = [entry["path"] for entry in plugins["plugins"]]
    assert plugin_paths, "no plugins declared in system/config/mngr_plugins.toml"
    named += [("system/config/mngr_plugins.toml", path) for path in plugin_paths]

    root = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    sources = [
        source["path"]
        for source in root["tool"]["uv"]["sources"].values()
        if isinstance(source, dict) and str(source.get("path", "")).startswith("system/vendor/mngr/")
    ]
    assert sources, "no system/vendor/mngr path dependencies in pyproject.toml"
    named += [("pyproject.toml", path) for path in sources]

    for script in ("system/scripts/build_workspace.sh", "system/apps/terminal/run_ttyd.sh"):
        text = (_REPO_ROOT / script).read_text()
        literals = re.findall(r"system/vendor/mngr/[A-Za-z0-9_./-]+", text)
        assert literals, f"no system/vendor/mngr paths found in {script}"
        named += [(script, path) for path in literals]

    return sorted(set(named))


@pytest.mark.parametrize("manifest, vendor_path", _paths_named_by_manifests())
def test_every_vendored_mngr_path_a_manifest_names_exists(manifest: str, vendor_path: str) -> None:
    assert (_REPO_ROOT / vendor_path).exists(), (
        f"{manifest} names {vendor_path}, which is missing from the vendored tree. "
        "If mngr's mirror allowlist stopped publishing it, the allowlist is what needs fixing."
    )
