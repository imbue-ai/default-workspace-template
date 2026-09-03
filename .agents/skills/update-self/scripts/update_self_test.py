"""Unit tests for the deterministic update-self helpers.

Covers the pieces the flow relies on being exactly right: target-tag
resolution (latest stable, prereleases excluded, semver not lexical order), the
merged-vs-pulled-in classification, the path -> change-class mapping, and the
skill bootstrap that extracts the target ref's own copy of the flow.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Callable
from typing import Sequence

import pytest
import update_apply
import update_apply_contract
import update_banding
import update_classification
import update_environment
import update_layout
import update_ledger
import update_probes
import update_runtime
import update_self
import update_target

_SCRIPTS_DIR = Path(__file__).parent
# ``.agents/skills/update-self/scripts/`` -> the workspace root.
_WORKSPACE_ROOT = _SCRIPTS_DIR.parents[3]
_MODULE_PATH = _SCRIPTS_DIR / "update_self.py"


# --- pick_latest_stable_tag / resolve_target -------------------------------


def test_pick_latest_stable_tag_ignores_prereleases() -> None:
    tags = [
        "minds-v0.3.5",
        "minds-v0.3.7",
        "minds-v0.3.7-rc1",
        "minds-v0.3.6",
    ]
    assert update_target.pick_latest_stable_tag(tags) == "minds-v0.3.7"


def test_pick_latest_stable_tag_uses_semver_not_lexical_order() -> None:
    # Lexically "0.3.9" > "0.3.10"; semantically 0.3.10 is newer.
    tags = ["minds-v0.3.9", "minds-v0.3.10", "minds-v0.4.0"]
    assert update_target.pick_latest_stable_tag(tags) == "minds-v0.4.0"
    tags_no_major = ["minds-v0.3.9", "minds-v0.3.10"]
    assert update_target.pick_latest_stable_tag(tags_no_major) == "minds-v0.3.10"


def test_pick_latest_stable_tag_returns_none_when_all_prerelease_or_empty() -> None:
    assert update_target.pick_latest_stable_tag([]) is None
    assert update_target.pick_latest_stable_tag(["minds-v0.3.7-rc1", "v1.2.3"]) is None


def test_resolve_target_defaults_to_latest_stable() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7", "minds-v0.3.7-rc1"]
    result = update_target.resolve_target(None, tags)
    assert result == update_target.ResolvedTarget("minds-v0.3.7", "tag")


def test_resolve_target_override_main_is_remote_qualified_branch() -> None:
    # Must resolve to the remote branch, not the stale local `main`.
    assert update_target.resolve_target("main", ["minds-v0.3.7"]) == (
        update_target.ResolvedTarget("upstream/main", "branch")
    )
    assert update_target.resolve_target(
        "main", ["minds-v0.3.7"], remote="official"
    ) == update_target.ResolvedTarget("official/main", "branch")


def test_resolve_target_override_known_tag_vs_arbitrary_ref() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7"]
    assert update_target.resolve_target("minds-v0.3.6", tags).kind == "tag"
    # An override git can validate later but that is not a known tag/main.
    passthrough = update_target.resolve_target("abc1234", tags)
    assert passthrough == update_target.ResolvedTarget("abc1234", "ref")


def test_resolve_target_raises_when_no_stable_tag_and_no_override() -> None:
    try:
        update_target.resolve_target(None, ["minds-v0.3.7-rc1"])
    except ValueError as exc:
        assert "no stable minds-v* tag" in str(exc)
    else:
        raise AssertionError("expected ValueError when no stable tag and no override")


# --- the app-version ceiling -----------------------------------------------


def test_ceiling_caps_selection_at_the_app_version() -> None:
    # The headline case: upstream has moved past the app driving this workspace.
    tags = ["minds-v0.3.8", "minds-v0.3.9", "minds-v0.4.0", "minds-v0.4.1"]
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.9"
    )
    result = update_target.resolve_target(None, tags, ceiling="minds-v0.3.9")
    assert result.ref == "minds-v0.3.9"
    assert result.ceiling == "minds-v0.3.9"
    assert result.exceeds_ceiling is False


def test_ceiling_picks_the_newest_tag_below_it_when_the_exact_tag_is_absent() -> None:
    # The app's own tag need not exist upstream (a release whose template tag was
    # never cut); the newest tag below it is still safe to take.
    tags = ["minds-v0.3.8", "minds-v0.4.0"]
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.8"
    )


def test_ceiling_compares_by_semver_not_lexically() -> None:
    tags = ["minds-v0.3.9", "minds-v0.3.10"]
    # Lexically "0.3.10" < "0.3.9", so a lexical cap would wrongly admit 0.3.10.
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.9"
    )
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.3.10")
        == "minds-v0.3.10"
    )


def test_non_release_ceiling_imposes_no_cap() -> None:
    # A dev app reports its branch rather than a release tag; there is no version
    # to compare, so the flow behaves exactly as it did before the ceiling.
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    assert update_target.pick_latest_stable_tag(tags, ceiling="main") == "minds-v0.4.0"
    result = update_target.resolve_target(None, tags, ceiling="main")
    assert result.ref == "minds-v0.4.0"
    assert result.ceiling == "main"


def test_resolve_target_explains_when_every_tag_is_above_the_ceiling() -> None:
    # Distinct from "upstream has no stable tags at all": here the user's fix is
    # to update the app, so the message has to say so.
    try:
        update_target.resolve_target(None, ["minds-v0.4.0"], ceiling="minds-v0.3.9")
    except ValueError as exc:
        assert "newer than this workspace's minds app" in str(exc)
        assert "minds-v0.3.9" in str(exc)
    else:
        raise AssertionError("expected ValueError when every tag is above the ceiling")


def test_override_above_the_ceiling_is_flagged_but_not_blocked() -> None:
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    newer = update_target.resolve_target("minds-v0.4.0", tags, ceiling="minds-v0.3.9")
    assert newer.ref == "minds-v0.4.0"
    assert newer.exceeds_ceiling is True


def test_override_at_or_below_the_ceiling_is_not_flagged() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.9"]
    older = update_target.resolve_target("minds-v0.3.6", tags, ceiling="minds-v0.3.9")
    assert older.exceeds_ceiling is False
    at_ceiling = update_target.resolve_target(
        "minds-v0.3.9", tags, ceiling="minds-v0.3.9"
    )
    assert at_ceiling.exceeds_ceiling is False


def test_unprovable_overrides_are_flagged() -> None:
    # `main` and a bare commit carry no version, so the ceiling cannot vouch for
    # them; they must surface for confirmation rather than pass silently.
    tags = ["minds-v0.3.9"]
    assert (
        update_target.resolve_target(
            "main", tags, ceiling="minds-v0.3.9"
        ).exceeds_ceiling
        is True
    )
    assert (
        update_target.resolve_target(
            "abc1234", tags, ceiling="minds-v0.3.9"
        ).exceeds_ceiling
        is True
    )
    # A prerelease, by contrast, *is* provable -- it carries a real version, and
    # 0.3.7-rc1 sits below the 0.3.9 ceiling -- so it is not flagged.
    assert (
        update_target.resolve_target(
            "minds-v0.3.7-rc1", tags, ceiling="minds-v0.3.9"
        ).exceeds_ceiling
        is False
    )


def test_overrides_are_never_flagged_without_a_ceiling() -> None:
    assert (
        update_target.resolve_target(
            "main", ["minds-v0.3.9"], ceiling=None
        ).exceeds_ceiling
        is False
    )


# --- fetch_app_template_ref ------------------------------------------------


def _install_fake_latchkey(
    monkeypatch, directory: Path, body: str, status: str, exit_code: int = 0
) -> None:
    """Put a stub ``latchkey`` on PATH that mimics the real curl passthrough.

    The real ``latchkey curl`` forwards its arguments to ``curl`` and passes curl's
    exit code, stdout and stderr back. The stub honors the two the fetch depends on
    -- ``--output <file>`` for the body and ``--write-out %{http_code}`` for the
    status on stdout -- so the test exercises the actual subprocess call.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "latchkey"
    lines = [
        "#!/usr/bin/env python3",
        "import sys",
        "argv = sys.argv[1:]",
        "out = argv[argv.index('--output') + 1]",
        f"open(out, 'w').write({body!r})",
        f"sys.stdout.write({status!r})",
    ]
    if exit_code:
        lines.append("sys.stderr.write('connection refused')")
        lines.append(f"sys.exit({exit_code})")
    script.write_text("\n".join(lines) + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{directory}:{os.environ['PATH']}")


def _init_workspace_repo(
    root: Path, *, merged_tags: tuple[str, ...], unmerged_tags: tuple[str, ...]
) -> None:
    """Init a workspace repo whose HEAD carries local work on top of ``merged_tags``.

    A template base it was created from (``merged_tags``, ancestors of ``HEAD``),
    its own commits on top, and releases upstream has cut since on a line that has
    *not* been merged (``unmerged_tags``). Both sets are visible to ``git tag
    --list``, so target selection sees them all while the already-merged check can
    still tell them apart.
    """

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", "workspace")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    _git("commit", "--allow-empty", "-q", "-m", "template base")
    for tag in merged_tags:
        _git("tag", tag)
    _git("checkout", "-q", "-b", "upstream-line")
    _git("commit", "--allow-empty", "-q", "-m", "upstream release")
    for tag in unmerged_tags:
        _git("tag", tag)
    _git("checkout", "-q", "workspace")
    _git("commit", "--allow-empty", "-q", "-m", "local work")


def test_fetch_app_template_ref_returns_the_apps_pinned_ref(
    tmp_path, monkeypatch
) -> None:
    _install_fake_latchkey(
        monkeypatch,
        tmp_path,
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert update_target.fetch_app_template_ref() == "minds-v0.3.9"


def test_fetch_app_template_ref_blocks_when_the_gateway_denies_the_route(
    tmp_path, monkeypatch
) -> None:
    """A 403 is the *likelier* old-app signal and must get the same message as a 404.

    The route and the gateway grant that reaches it ship together, so an app old
    enough to lack the route is also old enough to lack the grant -- and the gateway
    denies before the app is ever asked.
    """
    _install_fake_latchkey(
        monkeypatch, tmp_path, body='{"error": "request not permitted"}', status="403"
    )

    try:
        update_target.fetch_app_template_ref()
    except update_target.CeilingUnavailableError as exc:
        assert "too old to report its version" in str(exc)
        assert "Update the minds app itself first" in str(exc)
    else:
        raise AssertionError("expected a 403 to block with the old-app message")


def test_fetch_app_template_ref_blocks_when_the_app_predates_the_route(
    tmp_path, monkeypatch
) -> None:
    # The case the ceiling most needs to catch: an app old enough to lack the
    # route is also an app a newer template would outrun. It must not degrade to
    # "no ceiling".
    _install_fake_latchkey(
        monkeypatch, tmp_path, body='{"error": "Not Found"}', status="404"
    )

    try:
        update_target.fetch_app_template_ref()
    except update_target.CeilingUnavailableError as exc:
        assert "too old to report its version" in str(exc)
    else:
        raise AssertionError("expected a 404 to block rather than return no ceiling")


def test_fetch_app_template_ref_blocks_when_the_gateway_call_fails(
    tmp_path, monkeypatch
) -> None:
    _install_fake_latchkey(monkeypatch, tmp_path, body="", status="000", exit_code=7)

    try:
        update_target.fetch_app_template_ref()
    except update_target.CeilingUnavailableError as exc:
        assert "could not reach the minds app" in str(exc)
        assert "connection refused" in str(exc)
    else:
        raise AssertionError("expected a transport failure to block")


def test_fetch_app_template_ref_blocks_on_an_unparseable_body(
    tmp_path, monkeypatch
) -> None:
    _install_fake_latchkey(
        monkeypatch, tmp_path, body="<html>gateway error</html>", status="200"
    )

    try:
        update_target.fetch_app_template_ref()
    except update_target.CeilingUnavailableError as exc:
        assert "could not be parsed" in str(exc)
    else:
        raise AssertionError("expected an unparseable body to block")


def test_resolve_target_cli_reads_the_ceiling_from_the_app(
    tmp_path, monkeypatch, capsys
) -> None:
    """End to end: with no ``--ceiling``, the CLI asks the app and caps on the answer.

    ``latest_available`` reports the release that was held back, which is what the
    approval message tells the user about.

    The workspace sits *behind* the ceiling (created from 0.3.5, app on 0.3.9), so
    the capped target is a real update and the pass proceeds -- otherwise this
    would be asserting the already-merged refusal's territory instead.
    """
    repo = tmp_path / "repo"
    _init_workspace_repo(
        repo,
        merged_tags=("minds-v0.3.5",),
        unmerged_tags=("minds-v0.3.9", "minds-v0.4.0"),
    )
    _install_fake_latchkey(
        monkeypatch,
        tmp_path / "bin",
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert (
        update_self.main(["resolve-target", "--local-tags", "--repo-root", str(repo)])
        == 0
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ref": "minds-v0.3.9",
        "kind": "tag",
        "ceiling": "minds-v0.3.9",
        "exceeds_ceiling": False,
        "latest_available": "minds-v0.4.0",
        # minds-v0.4.0 was available and the ceiling is why it wasn't taken, so
        # the approval message owes the user the "held back" line.
        "held_back_by_ceiling": True,
    }


def test_resolve_target_cli_refuses_when_the_app_caps_it_at_the_release_it_is_on(
    tmp_path, monkeypatch, capsys
) -> None:
    """The case the ceiling exists for, from the seat of a workspace already at it.

    Created from 0.3.9, app on 0.3.9, 0.4.0 upstream. Tag selection alone resolves
    0.3.9 -- the release the workspace *is* -- so without the refusal a whole
    backup, worker and validation pass merges nothing. It has to name the app,
    because updating the app is the one action that gets them 0.4.0.
    """
    repo = tmp_path / "repo"
    _init_workspace_repo(
        repo, merged_tags=("minds-v0.3.9",), unmerged_tags=("minds-v0.4.0",)
    )
    _install_fake_latchkey(
        monkeypatch,
        tmp_path / "bin",
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert (
        update_self.main(["resolve-target", "--local-tags", "--repo-root", str(repo)])
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already on minds-v0.3.9" in captured.err
    assert "minds-v0.4.0 is available upstream but needs a newer app" in captured.err
    assert "Traceback" not in captured.err


def test_resolve_target_cli_refuses_when_already_on_the_newest_release(
    tmp_path, monkeypatch, capsys
) -> None:
    """Nothing newer exists, so the refusal must not blame the app for it."""
    repo = tmp_path / "repo"
    _init_workspace_repo(repo, merged_tags=("minds-v0.3.9",), unmerged_tags=())
    _install_fake_latchkey(
        monkeypatch,
        tmp_path / "bin",
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert (
        update_self.main(["resolve-target", "--local-tags", "--repo-root", str(repo)])
        == 1
    )

    captured = capsys.readouterr()
    assert "already on minds-v0.3.9" in captured.err
    assert "nothing to update" in captured.err
    assert "newer app" not in captured.err


def test_resolve_target_cli_does_not_block_an_override_it_is_already_on(
    tmp_path, monkeypatch, capsys
) -> None:
    """An override names a ref explicitly, and that rule outranks saving a no-op merge.

    Blocking here would make ``--override`` unusable for the one case it is most
    needed in: re-running a landing that half-finished.
    """
    repo = tmp_path / "repo"
    _init_workspace_repo(
        repo, merged_tags=("minds-v0.3.9",), unmerged_tags=("minds-v0.4.0",)
    )
    _install_fake_latchkey(
        monkeypatch,
        tmp_path / "bin",
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert (
        update_self.main(
            [
                "resolve-target",
                "--local-tags",
                "--repo-root",
                str(repo),
                "--override",
                "minds-v0.3.9",
            ]
        )
        == 0
    )

    assert json.loads(capsys.readouterr().out)["ref"] == "minds-v0.3.9"


def test_already_current_message_only_blames_the_app_when_it_is_to_blame() -> None:
    held_back = update_target.already_current_message(
        "minds-v0.3.9", "minds-v0.4.0", "minds-v0.3.9", True
    )
    assert "minds-v0.3.9" in held_back and "minds-v0.4.0" in held_back
    assert "needs a newer app" in held_back

    current = update_target.already_current_message(
        "minds-v0.3.9", "minds-v0.3.9", "minds-v0.3.9", False
    )
    assert "nothing to update" in current
    assert "newer app" not in current


def test_resolve_target_cli_exits_nonzero_with_a_readable_message_when_blocked(
    tmp_path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    _install_fake_latchkey(
        monkeypatch, tmp_path / "bin", body="", status="000", exit_code=7
    )

    assert (
        update_self.main(["resolve-target", "--local-tags", "--repo-root", str(repo)])
        == 1
    )

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "could not reach the minds app" in captured.err
    # A refusal, not a crash: no traceback for the lead to relay.
    assert "Traceback" not in captured.err


# --- classify_path ---------------------------------------------------------


def test_classify_path_reveal_classes() -> None:
    cases = {
        "system/apps/system_interface/src/App.tsx": update_classification.CLASS_SYSTEM_INTERFACE,
        "system/supervisord.conf": update_classification.CLASS_SERVICE,
        "system/libs/bootstrap/src/bootstrap/main.py": update_classification.CLASS_SERVICE,
        "system/vendor/mngr/libs/mngr/foo.py": update_classification.CLASS_EDITABLE_TOOL,
        "system/scripts/forward_port.py": update_classification.CLASS_SHARED_RUNTIME,
        ".agents/skills/update-self/SKILL.md": update_classification.CLASS_SHARED_RUNTIME,
        "system/services/oom_priority/src/oom_priority/ledger.py": update_classification.CLASS_SHARED_RUNTIME,
        # Provisioning files: the toolchain entry point (would otherwise read as
        # shared_runtime under system/scripts/) and the .mngr/ create config (would
        # otherwise fall through to other) -- both need the provisioner reveal.
        # The installers the entry point chains stay shared_runtime; the apply
        # keys its provisioner re-run on them separately (read_provisioner_inputs).
        "system/scripts/setup_system.sh": update_classification.CLASS_PROVISIONER,
        "system/scripts/install_secret_scanners.sh": update_classification.CLASS_SHARED_RUNTIME,
        "system/scripts/_provision_guard.sh": update_classification.CLASS_SHARED_RUNTIME,
        ".mngr/settings.toml": update_classification.CLASS_PROVISIONER,
        "system/Dockerfile": update_classification.CLASS_DOCKERFILE,
        "CLAUDE.md": update_classification.CLASS_DOCS,
        "changelog/some-entry.md": update_classification.CLASS_DOCS,
        "system/config/parent.toml": update_classification.CLASS_OTHER,
        # A README is docs even under a prefix with its own reveal class --
        # it must never trigger that class's reveal action (e.g. a service
        # restart for system/libs/bootstrap/README.md).
        "system/libs/bootstrap/README.md": update_classification.CLASS_DOCS,
        "system/apps/system_interface/README.md": update_classification.CLASS_DOCS,
        "system/vendor/mngr/README.md": update_classification.CLASS_DOCS,
        # Changelog entries likewise, in every project's bucket -- a release
        # ships them under runtime prefixes, so without this nearly every update
        # would restart a service (or run an impact analysis) over markdown.
        ".agents/changelog/some-entry.md": update_classification.CLASS_DOCS,
        "system/libs/bootstrap/changelog/some-entry.md": update_classification.CLASS_DOCS,
        "system/apps/system_interface/changelog/some-entry.md": update_classification.CLASS_DOCS,
        # But the match is one level deep and markdown-only, so an app that
        # happens to be *named* changelog still reveals as code.
        "system/apps/changelog/main.py": update_classification.CLASS_SHARED_RUNTIME,
    }
    for path, expected in cases.items():
        assert update_classification.classify_path(path).reveal_class == expected, path


def test_classify_path_project_mapping() -> None:
    assert (
        update_classification.classify_path(
            "system/apps/system_interface/foo.py"
        ).project
        == "system/apps/system_interface"
    )
    assert (
        update_classification.classify_path("system/vendor/mngr/x.py").project
        == "system/vendor/mngr"
    )
    assert (
        update_classification.classify_path("system/scripts/forward_port.py").project
        == "."
    )


def test_classify_path_manifest_flag() -> None:
    assert update_classification.classify_path(
        "system/apps/system_interface/pyproject.toml"
    ).is_manifest
    assert update_classification.classify_path(
        "system/vendor/mngr/libs/mngr/pyproject.toml"
    ).is_manifest
    assert not update_classification.classify_path(
        "system/scripts/forward_port.py"
    ).is_manifest


# --- classify_merge --------------------------------------------------------


def test_classify_merge_splits_merged_and_pulled_in() -> None:
    upstream_changed = [
        "system/apps/system_interface/src/App.tsx",  # also local -> merged
        "system/scripts/forward_port.py",  # upstream only -> pulled in
        "system/supervisord.conf",  # upstream only -> pulled in
    ]
    local_changed = [
        "system/apps/system_interface/src/App.tsx",
        "PURPOSE.md",  # local only, not an upstream update -> ignored
    ]
    result = update_classification.classify_merge(upstream_changed, local_changed)

    merged_paths = [entry["path"] for entry in result.merged]
    pulled_paths = [entry["path"] for entry in result.pulled_in]
    assert merged_paths == ["system/apps/system_interface/src/App.tsx"]
    assert pulled_paths == ["system/scripts/forward_port.py", "system/supervisord.conf"]
    # A file only local changed is not surfaced as an upstream update at all.
    assert "PURPOSE.md" not in merged_paths + pulled_paths
    # Any both-sides file means merge work happened, so the review gates run.
    assert result.has_merge_work is True


def test_classify_merge_summary_fields() -> None:
    upstream_changed = [
        "system/apps/system_interface/src/App.tsx",  # merged
        "system/vendor/mngr/libs/mngr/foo.py",  # merged
        "system/scripts/forward_port.py",  # pulled in
    ]
    local_changed = [
        "system/apps/system_interface/src/App.tsx",
        "system/vendor/mngr/libs/mngr/foo.py",
    ]
    result = update_classification.classify_merge(upstream_changed, local_changed)
    assert result.reveal_classes_merged == [
        update_classification.CLASS_EDITABLE_TOOL,
        update_classification.CLASS_SYSTEM_INTERFACE,
    ]
    assert result.reveal_classes_pulled_in == [
        update_classification.CLASS_SHARED_RUNTIME
    ]
    assert result.projects_to_validate == [
        "system/apps/system_interface",
        "system/vendor/mngr",
    ]


def test_classify_merge_surfaces_provisioner_bump() -> None:
    # The motivating case: upstream bumps the pinned latchkey version in
    # system/scripts/setup_system.sh and touches .mngr/settings.toml, local left both
    # untouched. They come in as a clean pull, but must still surface under the
    # provisioner reveal class (not shared_runtime/other) so the flow re-runs the
    # provisioner or flags a rebuild rather than silently dropping the new pin.
    result = update_classification.classify_merge(
        ["system/scripts/setup_system.sh", ".mngr/settings.toml"], []
    )
    assert result.reveal_classes_pulled_in == [update_classification.CLASS_PROVISIONER]
    assert [entry["reveal_class"] for entry in result.pulled_in] == [
        update_classification.CLASS_PROVISIONER,
        update_classification.CLASS_PROVISIONER,
    ]


def test_classify_merge_reports_no_merge_work_on_a_pure_clean_pull() -> None:
    # No file diverged on both sides: everything arrives exactly as upstream
    # shipped it, so the mechanical half of the review-gate rule clears. Local
    # changes to files upstream did NOT touch do not flip it -- they are not
    # part of the merge at all.
    result = update_classification.classify_merge(
        ["system/scripts/forward_port.py", "system/supervisord.conf"],
        ["PURPOSE.md", "system/apps/my_app/server.py"],
    )
    assert result.merged == []
    assert result.has_merge_work is False


def test_classify_merge_empty() -> None:
    result = update_classification.classify_merge([], [])
    assert result.merged == []
    assert result.pulled_in == []
    assert result.projects_to_validate == []
    assert result.has_merge_work is False


# --- CLI wiring --------------------------------------------------------------


def test_repo_root_flag_accepted_before_and_after_subcommand(tmp_path, capsys) -> None:
    # `--repo-root` must work both before and after the subcommand. Each
    # ordering has broken in its own way: a value after the subcommand errored
    # when the option lived only on the top parser, and a value *before* it was
    # silently clobbered back to cwd by the subparser's default on
    # Python < 3.13 (bpo-9351). Asserting on the resolved tag (which only
    # exists in the tmp repo) catches both -- a clobber would resolve against
    # the real repo and either fail or print a different ref.
    #
    # The tag has to sit on an *unmerged* line: a tag on HEAD is a target the
    # workspace already has, which resolve-target refuses, and this test is about
    # the flag plumbing rather than that refusal.
    _init_workspace_repo(tmp_path, merged_tags=(), unmerged_tags=("minds-v0.1.0",))

    # ``--ceiling main`` pins a non-release ceiling (i.e. no cap), so this test
    # stays about the ``--repo-root`` plumbing and never reaches for the app.
    for argv in (
        [
            "resolve-target",
            "--local-tags",
            "--ceiling",
            "main",
            "--repo-root",
            str(tmp_path),
        ],
        [
            "--repo-root",
            str(tmp_path),
            "resolve-target",
            "--local-tags",
            "--ceiling",
            "main",
        ],
    ):
        assert update_self.main(argv) == 0, argv
        assert '"minds-v0.1.0"' in capsys.readouterr().out, argv


def test_changelog_entries_collects_every_bucket_not_just_top_level(
    tmp_path, capsys
) -> None:
    # Per-PR changelog entries live in a ``changelog/`` dir under each project
    # bucket, not only the legacy top-level ``changelog/``. The command must
    # surface entries from every bucket -- else the update-self "what's new"
    # digest silently drops everything on the current (bucketed) convention --
    # while ignoring the vendored subtree's separate changelog system and files
    # that only happen to sit next to a changelog dir.
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    def _write(rel: str, text: str = "entry\n") -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    # Base commit: one pre-existing top-level entry (must NOT be reported as
    # newly added), plus a source file the target will leave untouched.
    _write("changelog/old-entry.md")
    _write("system/apps/browser/src/browser/session.py", "print('hi')\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "base")
    _git("tag", "base")

    # Target commit: newly-added entries across every bucket, a vendored-subtree
    # entry (excluded), and a non-changelog source change (ignored).
    _write(".agents/changelog/my-branch.md")
    _write("system/changelog/my-branch.md")
    _write("system/apps/browser/changelog/my-branch.md")
    _write("system/apps/system_interface/changelog/my-branch.md")
    _write("system/services/gamma/changelog/my-branch.md")
    _write("system/vendor/mngr/libs/mngr/changelog/upstream-entry.md")
    _write("system/apps/browser/src/browser/session.py", "print('bye')\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "target")
    _git("tag", "target")

    assert (
        update_self.main(
            [
                "changelog-entries",
                "--base",
                "base",
                "--target",
                "target",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    added = json.loads(capsys.readouterr().out)["added"]
    assert sorted(added) == [
        ".agents/changelog/my-branch.md",
        "system/apps/browser/changelog/my-branch.md",
        "system/apps/system_interface/changelog/my-branch.md",
        "system/changelog/my-branch.md",
        "system/services/gamma/changelog/my-branch.md",
    ]

    # The worker guide has the tool run from the staged skill's scripts
    # directory. A git pathspec is cwd-relative, so from a subdirectory the
    # changelog glob matched nothing and the digest was silently empty.
    subdir = tmp_path / "system" / "apps" / "browser"
    assert (
        update_self.main(
            [
                "changelog-entries",
                "--base",
                "base",
                "--target",
                "target",
                "--repo-root",
                str(subdir),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["added"] == added


def test_classify_merge_refuses_a_local_that_already_contains_the_target(
    tmp_path, capsys
) -> None:
    # After the worker adds any commit on top of its merge, the guide's
    # `--local HEAD^1` re-run points at the merge commit itself -- which
    # contains the target, so the merge base collapses to the target and the
    # classification silently prints empty over a real merge (this reported
    # zero changed files over an 818-file merge in a real incident). That
    # degenerate invocation must be a loud error, not an empty answer.
    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    def _write(rel: str, text: str) -> None:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    _write("shared.txt", "base\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "base")
    _git("checkout", "-q", "-b", "upstream-line")
    _write("upstream.txt", "upstream change\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "upstream")
    _git("tag", "target")
    _git("checkout", "-q", "-")
    _git("merge", "-q", "--no-ff", "--no-edit", "target")
    _write("extra.txt", "worker follow-up\n")
    _git("add", "-A")
    _git("commit", "-q", "-m", "follow-up")

    # HEAD^1 is now the merge commit, which contains the target: refused.
    code = update_self.main(
        [
            "classify-merge",
            "--local",
            "HEAD^1",
            "--target",
            "target",
            "--repo-root",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 1
    assert "already contains --target" in captured.err
    assert "first parent" in captured.err
    assert captured.out == ""

    # The correct invocation -- the merge commit's own first parent -- still
    # answers, and sees the upstream change.
    code = update_self.main(
        [
            "classify-merge",
            "--local",
            "HEAD^^1",
            "--target",
            "target",
            "--repo-root",
            str(tmp_path),
        ]
    )
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    assert [entry["path"] for entry in result["pulled_in"]] == ["upstream.txt"]


# --- bootstrap-skill --------------------------------------------------------


def _init_repo_with_skill(root: Path, skill_body: str) -> None:
    """Init a git repo at ``root`` carrying the update-self skill, tagged v1."""

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    skill_dir = root / update_self.SKILL_DIR_REL
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(skill_body, encoding="utf-8")
    (skill_dir / "scripts" / "update_self.py").write_text("# v1\n", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-q", "-m", "add skill")
    _git("tag", "minds-v1.0.0")


def test_bootstrap_skill_extracts_tag_copy_and_flags_difference(
    tmp_path, capsys
) -> None:
    # The tag carries the "original" skill; local then edits SKILL.md, so the
    # bootstrap must extract the *tag's* copy (unchanged body) and report that it
    # differs from the drifted local copy.
    repo = tmp_path / "repo"
    _init_repo_with_skill(repo, skill_body="ORIGINAL FLOW\n")
    (repo / update_self.SKILL_DIR_REL / "SKILL.md").write_text(
        "LOCALLY EDITED FLOW\n", encoding="utf-8"
    )

    dest = tmp_path / "staging"
    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v1.0.0",
                "--dest",
                str(dest),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["differs"] is True
    assert payload["ref"] == "minds-v1.0.0"
    staged_skill = Path(payload["skill_dir"])
    # The staged copy is the tag's content, not the drifted local edit.
    assert staged_skill.joinpath("SKILL.md").read_text() == "ORIGINAL FLOW\n"


def test_bootstrap_skill_reports_no_difference_when_local_matches_tag(
    tmp_path, capsys
) -> None:
    repo = tmp_path / "repo"
    _init_repo_with_skill(repo, skill_body="STABLE FLOW\n")

    dest = tmp_path / "staging"
    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v1.0.0",
                "--dest",
                str(dest),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["differs"] is False
    # Even when identical, the fixed path is left populated with a runnable copy --
    # the flow always dispatches from it, so it must never be empty.
    staged_skill = Path(payload["skill_dir"])
    assert staged_skill == dest / update_self.SKILL_DIR_REL
    assert staged_skill.joinpath("SKILL.md").read_text() == "STABLE FLOW\n"


def test_bootstrap_skill_ignores_untracked_build_artifacts(tmp_path, capsys) -> None:
    # Importing the script drops __pycache__/*.pyc into the skill's scripts/. Those are
    # untracked, so `git diff` ignores them and they must not register as a
    # spurious difference -- otherwise the "identical -> stay on the local flow"
    # branch would be dead in every real checkout (where the module has been
    # imported at least once).
    repo = tmp_path / "repo"
    _init_repo_with_skill(repo, skill_body="STABLE FLOW\n")
    pycache = repo / update_self.SKILL_DIR_REL / "scripts" / "__pycache__"
    pycache.mkdir()
    (pycache / "update_self.cpython-313.pyc").write_bytes(b"\x00compiled\x00")

    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v1.0.0",
                "--dest",
                str(tmp_path / "staging"),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["differs"] is False


def test_bootstrap_skill_stages_local_copy_when_ref_predates_skill(
    tmp_path, capsys
) -> None:
    # A ref with no update-self skill at all has no target copy to hand off to, so
    # the command stages the *local* copy at the fixed path (the flow always runs
    # from that one path) and reports differs=False so the caller stays on the
    # local flow.
    repo = tmp_path / "repo"

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)

    # Tag an empty root commit that predates the skill dir, then add the skill to
    # the working tree -- so `minds-v0.0.1` has no skill but the local copy does.
    repo.mkdir()
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    _git("commit", "--allow-empty", "-q", "-m", "root")
    _git("tag", "minds-v0.0.1")
    skill_dir = repo / update_self.SKILL_DIR_REL
    (skill_dir / "scripts").mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("LOCAL FLOW\n", encoding="utf-8")
    (skill_dir / "scripts" / "update_self.py").write_text("# local\n", encoding="utf-8")

    dest = tmp_path / "staging"
    assert (
        update_self.main(
            [
                "bootstrap-skill",
                "--ref",
                "minds-v0.0.1",
                "--dest",
                str(dest),
                "--repo-root",
                str(repo),
            ]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["differs"] is False
    staged_skill = Path(payload["skill_dir"])
    assert staged_skill == dest / update_self.SKILL_DIR_REL
    # The staged copy is the local working-tree flow, present and runnable.
    assert staged_skill.joinpath("SKILL.md").read_text() == "LOCAL FLOW\n"
    assert staged_skill.joinpath("scripts", "update_self.py").exists()


# --- is_held_back_by_ceiling ------------------------------------------------


def test_held_back_is_true_only_when_the_ceiling_chose_the_lower_target() -> None:
    assert (
        update_target.is_held_back_by_ceiling(
            resolved_ref="minds-v0.3.9",
            latest_available="minds-v0.4.0",
            ceiling="minds-v0.3.9",
            has_override=False,
        )
        is True
    )
    # Already on the newest release: nothing was held back.
    assert (
        update_target.is_held_back_by_ceiling(
            resolved_ref="minds-v0.4.0",
            latest_available="minds-v0.4.0",
            ceiling="minds-v0.4.0",
            has_override=False,
        )
        is False
    )


def test_held_back_is_false_when_the_users_own_override_picked_the_older_tag() -> None:
    """The bug this flag exists to prevent: blaming the app for the user's choice.

    `--override minds-v0.3.6` under a `minds-v0.3.9` ceiling leaves `ref` below
    `latest_available`, so an eyeball comparison would tell the user their Minds
    app held the update back when they picked the older tag themselves.
    """
    assert (
        update_target.is_held_back_by_ceiling(
            resolved_ref="minds-v0.3.6",
            latest_available="minds-v0.4.0",
            ceiling="minds-v0.3.9",
            has_override=True,
        )
        is False
    )


def test_held_back_is_false_when_the_app_imposes_no_cap() -> None:
    """A dev app caps nothing, so a gap can never be the ceiling's doing.

    A dev build reports a *branch*, not nothing, so `ceiling="main"` -- and not
    `None` -- is the shape the CLI actually produces here. It reaches `False` by a
    different route than a `None` ceiling does: the branch parses to no version, so
    the selection was never bounded. Both routes are asserted.
    """
    assert (
        update_target.is_held_back_by_ceiling(
            resolved_ref="minds-v0.4.0",
            latest_available="minds-v0.4.0",
            ceiling="main",
            has_override=False,
        )
        is False
    )
    # No ceiling supplied at all -- only a direct caller does this.
    assert (
        update_target.is_held_back_by_ceiling(
            resolved_ref="minds-v0.4.0",
            latest_available="minds-v0.4.0",
            ceiling=None,
            has_override=False,
        )
        is False
    )


# --- a prerelease ceiling ---------------------------------------------------


def test_prerelease_ceiling_caps_rather_than_disabling_the_cap() -> None:
    """An app on a release candidate is a real app and must still cap its workspaces.

    Parsing the ceiling as "not a stable tag, therefore no ceiling" would let a
    workspace on an rc app update arbitrarily far past it.
    """
    tags = ["minds-v0.3.9", "minds-v0.4.0", "minds-v0.4.1"]
    # Semver: 0.4.0-rc1 precedes 0.4.0, so 0.4.0 itself is above this ceiling.
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.4.0-rc1")
        == "minds-v0.3.9"
    )
    result = update_target.resolve_target(None, tags, ceiling="minds-v0.4.0-rc1")
    assert result.ref == "minds-v0.3.9"
    assert result.ceiling == "minds-v0.4.0-rc1"


def test_a_prerelease_ceiling_still_admits_its_own_earlier_releases() -> None:
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.4.1-rc1")
        == "minds-v0.4.0"
    )


def test_capping_by_a_prerelease_does_not_make_prereleases_selectable() -> None:
    # The ceiling widening to prereleases must not widen *candidate* selection:
    # the default target is still only ever a stable release.
    tags = ["minds-v0.3.9", "minds-v0.4.0-rc1", "minds-v0.4.0-rc2"]
    assert (
        update_target.pick_latest_stable_tag(tags, ceiling="minds-v0.4.0-rc2")
        == "minds-v0.3.9"
    )


def test_parse_version_orders_prereleases_semver_style() -> None:
    below = update_target.parse_version("minds-v0.4.0-rc1")
    above = update_target.parse_version("minds-v0.4.0")
    assert below is not None and above is not None
    # A prerelease sorts below the release it precedes.
    assert below < above
    # Numeric identifiers compare numerically, not lexically: rc.10 follows rc.2.
    rc2 = update_target.parse_version("minds-v0.4.0-rc.2")
    rc10 = update_target.parse_version("minds-v0.4.0-rc.10")
    assert rc2 is not None and rc10 is not None
    assert rc2 < rc10
    # A branch or bare commit has no version at all, and stays uncomparable.
    assert update_target.parse_version("main") is None
    assert update_target.parse_version("abc1234") is None


# --- SKILL.md task-file template cross-version contract --------------------


def test_skill_md_task_template_carries_the_lead_agent_and_report_fields() -> None:
    """The SKILL.md task-file heredoc must keep `lead_agent` and `finish_report_path`.

    This SKILL.md is executed cross-version: an OLDER workspace's lead follows
    this (staged, target-version) prose but launches with its own, possibly
    pre-lead-agent-stamping `create_worker.py` -- so the template itself is the
    only thing that gives the worker a report address there. Removing either
    line reintroduces the v0.3.11 -> v0.3.16 failure where the worker finished
    but could never deliver its report and the lead waited out the full
    timeout in silence.
    """
    skill_md = (_MODULE_PATH.parent.parent / "SKILL.md").read_text(encoding="utf-8")
    start = skill_md.index("cat << FRONTMATTER_EOF")
    end = skill_md.index("FRONTMATTER_EOF", start + len("cat << FRONTMATTER_EOF"))
    frontmatter_template = skill_md[start:end]
    assert "lead_agent: $MNGR_AGENT_NAME" in frontmatter_template
    assert "finish_report_path: " in frontmatter_template


def test_skill_md_runs_its_scripts_from_the_staged_copy_below_step_3() -> None:
    """Every ``update_self.py`` invocation from Step 3 on must use the staged copy.

    From Step 3 the lead follows the staged target-version prose, but a relative
    ``.agents/skills/update-self/scripts/update_self.py`` resolves to the
    *workspace's own* copy, which is whatever release the workspace is still
    on. For the ``run-status`` writes (delegate, hold, resume, verdict) that
    copy may predate the subcommand entirely, so a local-path invocation fails
    exactly on the first update into the release that ships it -- the runs
    those records exist for. Only Step 1's ``run-status start`` runs before
    anything is staged and stays local.
    """
    skill_dir = _MODULE_PATH.parent.parent
    skill_md = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    below_step_3 = skill_md[skill_md.index("## 3. Dispatch the worker") :]
    # The references are all read after the handoff, so every invocation in
    # them is held to the same rule.
    prose = [below_step_3] + [
        path.read_text(encoding="utf-8")
        for path in sorted((skill_dir / "references").glob("*.md"))
    ]
    invocations = [
        line
        for text in prose
        for line in text.replace("\\\n", " ").splitlines()
        if line.startswith("python3 ") and "update_self.py" in line
    ]
    # The commands this guard exists for; without them it is vacuous.
    assert any("run-status delegate" in line for line in invocations)
    assert any("run-status hold" in line for line in invocations)
    staged = "data/.tasks/update-self/skill-at-target/.agents/skills/update-self/scripts/update_self.py"
    strays = [line for line in invocations if staged not in line]
    assert strays == []


# ==== The atomic apply =========================================================
#
# The orchestration tests inject a recording ``Runner`` (so no real
# ``git``/``npm``/``uv``/``mngr`` runs), a programmable ``HttpClient``, a fake
# ``Spawner``, and a no-op sleeper, and run against a real temporary repo
# directory. The recording runner emulates the build tool's destructive
# behaviour (emptying the bundle directory before writing), so a working
# rollback is distinguishable from one that never deleted anything.

_ROLLBACK = "abc123def456"
_MERGE_REF = "mngr/update-self"
_LIVE_BASE = "http://test-live"
_ASSET_NAME = "index-abc123.js"
_TODAY = "2026-08-19"


def _write_bundle(repo_root: Path, stamp: str | None = None) -> None:
    static = repo_root / update_layout.STATIC_DIR
    (static / "assets").mkdir(parents=True, exist_ok=True)
    (static / "index.html").write_text(
        f'<!doctype html><html><head><script type="module" src="/assets/{_ASSET_NAME}">'
        "</script></head><body></body></html>"
    )
    (static / "assets" / _ASSET_NAME).write_text("console.log('app');")
    if stamp is not None:
        (static / update_layout.BUNDLE_STAMP_FILENAME).write_text(stamp + "\n")


def _bundle_exists(repo_root: Path) -> bool:
    return (repo_root / update_layout.FRONTEND_BUILD_INDEX).exists()


def _installed_stamp(repo_root: Path) -> str | None:
    """The served bundle's source stamp: what tells the pre-apply copy (the
    fixture writes none) from a bundle the emulated build produced."""
    return update_apply._read_bundle_stamp(repo_root / update_layout.STATIC_DIR)


def _make_apply_repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / update_layout.FRONTEND_DIR).mkdir(parents=True)
    return repo_root


@pytest.fixture
def apply_repo(tmp_path: Path) -> Path:
    """A repo root that already serves a built bundle, as a live workspace does."""
    repo_root = _make_apply_repo(tmp_path)
    _write_bundle(repo_root)
    return repo_root


@pytest.fixture
def unbuilt_apply_repo(tmp_path: Path) -> Path:
    """A repo root that has never built a bundle, so there is none to snapshot."""
    return _make_apply_repo(tmp_path)


def _unwrap_expendable(argv: list[str]) -> list[str]:
    """Strip the ``sh -c <tag> sh <argv...>`` wrapper the hungry steps carry."""
    if argv[:2] == ["sh", "-c"] and argv[3:4] == ["sh"]:
        return argv[4:]
    return argv


def _tagging_expend(argv: Sequence[str]) -> list[str]:
    """A recognizable stand-in for ``as_expendable`` (the real one is inert when
    the tree carries no oom_priority package, as these temp repos do not)."""
    return ["sh", "-c", "expendable-tag", "sh", *argv]


@dataclass
class _Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class _RecordingRunner(update_runtime.Runner):
    """Records every ``run`` call; returns canned results keyed by argv prefix.

    A response may be a single ``_Result`` or a list consumed in order (the
    last entry repeats). When ``repo_root`` is set, ``npm run build`` also
    *behaves* like the real build tool: it empties the bundle directory first
    and only writes a new bundle if the canned result is a success.
    ``on_command`` (when set) observes every unwrapped argv as it runs -- used
    to capture mid-flight state like the marker's existence at merge time.
    """

    calls: list[list[str]] = field(default_factory=list)
    raw_calls: list[list[str]] = field(default_factory=list)
    envs: list[dict | None] = field(default_factory=list)
    timeouts: list[float | None] = field(default_factory=list)
    executables: dict[str, str] = field(default_factory=dict)
    repo_root: Path | None = None
    is_build_output_written: bool = True
    # What the emulated build's postbuild step stamps the bundle with (None =
    # a build with no git repo, which writes no stamp).
    build_stamp: str | None = None
    on_command: Callable[[list[str]], None] | None = None
    _responses: dict[tuple[str, ...], object] = field(default_factory=dict)

    def respond(self, prefix: tuple[str, ...], result: object) -> None:
        self._responses[prefix] = result

    def which(self, executable: str) -> str | None:
        return self.executables.get(executable)

    def run_process_group(self, argv: Sequence[str], **kwargs) -> _Result:
        return self.run(argv, **kwargs)

    def run(self, argv: Sequence[str], **kwargs) -> _Result:
        self.raw_calls.append(list(argv))
        argv_list = _unwrap_expendable(list(argv))
        self.calls.append(argv_list)
        self.envs.append(kwargs.get("env"))
        self.timeouts.append(kwargs.get("timeout"))
        if self.on_command is not None:
            self.on_command(argv_list)
        result = self._canned_result(argv_list)
        if argv_list[:3] == ["npm", "run", "build"] and self.repo_root is not None:
            self._emulate_build(result.returncode == 0)
        return result

    def _canned_result(self, argv_list: list[str]) -> _Result:
        # Longest prefix wins, so a test can narrow one of the broad defaults
        # `_apply_runner` registers (e.g. ("git", "log")) for a single command.
        by_specificity = sorted(
            self._responses.items(), key=lambda item: len(item[0]), reverse=True
        )
        for prefix, result in by_specificity:
            if tuple(argv_list[: len(prefix)]) == prefix:
                if isinstance(result, list):
                    result = result.pop(0) if len(result) > 1 else result[0]
                if isinstance(result, BaseException):
                    raise result
                assert isinstance(result, _Result)
                return result
        return _Result()

    def _emulate_build(self, is_successful: bool) -> None:
        assert self.repo_root is not None
        static = self.repo_root / update_layout.STATIC_DIR
        # vite's `emptyOutDir: true` -- the output is destroyed before any new
        # output is written, so a failure part-way through leaves nothing.
        shutil.rmtree(static, ignore_errors=True)
        if is_successful and self.is_build_output_written:
            _write_bundle(self.repo_root, self.build_stamp)

    def argvs_starting(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if tuple(c[: len(prefix)]) == prefix]

    def ran(self, *prefix: str) -> bool:
        return bool(self.argvs_starting(*prefix))


class _FakeHttp(update_runtime.HttpClient):
    """Returns whatever ``responder(url)`` yields for the health-probe GETs;
    ``page_responder`` drives the frontend probe and defaults to a healthy
    built app shell."""

    def __init__(
        self,
        responder: Callable[[str], int | None],
        page_responder: Callable[[str], update_runtime.FetchedPage | None]
        | None = None,
    ) -> None:
        self._responder = responder
        self._page_responder = page_responder or _built_app_page
        self.get_urls: list[str] = []
        self.page_urls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int | None:
        self.get_urls.append(url)
        return self._responder(url)

    def get_page(self, url: str, timeout: float) -> update_runtime.FetchedPage | None:
        self.page_urls.append(url)
        return self._page_responder(url)


def _built_app_page(url: str) -> update_runtime.FetchedPage:
    if url.endswith(".js"):
        return update_runtime.FetchedPage(
            status=200,
            body="console.log('app');",
            headers={"content-type": "text/javascript"},
        )
    return update_runtime.FetchedPage(
        status=200,
        body=f'<!doctype html><script type="module" src="/assets/{_ASSET_NAME}"></script>',
        headers={
            "content-type": "text/html",
            update_probes.FRONTEND_BUILT_HEADER: "true",
        },
    )


def _placeholder_page(url: str) -> update_runtime.FetchedPage:
    return update_runtime.FetchedPage(
        status=200,
        body="<!doctype html><p>Frontend not built</p>",
        headers={
            "content-type": "text/html",
            update_probes.FRONTEND_BUILT_HEADER: "false",
        },
    )


@dataclass
class _FakeSpawned:
    output: str = ""
    exited: bool = False
    terminated: bool = False

    def terminate(self) -> None:
        self.terminated = True

    def has_exited(self) -> bool:
        return self.exited

    def read_output(self) -> str:
        return self.output


@dataclass
class _FakeSpawner(update_runtime.Spawner):
    output: str = ""
    exited: bool = False
    spawns: list[list[str]] = field(default_factory=list)
    raw_spawns: list[list[str]] = field(default_factory=list)
    envs: list[dict] = field(default_factory=list)
    last: _FakeSpawned | None = None
    # A boot that never starts, as subprocess reports one: not an exit code.
    spawn_error: OSError | None = None

    def spawn(
        self, argv: Sequence[str], cwd: str, env: dict, output_path: Path
    ) -> _FakeSpawned:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.raw_spawns.append(list(argv))
        self.spawns.append(_unwrap_expendable(list(argv)))
        self.envs.append(dict(env))
        self.last = _FakeSpawned(output=self.output, exited=self.exited)
        return self.last


class _Clock:
    """A deterministic stand-in for ``time.time`` the tests can advance."""

    def __init__(self, start: float = 1000.0) -> None:
        self.value = start

    def __call__(self) -> float:
        self.value += 1.0
        return self.value


def _apply_runner(name_status: str, repo_root: Path) -> _RecordingRunner:
    runner = _RecordingRunner(repo_root=repo_root)
    # Clean for the precondition check. The rollback's own "is anything staged
    # to commit" question is the index diff, and by then the restore has
    # staged the reverted paths.
    runner.respond(("git", "status", "--porcelain"), _Result(stdout=""))
    runner.respond(("git", "diff", "--cached", "--quiet"), _Result(returncode=1))
    # Not yet merged: the merge-base ancestor check answers "no".
    runner.respond(("git", "merge-base", "--is-ancestor"), _Result(returncode=1))
    # No merge left staged by a killed apply (the MERGE_HEAD lookup).
    runner.respond(("git", "rev-parse", "--verify"), _Result(returncode=1))
    runner.respond(("git", "rev-parse", "--short=7"), _Result(stdout="abc1234"))
    runner.respond(("git", "rev-parse", "HEAD"), _Result(stdout=_ROLLBACK))
    runner.respond(("git", "rev-parse", _MERGE_REF), _Result(stdout="fedcba9876543"))
    runner.respond(("git", "diff"), _Result(stdout=name_status))
    runner.respond(
        ("git", "log"), _Result(stdout=f"{_ROLLBACK} Initial workspace commit")
    )
    runner.respond(("git", "rev-list"), _Result(stdout=_ROLLBACK))
    runner.respond(("git", "describe"), _Result(returncode=128))
    return runner


def _apply(
    runner: _RecordingRunner,
    http: _FakeHttp,
    spawner: _FakeSpawner,
    repo_root: Path,
    *,
    merge_ref: str = _MERGE_REF,
    ff_only: bool = True,
    worker_bundle: str | None = None,
    target_ref: str | None = None,
    is_pid_live: Callable[[int], bool] = lambda pid: False,
    expend: Callable[[Sequence[str]], list[str]] = _tagging_expend,
) -> int:
    return update_apply.apply_update(
        merge_ref,
        repo_root,
        ff_only=ff_only,
        worker_bundle=worker_bundle,
        target_ref=target_ref,
        runner=runner,
        http=http,
        spawner=spawner,
        sleeper=lambda _seconds: None,
        base_url=_LIVE_BASE,
        now=_Clock(),
        today=_TODAY,
        is_pid_live=is_pid_live,
        expend=expend,
    )


def _all_healthy(_url: str) -> int:
    return 200


def _no_sleep(_seconds: float) -> None:
    return None


def _is_live(url: str) -> bool:
    return url.startswith(_LIVE_BASE)


def _snapshot_copy(repo_root: Path, name: str) -> Path:
    return (
        repo_root
        / update_apply_contract.STATE_DIR_REL
        / update_apply_contract.SNAPSHOTS_DIRNAME
        / name
    )


def _placeholder_after(runner: _RecordingRunner, *prefix: str):
    """A page responder that serves the placeholder once ``prefix`` has run.

    The shape every regression test needs: a healthy app shell for the
    baseline probe, and a broken one from the step under test onwards.
    """

    def page_responder(url: str) -> update_runtime.FetchedPage:
        return _placeholder_page(url) if runner.ran(*prefix) else _built_app_page(url)

    return page_responder


def _refreshed_the_view(runner: _RecordingRunner, repo_root: Path) -> bool:
    return runner.ran(
        sys.executable, str(repo_root / "system/scripts/refresh_workspace_view.py")
    )


def _marker_exists(repo_root: Path) -> bool:
    return update_apply_contract.marker_path(repo_root).exists()


def _plant_marker(
    repo_root: Path,
    *,
    dri_agent: str = "the-lead",
    merge_ref: str = _MERGE_REF,
    phase: str = update_apply_contract.PHASE_BUILT,
    pid: int = 12345,
    updated_at: float = 1000.0,
    live_service_restarted: bool = False,
    provisioner_ran: bool = False,
    snapshots: list | None = None,
    frontend_expected: bool | None = True,
) -> "update_apply_contract.ApplyMarker":
    """Write the marker an interrupted apply would have left behind."""
    marker = update_apply_contract.ApplyMarker(
        dri_agent=dri_agent,
        rollback_to=_ROLLBACK,
        merge_ref=merge_ref,
        target_ref=None,
        ff_only=True,
        worker_bundle=None,
        phase=phase,
        pid=pid,
        started_at=updated_at - 10,
        updated_at=updated_at,
        provisioner_ran=provisioner_ran,
        live_service_restarted=live_service_restarted,
        frontend_expected=frontend_expected,
        snapshots=snapshots or [],
    )
    update_apply_contract.write_marker(marker, repo_root, now=lambda: updated_at)
    return marker


def _plant_snapshotted_marker(repo_root: Path, **kwargs) -> list:
    """Take a real pre-apply copy of the bundle, then plant a marker naming it.

    The state a frontend apply killed after its snapshot step leaves behind,
    and the starting point of every recover test that has something to restore.
    """
    plan = _plan(["system/apps/system_interface/frontend/src/App.ts"])
    snapshots = update_environment.take_snapshots(
        plan, repo_root, _RecordingRunner(), []
    )
    _plant_marker(repo_root, snapshots=snapshots, **kwargs)
    return snapshots


def _read_emergency(repo_root: Path) -> dict:
    return json.loads(update_apply_contract.emergency_path(repo_root).read_text())


_RESTART = ("mngr", "start", "--restart", "system-services")
_PROVISION = ("bash", update_layout.PROVISIONER_SCRIPT)

_FRONTEND_DIFF = "M\tsystem/apps/system_interface/frontend/src/views/Chat.ts\n"
_BACKEND_DIFF = "M\tsystem/apps/system_interface/imbue/system_interface/server.py\n"
_VENDORED_DIFF = "M\tsystem/vendor/mngr/libs/mngr/imbue/mngr/api/list.py\n"
_SETTINGS_DIFF = "M\t.mngr/settings.toml\n"
_APT_SNAPSHOT_DIFF = "M\t.mngr/apt-snapshot-timestamp\n"
_BACKEND_MANIFEST_DIFF = "M\tsystem/apps/system_interface/pyproject.toml\n"
_FRONTEND_MANIFEST_DIFF = "M\tsystem/apps/system_interface/frontend/package.json\n"
_DOCS_DIFF = "M\tREADME.md\nM\t.agents/changelog/some-entry.md\n"


# --- plan_apply ---------------------------------------------------------------

# The real tree's provisioner inputs: the entry point, the apt snapshot
# timestamp, and whatever setup_system.sh chains today.
_PROVISIONER_INPUTS = update_classification.read_provisioner_inputs(_WORKSPACE_ROOT)


def _plan(paths: list[str]) -> update_classification.ApplyPlan:
    return update_classification.plan_apply(paths, _PROVISIONER_INPUTS)


def test_plan_apply_maps_each_change_class() -> None:
    plan = _plan(
        [
            "system/apps/system_interface/frontend/src/views/Chat.ts",
            "system/apps/system_interface/imbue/system_interface/server.py",
            "system/apps/system_interface/frontend/package.json",
            "system/apps/system_interface/pyproject.toml",
            "system/scripts/setup_system.sh",
        ]
    )
    assert plan.frontend_src and plan.frontend_manifest
    assert plan.backend_src and plan.backend_manifest
    assert plan.provisioner


def test_plan_apply_keys_the_provisioner_run_on_what_it_reads() -> None:
    # The provisioner never reads the create config, so no re-run for it; the
    # apt snapshot timestamp is the one .mngr/ file it does read.
    assert not _plan([".mngr/settings.toml"]).provisioner
    assert _plan([".mngr/apt-snapshot-timestamp"]).provisioner
    # An installer the entry point chains counts even though the entry point
    # itself did not change: a pin bump in it alone must reinstall the binary.
    assert _plan(["system/scripts/install_secret_scanners.sh"]).provisioner
    # A sibling script the provisioner never runs does not.
    assert not _plan(["system/scripts/seed_home_skeleton.sh"]).provisioner


@pytest.mark.parametrize(
    "path",
    [
        # The app's own manifest, and the root ones the whole workspace
        # environment is resolved from.
        "system/apps/system_interface/pyproject.toml",
        "pyproject.toml",
        "uv.lock",
        # The vendored mngr is an editable install the backend imports, so its
        # workspace root and each of its libraries move the same closure.
        "system/vendor/mngr/pyproject.toml",
        "system/vendor/mngr/libs/mngr/pyproject.toml",
        # Not a Python manifest, but it is what the refresh unions into each
        # tool's reinstall, so it decides which packages the tool environments
        # carry. A release that only re-assigns an existing plugin to another
        # tool changes nothing else, and without this would never be registered.
        update_layout.PLUGIN_MANIFEST_PATH,
    ],
)
def test_plan_apply_counts_every_backend_manifest(path: str) -> None:
    # Missing one of these means skipping `uv sync` and restarting the
    # workspace against an environment resolved for the pre-merge tree.
    assert _plan([path]).backend_manifest


@pytest.mark.parametrize(
    "path",
    [
        # Not a manifest: a source file nested where one would be.
        "system/vendor/mngr/libs/mngr/imbue/mngr/api/list.py",
        # A pyproject one level deeper than a vendored library's own root.
        "system/vendor/mngr/libs/mngr/imbue/pyproject.toml",
    ],
)
def test_plan_apply_does_not_mistake_nested_paths_for_manifests(path: str) -> None:
    assert not _plan([path]).backend_manifest


@pytest.mark.parametrize(
    "path",
    [
        "system/apps/system_interface/frontend/src/views/Chat.ts",
        # Everything under frontend/ counts, not just src/: the entry
        # document, the build configs and the public assets all change the
        # emitted bundle.
        "system/apps/system_interface/frontend/index.html",
        "system/apps/system_interface/frontend/vite.config.ts",
        "system/apps/system_interface/frontend/tsconfig.json",
        "system/apps/system_interface/frontend/public/logo.svg",
    ],
)
def test_plan_apply_counts_every_frontend_file_not_just_src(path: str) -> None:
    plan = _plan([path])
    assert plan.frontend_src and not plan.frontend_manifest


@pytest.mark.parametrize(
    "path",
    [
        "system/apps/system_interface/imbue/system_interface/server_test.py",
        "system/apps/system_interface/imbue/system_interface/test_layout_pipeline.py",
    ],
)
def test_plan_apply_ignores_backend_test_files(path: str) -> None:
    # No running process imports these, so they cannot leave the live backend
    # stale -- and bouncing the services agent for one blips the user's UI.
    assert not _plan([path]).backend_src


# --- apply: happy paths per change class ---------------------------------------


def test_apply_frontend_only_builds_refreshes_and_restarts(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy)

    code = _apply(runner, http, _FakeSpawner(), apply_repo)

    assert code == 0
    assert runner.ran("npm", "run", "build")
    # Every apply restarts the services agent, a frontend-only one included.
    assert runner.ran(*_RESTART)
    assert not runner.ran(*_PROVISION)
    assert _refreshed_the_view(runner, apply_repo)
    assert runner.ran("git", "merge", "--ff-only", _MERGE_REF)
    assert _bundle_exists(apply_repo)
    # Every exit path clears the marker; success also discards the snapshots.
    assert not _marker_exists(apply_repo)
    assert not _snapshot_copy(apply_repo, "bundle").parent.exists()


def test_apply_backend_change_preflights_restarts_and_probes(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _apply(runner, http, spawner, apply_repo)

    assert code == 0
    # Pre-flight boots the bare tool on a throwaway port before the restart.
    assert spawner.spawns == [[update_layout.TOOL_NAME]]
    assert runner.ran(*_RESTART)
    assert any(
        _is_live(url) and update_probes.HEALTH_PATH in url for url in http.get_urls
    )
    assert not runner.ran("npm", "run", "build")


def test_apply_vendored_source_change_restarts_without_building(
    apply_repo: Path,
) -> None:
    # The geebspace lesson: vendored-mngr source is imported in-process by the
    # live system interface, so "picked up live" was never true -- it restarts.
    runner = _apply_runner(_VENDORED_DIFF, apply_repo)
    spawner = _FakeSpawner()

    code = _apply(runner, _FakeHttp(_all_healthy), spawner, apply_repo)

    assert code == 0
    assert runner.ran(*_RESTART)
    assert spawner.spawns  # pre-flighted before the restart
    assert not runner.ran("npm", "run", "build")
    assert not runner.ran("uv", "tool", "install")  # source-only: no env refresh


def test_apply_apt_snapshot_change_provisions_before_any_restart(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_APT_SNAPSHOT_DIFF + _BACKEND_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    provision_index = runner.calls.index(list(_PROVISION))
    restart_index = runner.calls.index(list(_RESTART))
    assert provision_index < restart_index


def test_apply_settings_change_restarts_without_a_provisioner_run(
    apply_repo: Path,
) -> None:
    # setup_system.sh never reads .mngr/settings.toml, so a settings-only
    # release must not pay its 1800s-budget run (and risk a spurious
    # provision-incomplete record); the live reader is bounced instead.
    runner = _apply_runner(_SETTINGS_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert not runner.ran(*_PROVISION)
    assert runner.ran(*_RESTART)


def test_apply_backend_manifest_refreshes_all_three_environments(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    installs = runner.argvs_starting("uv", "tool", "install")
    assert len(installs) == 2  # the vendored mngr tool and the app tool
    assert runner.ran("uv", "sync", "--all-packages", "--frozen")
    assert runner.ran(*_RESTART)


def test_apply_docs_only_still_preflights_restarts_and_probes(apply_repo: Path) -> None:
    # Nothing to build, refresh or provision -- but the restart is not keyed
    # on the diff (no per-path rule stays complete), so it happens anyway, with
    # the pre-flight and the probes around it.
    runner = _apply_runner(_DOCS_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy)
    spawner = _FakeSpawner()

    code = _apply(runner, http, spawner, apply_repo)

    assert code == 0
    assert runner.ran("git", "merge", "--ff-only", _MERGE_REF)
    assert not runner.ran("npm")
    assert not runner.ran(*_PROVISION)
    assert spawner.spawns == [[update_layout.TOOL_NAME]]
    assert runner.ran(*_RESTART)
    assert any(
        _is_live(url) and update_probes.HEALTH_PATH in url for url in http.get_urls
    )
    assert not _marker_exists(apply_repo)


def test_apply_ordinary_merge_mode_uses_no_ff(apply_repo: Path) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(
        runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo, ff_only=False
    )

    assert code == 0
    assert runner.ran("git", "merge", "--no-ff", "--no-edit", _MERGE_REF)
    assert not runner.ran("git", "merge", "--ff-only")


def test_apply_refused_merge_changes_nothing_and_exits_1(apply_repo: Path) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(
        ("git", "merge", "--ff-only"), _Result(returncode=128, stderr="not possible")
    )

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 1
    assert runner.ran("git", "merge", "--abort")
    assert not runner.ran("npm")
    assert not runner.ran(*_RESTART)
    assert not _marker_exists(apply_repo)


def test_re_applying_a_rolled_back_merge_refuses_instead_of_claiming_success(
    apply_repo: Path,
) -> None:
    # The rollback is a *forward revert*, so the reverted merge stays an
    # ancestor of HEAD. Without the guard, a re-run skips the merge, sees an
    # empty rollback_to..HEAD diff, reports "nothing live needed to change",
    # exits 0 and writes a version-history line for an update the tree does not
    # contain -- and the documented post-rollback retry is exactly a re-run.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("git", "merge-base", "--is-ancestor"), _Result(returncode=0))
    runner.respond(("git", "diff", "--no-renames"), _Result(stdout=""))
    runner.respond(
        ("git", "log", "--format=%s"),
        _Result(stdout="Roll back update apply (restore to abc123def456)\n"),
    )

    with pytest.raises(update_runtime.ApplyPreconditionError) as raised:
        _apply(
            runner,
            _FakeHttp(_all_healthy),
            _FakeSpawner(),
            apply_repo,
            target_ref="minds-v0.4.2",
        )

    assert "rolled back" in str(raised.value)
    assert not (apply_repo / "docs/VERSION_HISTORY.md").exists()
    assert not runner.ran("uv", "run", "env-converge")
    assert not _marker_exists(apply_repo)


def test_re_applying_an_already_applied_merge_is_still_a_no_op_not_a_refusal(
    apply_repo: Path,
) -> None:
    # The other side of the same guard: a merge that landed and stayed landed
    # has no rollback commit on top, so a re-run (e.g. after a kill between the
    # marker clear and the ledger write) still completes the bookkeeping.
    runner = _apply_runner(_DOCS_DIFF, apply_repo)
    runner.respond(("git", "merge-base", "--is-ancestor"), _Result(returncode=0))
    runner.respond(("git", "diff", "--no-renames"), _Result(stdout=""))
    runner.respond(
        ("git", "log", "--format=%s"), _Result(stdout="version history: x\n")
    )

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    assert not runner.ran("git", "merge", "--ff-only")
    assert (
        "updated to minds-v0.4.2"
        in (apply_repo / "docs/VERSION_HISTORY.md").read_text()
    )


def test_apply_dirty_tree_refuses_before_touching_anything(apply_repo: Path) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("git", "status", "--porcelain"), _Result(stdout=" M foo\n"))

    with pytest.raises(update_runtime.ApplyPreconditionError):
        _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert not runner.ran("git", "merge")


def test_apply_unresolvable_merge_ref_leaves_no_marker_behind(
    apply_repo: Path,
) -> None:
    # A ref merge-base cannot resolve (a typo'd --merge-ref) is a precondition
    # failure -- and "nothing changed" must include the marker: one left behind
    # would show the "update interrupted" banner and block other applies until
    # a needless recover.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(
        ("git", "merge-base", "--is-ancestor"),
        _Result(returncode=128, stderr="fatal: not a valid object name"),
    )

    with pytest.raises(update_runtime.ApplyPreconditionError):
        _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert not runner.ran("git", "merge", "--ff-only")
    assert not _marker_exists(apply_repo)


# --- apply: worker bundle -------------------------------------------------------


def test_apply_installs_the_workers_bundle_instead_of_building(
    apply_repo: Path, tmp_path: Path
) -> None:
    worker_bundle = tmp_path / "worker-static"
    (worker_bundle / "assets").mkdir(parents=True)
    (worker_bundle / "index.html").write_text(
        f'<!doctype html><script type="module" src="/assets/{_ASSET_NAME}"></script>'
    )
    (worker_bundle / "assets" / _ASSET_NAME).write_text("console.log('worker');")
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(worker_bundle),
    )

    assert code == 0
    # The worker's validated artifact is installed as-is; no live build runs.
    assert not runner.ran("npm", "run", "build")
    installed = (
        apply_repo / update_layout.STATIC_DIR / "assets" / _ASSET_NAME
    ).read_text()
    assert installed == "console.log('worker');"


def test_apply_falls_back_to_a_live_build_when_the_bundle_path_is_empty(
    apply_repo: Path, tmp_path: Path
) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(tmp_path / "not-built"),
    )

    assert code == 0
    assert runner.ran("npm", "run", "build")


_FRONTEND_TREE_HASH = "f1e2d3c4b5a6978877665544332211ffeeddccbb"


def _make_worker_bundle(tmp_path: Path, stamp: str | None) -> Path:
    worker_bundle = tmp_path / "worker-static"
    (worker_bundle / "assets").mkdir(parents=True)
    (worker_bundle / "index.html").write_text(
        f'<!doctype html><script type="module" src="/assets/{_ASSET_NAME}"></script>'
    )
    (worker_bundle / "assets" / _ASSET_NAME).write_text("console.log('worker');")
    if stamp is not None:
        (worker_bundle / update_layout.BUNDLE_STAMP_FILENAME).write_text(stamp + "\n")
    return worker_bundle


def _verifiable_runner(name_status: str, repo_root: Path) -> _RecordingRunner:
    """An apply runner whose git can resolve the merged tree's frontend hash,
    so bundle stamps are actually compared (and whose emulated build stamps
    its output like the real postbuild step)."""
    runner = _apply_runner(name_status, repo_root)
    runner.respond(
        ("git", "rev-parse", f"HEAD:{update_layout.FRONTEND_DIR}"),
        _Result(stdout=_FRONTEND_TREE_HASH + "\n"),
    )
    runner.build_stamp = _FRONTEND_TREE_HASH
    return runner


def _installed_asset(repo_root: Path) -> str:
    return (repo_root / update_layout.STATIC_DIR / "assets" / _ASSET_NAME).read_text()


def test_a_verified_worker_bundle_is_installed_without_the_npm_refresh(
    apply_repo: Path, tmp_path: Path
) -> None:
    # Installing the worker's bundle is a plain copy that needs no
    # node_modules, so with a manifest change in the plan the `npm ci` --
    # the slowest, most memory-hungry step, and one whose shed rolls the
    # whole update back -- is dead work on the critical path and must not run.
    worker_bundle = _make_worker_bundle(tmp_path, stamp=_FRONTEND_TREE_HASH)
    runner = _verifiable_runner(_FRONTEND_MANIFEST_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(worker_bundle),
    )

    assert code == 0
    assert not runner.ran("npm", "ci")
    assert not runner.ran("npm", "run", "build")
    assert _installed_asset(apply_repo) == "console.log('worker');"


def test_a_stale_worker_bundle_falls_back_to_a_refreshed_live_build(
    apply_repo: Path, tmp_path: Path, capsys
) -> None:
    # A --worker-bundle that was built from some other frontend source than
    # the merged tree's (a wrong path, an old worker's leftovers) is exactly
    # the "source updated, UI didn't" state a user once caught by eye. It must
    # never be served: the live build runs instead -- with its npm refresh,
    # since the copy-only shortcut no longer applies.
    worker_bundle = _make_worker_bundle(tmp_path, stamp="0" * 40)
    runner = _verifiable_runner(_FRONTEND_MANIFEST_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(worker_bundle),
    )

    assert code == 0
    assert runner.ran("npm", "ci")
    assert runner.ran("npm", "run", "build")
    assert _installed_asset(apply_repo) == "console.log('app');"
    err = capsys.readouterr().err
    assert "it is stale" in err
    assert "building live instead" in err


def test_an_unstamped_worker_bundle_is_not_trusted_over_a_verifiable_tree(
    apply_repo: Path, tmp_path: Path, capsys
) -> None:
    worker_bundle = _make_worker_bundle(tmp_path, stamp=None)
    runner = _verifiable_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(worker_bundle),
    )

    assert code == 0
    assert runner.ran("npm", "run", "build")
    assert "cannot be verified" in capsys.readouterr().err


def test_a_live_build_whose_bundle_does_not_match_the_merged_source_rolls_back(
    apply_repo: Path,
) -> None:
    # The exit-code check cannot tell a build that wrote the merged source's
    # bundle from one that left an older bundle in place; the stamp can.
    runner = _verifiable_runner(_FRONTEND_DIFF, apply_repo)
    runner.build_stamp = "0" * 40

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    # The pre-apply copy is back, not the mismatched build (which the
    # emulated build left in place, so the index alone cannot tell them apart).
    assert _bundle_exists(apply_repo)
    assert _installed_stamp(apply_repo) is None


def test_a_bundle_the_tree_cannot_vouch_for_is_accepted_on_the_index_alone(
    apply_repo: Path, tmp_path: Path, capsys
) -> None:
    # When git cannot resolve the merged frontend tree there is nothing to
    # compare a stamp against, and an apply must not be blocked on a read
    # failure: the pre-stamp acceptance (index.html present) is what is left.
    worker_bundle = _make_worker_bundle(tmp_path, stamp=None)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(
        ("git", "rev-parse", f"HEAD:{update_layout.FRONTEND_DIR}"),
        _Result(returncode=128, stderr="fatal: not a tree"),
    )

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        worker_bundle=str(worker_bundle),
    )

    assert code == 0
    assert not runner.ran("npm", "run", "build")
    assert _installed_asset(apply_repo) == "console.log('worker');"
    assert "cannot be verified" in capsys.readouterr().err


# --- apply: failure -> rollback --------------------------------------------------


def test_a_step_that_cannot_be_spawned_rolls_back_and_names_the_step(
    apply_repo: Path, capsys
) -> None:
    # A forward step whose executable is not on the PATH this apply inherited
    # raises FileNotFoundError out of subprocess, not a non-zero exit. Before
    # `run_checked` caught OSError it escaped the whole forward block (which
    # catches only ApplyFailed), so the merge stayed landed with no rollback.
    # It must instead read as an ordinary failed step, named.
    runner = _apply_runner(_FRONTEND_MANIFEST_DIFF + _FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "ci"), FileNotFoundError(2, "No such file", "npm"))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    assert not _marker_exists(apply_repo)
    assert "npm ci could not be run" in capsys.readouterr().err


def test_an_unexpected_error_in_the_forward_apply_still_rolls_back(
    apply_repo: Path, capsys
) -> None:
    # The last resort. A real one of these shipped: the staged apply read
    # `oom_tag_shell_prefix` off a pre-merge bands module too old to have it,
    # and the AttributeError escaped past the merge and the snapshots with only
    # ApplyFailed caught -- leaving the workspace half-applied with a marker and
    # no rollback. Whatever the bug, the merge must still come back out.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), AttributeError("no attribute 'boom'"))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    assert not _marker_exists(apply_repo)
    err = capsys.readouterr().err
    assert "unexpected AttributeError" in err
    # The traceback survives the tidying, so the bug is still diagnosable --
    # under its own heading, since a traceback filed as the pre-flight boot's
    # output sends the reader to a step that never ran.
    assert "Traceback (most recent call last)" in err
    assert "--- traceback ---" in err
    assert "pre-flight boot output" not in err


def test_failed_build_rolls_back_the_merge_and_restores_the_bundle(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    # The entire merge is reverted as a forward revert commit...
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    assert runner.ran("git", "commit", "--no-verify")
    # ...and the emptied bundle is back, restored from the pre-apply copy
    # (the emulated build destroyed it before failing).
    assert _bundle_exists(apply_repo)
    # No restart happened forward or during recovery: the live service never
    # stopped serving known-good code.
    assert not runner.ran(*_RESTART)
    assert not _marker_exists(apply_repo)


def test_failed_preflight_never_restarts_the_live_service(apply_repo: Path) -> None:
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner(output="Traceback: ImportError boom", exited=True)

    def only_live_healthy(url: str) -> int | None:
        return 200 if _is_live(url) else None

    code = _apply(runner, _FakeHttp(only_live_healthy), spawner, apply_repo)

    assert code == 2
    assert not runner.ran(*_RESTART)
    assert runner.ran("git", "checkout", _ROLLBACK, "--")


def test_a_failed_preflight_reports_why_the_backend_did_not_boot(
    apply_repo: Path, capsys
) -> None:
    # The whole point of the pre-flight is that the merged code never reaches
    # the live service -- so its output is the only evidence of *why* it was
    # rejected. Without it an exit 2 is indistinguishable from a slow boot, and
    # whoever picks it up diagnoses a cause they cannot see.
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner(
        output=(
            "starting up\nDEBUG loading agents\nTraceback (most recent call last):\n"
            "ModuleNotFoundError: No module named 'frontmatter'\n"
        ),
        exited=True,
    )

    code = _apply(
        runner,
        _FakeHttp(lambda url: 200 if _is_live(url) else None),
        spawner,
        apply_repo,
    )

    assert code == 2
    # stderr gets all of it -- whoever ran the apply is looking right now.
    reported = capsys.readouterr().err
    assert "ModuleNotFoundError: No module named 'frontmatter'" in reported
    assert "DEBUG loading agents" in reported
    # The rollback commit carries only the line that names the cause, so the
    # reason survives in git history after that terminal is gone without
    # writing a backend's startup log into the repository.
    commit = runner.argvs_starting("git", "commit")[0]
    message = next(arg for arg in commit if "auto-reverted" in arg)
    assert "ModuleNotFoundError: No module named 'frontmatter'" in message
    assert "DEBUG loading agents" not in message
    assert "starting up" not in message


def test_a_preflight_that_cannot_be_launched_reads_as_a_failed_preflight(
    apply_repo: Path, capsys
) -> None:
    # Not booting and failing to boot are the same verdict, and a console
    # script missing from the PATH the apply inherited is exactly what a tool
    # reinstall that half-succeeded leaves behind. Unguarded it is an OSError
    # out of subprocess, past the merge and the snapshots.
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner(spawn_error=FileNotFoundError(2, "No such file", "mngr"))

    code = _apply(
        runner,
        _FakeHttp(lambda url: 200 if _is_live(url) else None),
        spawner,
        apply_repo,
    )

    assert code == 2
    # Never restarted, like any other rejected pre-flight, and rolled back.
    assert not runner.ran(*_RESTART)
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    assert "could not be launched" in capsys.readouterr().err


def test_a_failed_preflight_that_produced_no_output_says_so(
    apply_repo: Path, capsys
) -> None:
    # A silent failure is itself a finding (the tool never got far enough to
    # log), and must not read as "the output was dropped again".
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)

    _apply(
        runner,
        _FakeHttp(lambda url: 200 if _is_live(url) else None),
        _FakeSpawner(exited=True),
        apply_repo,
    )

    assert "wrote nothing at all" in capsys.readouterr().err


def test_preflight_output_is_tailed_to_the_interesting_end() -> None:
    # A backend that logs its way to a crash would otherwise bury the traceback
    # under startup chatter, so keep the end and say what was dropped.
    limit = update_probes._PREFLIGHT_OUTPUT_TAIL_LINES

    tailed = update_runtime.tail(
        "\n".join([f"chatter {index}" for index in range(limit + 10)] + ["the error"]),
        limit,
    )

    assert tailed.splitlines()[-1] == "the error"
    assert len(tailed.splitlines()) == limit + 1  # the omission notice
    assert "11 earlier line(s) omitted" in tailed
    assert "chatter 0" not in tailed


def test_preflight_stops_polling_once_the_backend_has_died(apply_repo: Path) -> None:
    # A backend that died on import will not become healthy, so the apply must
    # not sit out the rest of the deadline before rolling back.
    probes: list[str] = []

    def record(url: str) -> int | None:
        probes.append(url)
        return 200 if _is_live(url) else None

    runner = _apply_runner(_BACKEND_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(record), _FakeSpawner(exited=True), apply_repo)

    assert code == 2
    # One pre-flight probe, not _PREFLIGHT_ATTEMPTS of them.
    assert len([url for url in probes if not _is_live(url)]) == 1


def test_preflight_drops_the_callers_agent_identity(
    apply_repo: Path, monkeypatch
) -> None:
    # The apply runs inside an agent, so its environment carries MNGR_AGENT_ID
    # -- under which the throwaway pre-flight boot would persist layout state
    # as that agent, clobbering the live layout.json (the preview flow drops it
    # for exactly this reason).
    monkeypatch.setenv("MNGR_AGENT_ID", "the-lead-agent-id")
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner()

    code = _apply(runner, _FakeHttp(_all_healthy), spawner, apply_repo)

    assert code == 0
    assert spawner.envs, "the pre-flight boot never spawned"
    assert all("MNGR_AGENT_ID" not in env for env in spawner.envs)


def test_failed_post_restart_health_rolls_back_and_restarts_into_known_good(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    health_by_phase = {"restarts_seen": 0}

    def responder(url: str) -> int | None:
        if not _is_live(url):
            return 200  # the pre-flight throwaway boot is healthy
        # The live service is unhealthy after the forward restart, healthy
        # again once recovery has restarted it into known-good code.
        return 200 if health_by_phase["restarts_seen"] >= 2 else 500

    def count_restarts(argv: list[str]) -> None:
        if tuple(argv[:4]) == _RESTART:
            health_by_phase["restarts_seen"] += 1

    runner.on_command = count_restarts

    code = _apply(runner, _FakeHttp(responder), _FakeSpawner(), apply_repo)

    assert code == 2
    assert len(runner.argvs_starting(*_RESTART)) == 2  # forward, then recovery


_PROVISIONER_DIFF = "M\tsystem/scripts/setup_system.sh\n"


def _read_provision_incomplete(repo_root: Path) -> dict:
    return json.loads(
        update_apply_contract.provision_incomplete_path(repo_root).read_text()
    )


def test_a_failed_provisioner_alone_lands_the_update_and_records_the_gap(
    apply_repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed tool install leaves the tree and services consistent, and
    # re-running the provisioner later is cheap and merge-independent -- so it
    # must not cost the whole release plus a fresh worker pass. The update
    # lands, and the gap is loud: on stderr, and as a durable record for the
    # skill to act on.
    monkeypatch.setenv("MNGR_AGENT_NAME", "the-lead")
    runner = _apply_runner(_PROVISIONER_DIFF, apply_repo)
    runner.respond(("bash",), _Result(returncode=1, stderr="curl: (6) no network"))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert not runner.ran("git", "checkout", _ROLLBACK, "--")
    assert len(runner.argvs_starting(*_PROVISION)) == 1
    record = _read_provision_incomplete(apply_repo)
    assert "exit 1" in record["reason"] and "no network" in record["reason"]
    assert record["dri_agent"] == "the-lead"
    assert record["merge_ref"] == _MERGE_REF
    err = capsys.readouterr().err
    assert "applied with incomplete provisioning" in err
    assert f"bash {update_layout.PROVISIONER_SCRIPT}" in err
    assert not _marker_exists(apply_repo)


def test_a_hung_provisioner_is_a_named_failure_not_an_open_ended_wait(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_PROVISIONER_DIFF, apply_repo)
    runner.respond(
        ("bash",), subprocess.TimeoutExpired(cmd="bash setup_system.sh", timeout=1800)
    )

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert "did not finish within" in _read_provision_incomplete(apply_repo)["reason"]


def test_a_provisioner_that_cannot_be_spawned_is_a_recorded_failure_not_a_crash(
    apply_repo: Path,
) -> None:
    # An OSError out of the spawn (no bash, an exec failure) used to escape the
    # forward step block, which catches only ApplyFailed: no rollback, and the
    # marker left over a half-applied tree.
    runner = _apply_runner(_PROVISIONER_DIFF, apply_repo)
    runner.respond(("bash",), FileNotFoundError("bash: not found"))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert "bash: not found" in _read_provision_incomplete(apply_repo)["reason"]
    assert not _marker_exists(apply_repo)


def test_a_timed_out_provisioner_takes_its_own_children_with_it(
    tmp_path: Path,
) -> None:
    # subprocess.run's timeout kills only bash; the installer or curl it
    # spawned would run on after the apply rolled back around it. The real
    # Runner has to take the whole process group down.
    pid_file = tmp_path / "child.pid"
    script = f"sleep 60 & echo $! > {pid_file}; wait"

    with pytest.raises(subprocess.TimeoutExpired):
        update_runtime.Runner().run_process_group(
            ["bash", "-c", script], cwd=str(tmp_path), env=dict(os.environ), timeout=0.5
        )

    child_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the provisioner's child {child_pid} outlived the timeout")


def test_a_clean_provisioner_run_clears_an_earlier_incomplete_record(
    apply_repo: Path,
) -> None:
    update_apply_contract.write_provision_incomplete(
        apply_repo, "an earlier failure", "someone", _MERGE_REF, lambda: 1.0
    )
    runner = _apply_runner(_PROVISIONER_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert not update_apply_contract.provision_incomplete_path(apply_repo).exists()


def test_a_failed_provisioner_followed_by_a_failed_probe_still_rolls_back(
    apply_repo: Path, capsys
) -> None:
    # A load-bearing provisioner change (a node bump, a new apt dependency)
    # shows up as a failed pre-flight or probe, and that still rolls the whole
    # merge back -- with the provisioner re-run best-effort from the restored
    # tree, since its forward run may have moved global tool state. No
    # provisioning-incomplete record: the update did not land.
    runner = _apply_runner(_PROVISIONER_DIFF + _BACKEND_DIFF, apply_repo)
    runner.respond(("bash",), _Result(returncode=1, stderr="no network"))
    spawner = _FakeSpawner(output="ImportError: node too old", exited=True)

    def only_live_healthy(url: str) -> int | None:
        return 200 if _is_live(url) else None

    code = _apply(runner, _FakeHttp(only_live_healthy), spawner, apply_repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    assert len(runner.argvs_starting(*_PROVISION)) == 2  # forward, then recovery
    assert not update_apply_contract.provision_incomplete_path(apply_repo).exists()
    err = capsys.readouterr().err
    assert "still counts as recovered" in err
    assert "confirmed healthy" in err


def test_the_provisioner_runs_under_the_image_builds_environment(
    apply_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An agent's HOME is /home/user at runtime; installers that follow $HOME
    # would land beside neither the checks nor the PATH the provisioner fixes
    # to /root/.local. Forward run and recovery re-run alike get the canonical
    # env, with everything else ambient preserved.
    monkeypatch.setenv("HOME", "/home/user")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    # An image built when the Dockerfile exported its pins as ENV carries the
    # image's version in every process; setup_system.sh's `:=` defaults yield
    # to it, so the merged tree's bump would reinstall the old version and pass
    # the script's own pin check. The pins are the tree's, never the caller's.
    monkeypatch.setenv("CLAUDE_CODE_VERSION", "2.1.207")
    monkeypatch.setenv("NODE_VERSION", "22.0.0")
    runner = _apply_runner(_PROVISIONER_DIFF + _BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner(output="boom", exited=True)

    def only_live_healthy(url: str) -> int | None:
        return 200 if _is_live(url) else None

    code = _apply(runner, _FakeHttp(only_live_healthy), spawner, apply_repo)

    assert code == 2
    provisioner_envs = [
        env
        for argv, env in zip(runner.calls, runner.envs)
        if tuple(argv[: len(_PROVISION)]) == _PROVISION
    ]
    assert len(provisioner_envs) == 2
    for env in provisioner_envs:
        assert env is not None
        assert env["HOME"] == "/root"
        assert env["PATH"].startswith("/root/.local/bin:")
        assert env["HTTPS_PROXY"] == "http://proxy.example:3128"
        assert "CLAUDE_CODE_VERSION" not in env
        assert "NODE_VERSION" not in env
    # Only the recovery re-run is forced past the provision guard: the rolled-
    # back tree is the one the guard's marker was written for, so an unforced
    # re-run would skip and leave the global tools at the merged versions.
    assert [env.get("PROVISION_FORCE") for env in provisioner_envs] == [None, "1"]


def test_provisioner_inputs_are_read_off_the_entry_point(tmp_path: Path) -> None:
    # The live re-run is keyed on the files the provisioner reads, and which
    # installers it chains is read off the script itself rather than kept in
    # a list beside it: a release that adds or retires one is read as it ships.
    scripts = tmp_path / "system" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "setup_system.sh").write_text(
        "#!/usr/bin/env bash\n"
        '. "$(dirname "$0")/_provision_guard.sh"\n'
        'bash "$sources_dir/write_apt_sources.sh"\n'
        "# Keep the pins in sync with seed_home_skeleton.sh.\n"
        'bash /tmp/install_claude.sh "${CLAUDE_CODE_VERSION}"\n'
        'if [ -f "$setup_dir/install_dufs.sh" ]; then\n'
        '    bash "$setup_dir/install_dufs.sh"\n'
        "else\n"
        '    bash "$setup_dir/default-workspace-template-install-dufs"\n'
        "fi\n"
    )
    for name in (
        "_provision_guard.sh",
        "write_apt_sources.sh",
        "seed_home_skeleton.sh",
        "install_dufs.sh",
    ):
        (scripts / name).write_text("")

    assert update_classification.read_provisioner_inputs(tmp_path) == {
        "system/scripts/setup_system.sh",
        ".mngr/apt-snapshot-timestamp",
        # Sourced and run, respectively.
        "system/scripts/_provision_guard.sh",
        "system/scripts/write_apt_sources.sh",
        # Run, inside a branch.
        "system/scripts/install_dufs.sh",
        # Not: mentioned only in a comment (seed_home_skeleton.sh), a /tmp
        # download (install_claude.sh), an image-baked name without a sibling.
    }


def test_provisioner_inputs_on_a_tree_without_the_entry_point() -> None:
    # The apply may run against a tree that predates the script (or a test
    # repo without one): the fixed two still count, nothing else does.
    assert update_classification.read_provisioner_inputs(Path("/nonexistent")) == {
        "system/scripts/setup_system.sh",
        ".mngr/apt-snapshot-timestamp",
    }


def test_the_real_provisioner_chains_more_than_its_entry_point() -> None:
    # A parse that silently matched nothing in the real script would key every
    # installer pin bump off the list of two, exactly the gap this exists to
    # close.
    inputs = update_classification.read_provisioner_inputs(_WORKSPACE_ROOT)
    chained = inputs - {
        "system/scripts/setup_system.sh",
        ".mngr/apt-snapshot-timestamp",
    }
    assert chained != set()
    for path in chained:
        assert (_WORKSPACE_ROOT / path).is_file(), path


def test_a_hung_forward_step_rolls_back_naming_the_step(
    apply_repo: Path, capsys
) -> None:
    # The old reveal ran for 1h28m before anyone asked whether it was stuck.
    # A forward step that outlives its budget is a failure with a name, and
    # the rollback carries the per-phase timings that show where it hung.
    runner = _apply_runner(_FRONTEND_MANIFEST_DIFF, apply_repo)
    runner.respond(("npm", "ci"), subprocess.TimeoutExpired(cmd="npm ci", timeout=1200))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    err = capsys.readouterr().err
    assert "npm ci did not finish within 1200s" in err
    assert "apply phase timings:" in err
    assert update_apply_contract.PHASE_SNAPSHOTTED in err


def test_every_apply_reports_its_per_phase_timings(apply_repo: Path, capsys) -> None:
    diff = _FRONTEND_DIFF + _BACKEND_DIFF + _PROVISIONER_DIFF
    runner = _apply_runner(diff, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    err = capsys.readouterr().err
    timing_line = next(
        line for line in err.splitlines() if line.startswith("apply phase timings:")
    )
    for phase in (
        update_apply_contract.PHASE_MERGED,
        update_apply_contract.PHASE_SNAPSHOTTED,
        update_apply_contract.PHASE_REFRESHED,
        update_apply_contract.PHASE_PROVISIONED,
        update_apply_contract.PHASE_BUILT,
        update_apply_contract.PHASE_RESTARTED,
    ):
        assert f"{phase} +" in timing_line


def test_marker_phase_timings_roundtrip_and_tolerate_older_markers(
    tmp_path: Path,
) -> None:
    marker = _plant_marker(tmp_path)
    marker.phase_timings = {
        update_apply_contract.PHASE_MERGED: 1001.0,
        update_apply_contract.PHASE_BUILT: 1007.5,
    }
    update_apply_contract.write_marker(marker, tmp_path, now=lambda: 1010.0)
    read = update_apply_contract.read_marker(tmp_path)
    assert read is not None
    assert read.phase_timings == {
        update_apply_contract.PHASE_MERGED: 1001.0,
        update_apply_contract.PHASE_BUILT: 1007.5,
    }

    # A marker written by an older apply carries no timings at all.
    older = json.loads(update_apply_contract.marker_path(tmp_path).read_text())
    del older["phase_timings"]
    update_apply_contract.marker_path(tmp_path).write_text(json.dumps(older))
    read = update_apply_contract.read_marker(tmp_path)
    assert read is not None
    assert read.phase_timings == {}


def test_a_marker_that_is_not_an_object_is_ignored_not_fatal(
    apply_repo: Path, capsys
) -> None:
    # Well-formed JSON of the wrong shape (a list, a string) must degrade like
    # a torn write does: read_marker promises a corrupt marker never wedges the
    # recovery decision, and the cron re-reads it every five minutes forever.
    update_apply_contract.marker_path(apply_repo).parent.mkdir(parents=True)
    update_apply_contract.marker_path(apply_repo).write_text(
        json.dumps([{"phase": "merged"}])
    )
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    assert update_apply_contract.read_marker(apply_repo) is None
    assert "not a valid marker" in capsys.readouterr().err
    assert _recover(runner, _FakeHttp(_all_healthy), apply_repo, if_stale=True) == 0
    assert runner.calls == []


def test_emergency_when_rollback_cannot_restore_health(
    apply_repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", "the-lead")
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))

    # The merged backend pre-flights fine; it is the live service that never
    # comes back after the rollback's restart.
    def live_never_healthy(url: str) -> int | None:
        return 500 if _is_live(url) else 200

    code = _apply(runner, _FakeHttp(live_never_healthy), _FakeSpawner(), apply_repo)

    assert code == 3
    # The pre-apply copies are the operator's way back: kept, and named.
    bundle_copy = _snapshot_copy(apply_repo, "bundle")
    assert bundle_copy.exists()
    assert str(bundle_copy) in capsys.readouterr().err
    assert not _marker_exists(apply_repo)
    # The marker is gone, so the record is the only thing left that can say who
    # was driving this, what failed, and where the copies are.
    record = _read_emergency(apply_repo)
    assert record["dri_agent"] == "the-lead"
    assert "boom" in record["reason"]
    assert record["snapshots_dir"] == str(bundle_copy.parent)


def _plant_emergency(repo_root: Path) -> None:
    update_apply_contract.write_emergency(
        repo_root, "an earlier apply's rollback failed", "the-lead", lambda: 1.0
    )


@pytest.mark.parametrize(
    ("page_responder", "is_record_kept"),
    [(_built_app_page, False), (_placeholder_page, True)],
    ids=["healthy-ui", "broken-ui"],
)
@pytest.mark.parametrize(
    ("diff", "is_build_failing", "expected_code"),
    [(_BACKEND_DIFF, False, 0), (_FRONTEND_DIFF, True, 2)],
    ids=["applied", "rolled-back"],
)
def test_the_emergency_record_comes_down_only_over_a_confirmed_ui(
    apply_repo: Path,
    diff: str,
    is_build_failing: bool,
    expected_code: int,
    page_responder: Callable[[str], update_runtime.FetchedPage],
    is_record_kept: bool,
) -> None:
    # The banner keys off the record's mere presence, so an outcome that ends
    # with the workspace confirmed healthy has to take it down -- a rollback
    # that puts known-good code back just as much as an apply that lands, which
    # is why the outcome axis here is crossed rather than nested: the rule turns
    # on the UI alone. A clear that quietly stopped happening would leave a
    # workspace saying it may be broken forever, with nothing to contradict it.
    # The broken-UI half is the case that actually happens, since a UI that is
    # already down is the usual aftermath of the failure that wrote the record:
    # neither outcome probes its way back to a working one -- the backend
    # answers, the closing line names the breakage, and the user still cannot
    # see the workspace -- so clearing on that would take the banner away from
    # the one workspace that still needs it.
    _plant_emergency(apply_repo)
    runner = _apply_runner(diff, apply_repo)
    if is_build_failing:
        runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))
    http = _FakeHttp(_all_healthy, page_responder=page_responder)

    code = _apply(runner, http, _FakeSpawner(), apply_repo)

    assert code == expected_code
    assert update_apply_contract.emergency_path(apply_repo).exists() is is_record_kept


def test_an_apply_that_repairs_an_already_broken_ui_clears_the_emergency_record(
    apply_repo: Path, capsys
) -> None:
    # The record's usual aftermath is a UI that is down, which is exactly the
    # baseline that used to keep it from ever being cleared: an apply held to
    # no frontend standard would land, find the UI working, and still leave
    # the banner up until some later apply that happened to start healthy.
    _plant_emergency(apply_repo)
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)

    def page_responder(url: str) -> update_runtime.FetchedPage:
        # Down before the apply, serving once the merged backend is up.
        return _built_app_page(url) if runner.ran(*_RESTART) else _placeholder_page(url)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy, page_responder=page_responder),
        _FakeSpawner(),
        apply_repo,
    )

    assert code == 0
    assert not update_apply_contract.emergency_path(apply_repo).exists()
    assert "confirmed healthy" in capsys.readouterr().err.strip().splitlines()[-1]


def test_a_rollback_that_repairs_an_already_broken_ui_clears_the_emergency_record(
    apply_repo: Path, capsys
) -> None:
    _plant_emergency(apply_repo)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))
    builds = {"seen": 0}

    def count_builds(argv: list[str]) -> None:
        if argv[:3] == ["npm", "run", "build"]:
            builds["seen"] += 1

    runner.on_command = count_builds

    def page_responder(url: str) -> update_runtime.FetchedPage:
        # Down at the baseline; the restored pre-apply bundle serves.
        return _built_app_page(url) if builds["seen"] else _placeholder_page(url)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy, page_responder=page_responder),
        _FakeSpawner(),
        apply_repo,
    )

    assert code == 2
    assert not update_apply_contract.emergency_path(apply_repo).exists()
    assert "confirmed healthy" in capsys.readouterr().err.strip().splitlines()[-1]


def test_a_regressed_frontend_is_rolled_back(apply_repo: Path) -> None:
    # The app-shell probe answers, in order: built (the pre-apply baseline),
    # the placeholder (the apply regressed it), built again (the rollback
    # restored it) -- so the apply must roll back and confirm recovery.
    shell_answers = [_built_app_page, _placeholder_page, _built_app_page]

    def page_responder(url: str) -> update_runtime.FetchedPage:
        if url.endswith(".js"):
            return _built_app_page(url)
        answer = shell_answers.pop(0) if len(shell_answers) > 1 else shell_answers[0]
        return answer(url)

    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.build_stamp = "built-by-this-apply"

    code = _apply(
        runner,
        _FakeHttp(_all_healthy, page_responder=page_responder),
        _FakeSpawner(),
        apply_repo,
    )

    assert code == 2
    assert runner.ran("git", "checkout", _ROLLBACK, "--")
    # Restored from the pre-apply copy, not the regressing build the emulated
    # build left in place.
    assert _bundle_exists(apply_repo)
    assert _installed_stamp(apply_repo) is None


def test_a_build_that_writes_no_bundle_is_a_failure_not_a_success(
    apply_repo: Path,
) -> None:
    # A build tool killed after emptying its output directory can still exit 0.
    # Trusting the exit code alone would report success on an empty bundle and
    # hand the user a blank page.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.is_build_output_written = False

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 2
    assert _bundle_exists(apply_repo)  # the pre-apply copy is back


def test_a_rollback_whose_own_git_fails_is_an_emergency_that_keeps_the_copies(
    apply_repo: Path, capsys
) -> None:
    # The rollback's git steps run with check=True. An escape there would
    # surface as a traceback over a part-restored tree whose bundle the failed
    # build already destroyed, and would take the emergency record and the
    # copies with it. Not recovering is what exit 3 is for, and the copies are
    # what make it survivable.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    runner.respond(
        ("git", "checkout"),
        subprocess.CalledProcessError(1, ["git", "checkout"], stderr="index.lock"),
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 3

    assert _read_emergency(apply_repo)["reason"]
    kept = _snapshot_copy(apply_repo, "bundle")
    assert (kept / "index.html").exists()
    assert str(kept) in capsys.readouterr().err


def test_a_rollback_whose_git_cannot_be_spawned_is_an_emergency_too(
    apply_repo: Path,
) -> None:
    # The same escape hatch for the other way a rollback step fails: an
    # OSError (git itself missing, an exec failure) is not a CalledProcessError,
    # and must reach the same emergency exit rather than a traceback.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))
    runner.respond(("git", "checkout"), FileNotFoundError("git: not found"))

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 3

    assert "boom" in _read_emergency(apply_repo)["reason"]
    assert not _marker_exists(apply_repo)


def test_a_backend_only_emergency_is_not_pointed_at_the_bundle_copy(
    apply_repo: Path, capsys
) -> None:
    # Same exit 3, but nothing here ever wrote the bundle directory: the build
    # is the only step that empties it, and the rollback restores tracked files
    # while it is untracked output. So the copy is byte-identical to what is
    # already being served, and offering it would send someone whose UI is down
    # for a backend reason off to copy a bundle over itself.
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_after(runner, *_RESTART))

    assert _apply(runner, http, _FakeSpawner(), apply_repo) == 3

    assert "bundle was kept" not in capsys.readouterr().err


# --- apply: the already-broken-frontend baseline ------------------------------------
#
# The apply is answerable for *regressions*: a workspace that was not serving a
# working frontend before it started does not get its update rolled back for
# still not serving one afterwards -- that would lose the change without fixing
# anything. The baseline is measured once, before any destructive step, and the
# closing report is held to it either way.


def test_a_frontend_already_broken_beforehand_is_reported_not_rolled_back(
    unbuilt_apply_repo: Path, capsys
) -> None:
    runner = _apply_runner(_BACKEND_DIFF, unbuilt_apply_repo)
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    code = _apply(runner, http, _FakeSpawner(), unbuilt_apply_repo)

    assert code == 0
    assert not runner.ran("git", "checkout", _ROLLBACK, "--")
    # The lead relays the closing line to the user, so it must not sign off on
    # a UI we have just established they cannot see -- which is exactly what
    # the backend's own health check cannot tell us.
    closing_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "confirmed healthy" not in closing_line
    assert "not serving a working frontend" in closing_line


def test_a_rollback_does_not_claim_health_over_an_already_broken_frontend(
    apply_repo: Path, capsys
) -> None:
    # The same rule on the rollback path, which never probes the frontend a
    # second time: the health it confirms is the backend's, so the closing line
    # has to say what it could not confirm rather than claim the UI is fine.
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="type error"))
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    code = _apply(runner, http, _FakeSpawner(), apply_repo)

    assert code == 2
    closing_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "confirmed healthy" not in closing_line
    assert "cannot confirm it" in closing_line


def test_a_blip_on_the_baseline_probe_does_not_disarm_the_regression_check(
    apply_repo: Path,
) -> None:
    # The baseline probe decides whether the apply is answerable for the
    # frontend at all, and it is wrong in only one direction: a single
    # unanswered request would conclude "already broken" and silently downgrade
    # every later rollback into a warning. So a non-answer is retried, and this
    # apply -- which really does break the frontend -- is still rolled back.
    unanswered: list[str] = []

    def before_the_build(url: str) -> update_runtime.FetchedPage | None:
        if not unanswered:
            unanswered.append(url)
            return None
        return _built_app_page(url)

    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    def page_responder(url: str) -> update_runtime.FetchedPage | None:
        if runner.ran("npm", "run", "build"):
            return _placeholder_page(url)
        return before_the_build(url)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy, page_responder=page_responder),
        _FakeSpawner(),
        apply_repo,
    )

    # Rolled back (and escalated, because the fake keeps serving the
    # placeholder afterwards) rather than exiting 0 with a warning.
    assert code == 3
    assert unanswered  # the blip really did happen


# --- the frontend probe ------------------------------------------------------------
#
# The probe asks the two questions a browser would -- is this the real app
# shell, and does its module script load as JavaScript -- because the backend's
# own health endpoint answers 200 for both the not-built placeholder and an
# unserved /assets path.


def _html(body: str, **headers: str) -> update_runtime.FetchedPage:
    return update_runtime.FetchedPage(
        status=200, body=body, headers={"content-type": "text/html", **headers}
    )


_SHELL_WITH_SCRIPT = _html(
    f'<!doctype html><script type="module" src="/assets/{_ASSET_NAME}"></script>'
)


@pytest.mark.parametrize(
    ("shell", "asset", "expected", "is_answered"),
    [
        (None, None, "did not answer a request for the app shell", False),
        (
            update_runtime.FetchedPage(status=503, body="", headers={}),
            None,
            "returned HTTP 503",
            True,
        ),
        (
            _html("not built", **{update_probes.FRONTEND_BUILT_HEADER: "false"}),
            None,
            "'frontend not built' placeholder",
            True,
        ),
        (
            _html("<!doctype html><p>no script here</p>"),
            None,
            "loads no bundled script",
            True,
        ),
        (
            _SHELL_WITH_SCRIPT,
            None,
            "did not answer a request for the bundled script",
            False,
        ),
        (
            _SHELL_WITH_SCRIPT,
            update_runtime.FetchedPage(status=404, body="", headers={}),
            "returned HTTP 404",
            True,
        ),
        # The blank-screen mode: the shell is the real app, but its module
        # script comes back as the SPA fallback HTML, which the browser refuses.
        (_SHELL_WITH_SCRIPT, _html("<!doctype html>"), "rather than JavaScript", True),
    ],
    ids=[
        "no-answer",
        "shell-error",
        "placeholder",
        "no-script",
        "asset-no-answer",
        "asset-missing",
        "asset-is-html",
    ],
)
def test_probe_frontend_names_each_way_the_ui_can_be_broken(
    shell: update_runtime.FetchedPage | None,
    asset: update_runtime.FetchedPage | None,
    expected: str,
    is_answered: bool,
) -> None:
    def page_responder(url: str) -> update_runtime.FetchedPage | None:
        return asset if url.endswith(".js") else shell

    probe = update_probes.probe_frontend(
        _FakeHttp(_all_healthy, page_responder=page_responder), _LIVE_BASE
    )

    assert probe.failure is not None and expected in probe.failure
    # Only an unanswered request is worth asking again; every other shape is
    # the service telling us the frontend really is broken.
    assert probe.is_answered is is_answered


def test_a_healthy_built_app_is_no_failure_at_all() -> None:
    probe = update_probes.probe_frontend(
        _FakeHttp(_all_healthy, page_responder=_built_app_page), _LIVE_BASE
    )

    assert probe.failure is None and probe.is_answered


def test_the_frontend_probe_retries_a_non_answer_but_not_a_verdict() -> None:
    # The two halves of one rule. A non-answer says nothing about the frontend,
    # so it is worth asking again; a verdict -- here the placeholder, arriving
    # as a perfectly healthy 200 -- is the service telling us the frontend is
    # broken, and asking again only spends the budget to reach the same answer.
    answers: list[update_runtime.FetchedPage | None] = [
        None,
        None,
        _built_app_page("/"),
    ]
    retried = _FakeHttp(
        _all_healthy,
        page_responder=lambda url: answers.pop(0) if answers else _built_app_page(url),
    )

    assert (
        update_probes.describe_frontend_failure(retried, _LIVE_BASE, _no_sleep) is None
    )
    assert not answers  # it kept asking until it got an answer

    verdict = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    assert (
        update_probes.describe_frontend_failure(verdict, _LIVE_BASE, _no_sleep)
        is not None
    )
    assert len(verdict.page_urls) == 1


def test_a_service_that_never_answers_spends_the_budget_and_still_names_a_failure() -> (
    None
):
    # Exhausting the retries has to leave a usable answer behind: the caller
    # turns it into the rollback message, and a UI that will not answer is one
    # the user cannot see either.
    silent = _FakeHttp(_all_healthy, page_responder=lambda _url: None)

    failure = update_probes.describe_frontend_failure(silent, _LIVE_BASE, _no_sleep)

    assert failure is not None
    assert len(silent.page_urls) == update_probes._FRONTEND_PROBE_ATTEMPTS


# --- the view refresh ---------------------------------------------------------------
#
# The refresh runs last, after the apply has already landed and the live
# workspace is confirmed healthy. It is the one step that must never fail the
# apply: a non-zero exit there reads to the lead as "the update did not land".


@pytest.mark.parametrize(
    "failure",
    [
        OSError("Cannot allocate memory"),
        # Capturing the helper's output decodes what the child wrote, and bytes
        # the stdio encoding cannot decode raise UnicodeDecodeError -- a
        # ValueError, which neither OSError nor SubprocessError covers.
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"),
    ],
    ids=["unspawnable", "undecodable-output"],
)
def test_a_refresh_that_cannot_run_does_not_fail_an_apply_that_landed(
    apply_repo: Path, failure: Exception, capsys
) -> None:
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    runner.respond((sys.executable,), failure)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert _refreshed_the_view(runner, apply_repo)  # attempted, not skipped
    assert "an open view may still be showing" in capsys.readouterr().err


# --- tree restoration ----------------------------------------------------------------


def test_restore_tree_removes_adds_and_checks_out_the_rest(tmp_path: Path) -> None:
    # `--no-renames` makes the diff pure adds/modifies/deletes, and the two
    # halves need opposite treatment: a file the update added has no
    # known-good version to check out, so it has to be removed instead.
    runner = _RecordingRunner()

    update_apply._restore_tree(
        [
            ("A", "system/apps/system_interface/imbue/system_interface/new_module.py"),
            ("M", "system/apps/system_interface/imbue/system_interface/server.py"),
            ("D", "system/apps/system_interface/frontend/src/old.ts"),
        ],
        _ROLLBACK,
        tmp_path,
        runner,
    )

    assert runner.argvs_starting("git", "rm") == [
        [
            "git",
            "rm",
            "--force",
            "--ignore-unmatch",
            "system/apps/system_interface/imbue/system_interface/new_module.py",
        ]
    ]
    assert [c[-1] for c in runner.argvs_starting("git", "checkout")] == [
        "system/apps/system_interface/imbue/system_interface/server.py",
        "system/apps/system_interface/frontend/src/old.ts",
    ]


# --- apply: marker lifecycle ------------------------------------------------------


def test_marker_is_written_before_the_merge_lands(apply_repo: Path) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    seen: dict[str, bool] = {}

    def capture(argv: list[str]) -> None:
        if argv[:2] == ["git", "merge"]:
            seen["marker_at_merge"] = _marker_exists(apply_repo)

    runner.on_command = capture

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert seen["marker_at_merge"] is True
    assert not _marker_exists(apply_repo)  # cleared on the way out


def test_marker_records_the_dri_agent_and_phases(
    apply_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A plan that reaches every phase, so the sequence below is the whole
    # ladder rather than the subset a narrower diff happens to walk.
    diff = (
        _FRONTEND_DIFF
        + _FRONTEND_MANIFEST_DIFF
        + _BACKEND_DIFF
        + _BACKEND_MANIFEST_DIFF
        + "M\tsystem/scripts/setup_system.sh\n"
    )
    runner = _apply_runner(diff, apply_repo)
    phases: list[str] = []
    dri: list[str] = []

    def observe_marker() -> None:
        marker = update_apply_contract.read_marker(apply_repo)
        if marker is not None:
            phases.append(marker.phase)
            dri.append(marker.dri_agent)

    def capture(argv: list[str]) -> None:
        observe_marker()

    # Sampled at every command AND every health probe: the restarted phase is
    # only observable at the post-restart probe, because the marker comes down
    # at the last rollback point -- before any further command runs.
    def probing_responder(url: str) -> int:
        observe_marker()
        return 200

    runner.on_command = capture
    # monkeypatch (not a bare os.environ write): in a real agent workspace
    # MNGR_AGENT_NAME is already set, and it must be restored, not deleted.
    monkeypatch.setenv("MNGR_AGENT_NAME", "test-lead-agent")
    code = _apply(runner, _FakeHttp(probing_responder), _FakeSpawner(), apply_repo)

    assert code == 0
    assert "test-lead-agent" in dri
    # Every phase this plan reaches is recorded, and never out of order: an
    # interrupted apply is read back from the last phase the marker names, so a
    # phase that is skipped or stamped early sends recovery to the wrong place.
    ladder = [
        update_apply_contract.PHASE_STARTED,
        update_apply_contract.PHASE_MERGED,
        update_apply_contract.PHASE_SNAPSHOTTED,
        update_apply_contract.PHASE_REFRESHED,
        update_apply_contract.PHASE_PROVISIONED,
        update_apply_contract.PHASE_BUILT,
        update_apply_contract.PHASE_RESTARTED,
    ]
    assert set(phases) == set(ladder)
    ranks = [ladder.index(phase) for phase in phases]
    assert ranks == sorted(ranks)


def test_marker_comes_down_before_the_view_refresh(apply_repo: Path) -> None:
    # The refresh reloads every open view, and the reloaded shell renders the
    # "update was interrupted" banner off the marker's mere presence -- so a
    # successful apply must clear the marker BEFORE asking the views to reload,
    # or every successful update greets the user with a false interruption
    # banner until they reload again by hand.
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    seen: dict[str, bool] = {}

    def capture(argv: list[str]) -> None:
        if argv[:1] == [sys.executable] and argv[1].endswith(
            "refresh_workspace_view.py"
        ):
            seen["marker_at_refresh"] = _marker_exists(apply_repo)

    runner.on_command = capture

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    assert seen["marker_at_refresh"] is False
    # And it comes after the restart, for the same reason it comes after the
    # probes: a reload issued earlier would put every open view back on the
    # build that is about to be replaced.
    restart_at = next(
        index for index, c in enumerate(runner.calls) if tuple(c[:4]) == _RESTART
    )
    refresh_at = next(
        index
        for index, c in enumerate(runner.calls)
        if c[:1] == [sys.executable] and c[1].endswith("refresh_workspace_view.py")
    )
    assert restart_at < refresh_at


def test_marker_is_gone_before_the_post_success_bookkeeping(apply_repo: Path) -> None:
    # env-converge is an apt full-upgrade that can run for minutes, and the
    # update is fully applied, probed healthy, and ledger-recorded before it
    # starts. A marker surviving into that window would make a kill there look
    # like an interrupted apply -- and the unattended recover (boot, cron)
    # would roll back an update that already went live.
    runner = _apply_runner(_BACKEND_DIFF, apply_repo)
    seen: dict[str, bool] = {}

    def capture(argv: list[str]) -> None:
        if argv[:3] == ["uv", "run", "env-converge"]:
            seen["marker_at_converge"] = _marker_exists(apply_repo)
        if argv[:2] == ["git", "add"]:
            seen["marker_at_ledger"] = _marker_exists(apply_repo)

    runner.on_command = capture

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    assert seen["marker_at_ledger"] is False
    assert seen["marker_at_converge"] is False


def test_a_live_marker_blocks_a_concurrent_apply(apply_repo: Path) -> None:
    _plant_marker(apply_repo, dri_agent="other-agent")
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        is_pid_live=lambda pid: True,
    )

    assert code == 1
    assert runner.calls == []  # refused before touching anything
    assert _marker_exists(apply_repo)  # the running apply's marker is untouched


def test_a_dead_marker_for_the_same_merge_is_resumed(apply_repo: Path) -> None:
    _plant_marker(
        apply_repo, dri_agent="earlier-run", phase=update_apply_contract.PHASE_MERGED
    )
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    # The interrupted run already landed the merge.
    runner.respond(("git", "merge-base", "--is-ancestor"), _Result(returncode=0))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    # Resumed, not re-merged: the recorded rollback point is used for the diff
    # and no second merge runs.
    assert not runner.ran("git", "merge", "--ff-only")
    assert runner.ran("git", "diff", "--no-renames", "--name-status", _ROLLBACK)
    assert not _marker_exists(apply_repo)


def test_resuming_an_apply_killed_mid_merge_aborts_the_half_merge_first(
    apply_repo: Path,
) -> None:
    _plant_marker(
        apply_repo, dri_agent="earlier-run", phase=update_apply_contract.PHASE_STARTED
    )
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    # The interrupted run died inside `git merge`: the merge is staged
    # (MERGE_HEAD present) but never became a commit.
    runner.respond(("git", "rev-parse", "--verify"), _Result(returncode=0))

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 0
    # The half-merge is undone before the clean-tree check can refuse over it,
    # and before anything commits -- a commit on top of a staged merge lands it.
    order = [tuple(c[:3]) for c in runner.calls]
    assert ("git", "merge", "--abort") in order
    assert order.index(("git", "merge", "--abort")) < order.index(
        ("git", "status", "--porcelain")
    )
    # Then the resume re-merges from the clean tree.
    assert runner.ran("git", "merge", "--ff-only")
    assert not _marker_exists(apply_repo)


def test_a_dead_marker_for_a_different_merge_refuses_and_points_at_recover(
    apply_repo: Path, capsys
) -> None:
    _plant_marker(apply_repo, dri_agent="earlier-run", merge_ref="mngr/update-other")
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo)

    assert code == 1
    assert "recover" in capsys.readouterr().err
    assert _marker_exists(apply_repo)  # left for recover to consume


# --- apply: memory bands ----------------------------------------------------------


def test_only_the_hungry_forward_steps_are_expendable_and_recovery_is_not(
    unbuilt_apply_repo: Path,
) -> None:
    # Frontend manifest + backend manifest + backend source, failing after the
    # restart, so both the forward steps and the recovery rebuild run. No
    # bundle existed to snapshot, so recovery takes the rebuild branch.
    diff = (
        _FRONTEND_MANIFEST_DIFF
        + _FRONTEND_DIFF
        + _BACKEND_MANIFEST_DIFF
        + _BACKEND_DIFF
    )
    runner = _apply_runner(diff, unbuilt_apply_repo)
    restarts = {"seen": 0}

    def responder(url: str) -> int | None:
        if not _is_live(url):
            return 200
        return 200 if restarts["seen"] >= 2 else 500

    def count(argv: list[str]) -> None:
        if tuple(argv[:4]) == _RESTART:
            restarts["seen"] += 1

    runner.on_command = count
    spawner = _FakeSpawner()

    code = _apply(runner, _FakeHttp(responder), spawner, unbuilt_apply_repo)

    assert code == 2
    wrapped = [c for c in runner.raw_calls if c[:2] == ["sh", "-c"]]
    unwrapped = [c for c in runner.raw_calls if c[:2] != ["sh", "-c"]]
    wrapped_cmds = [_unwrap_expendable(c) for c in wrapped]
    # Forward hungry steps are tagged: npm ci, the uv installs, uv sync, the
    # build -- and the pre-flight boot.
    assert ["npm", "ci"] in wrapped_cmds
    assert ["npm", "run", "build"] in wrapped_cmds
    assert any(c[:3] == ["uv", "tool", "install"] for c in wrapped_cmds)
    assert any(c[:2] == ["uv", "sync"] for c in wrapped_cmds)
    assert spawner.raw_spawns and spawner.raw_spawns[0][:2] == ["sh", "-c"]
    # git and the restarts never are.
    assert all(c[0] != "git" for c in wrapped_cmds if c)
    assert list(_RESTART) in unwrapped
    # And during recovery nothing is tagged: the recovery-phase npm/uv calls
    # (the rebuild after the rollback) run under the orchestrator's protection.
    # Forward ran exactly one npm ci + one build wrapped; any further ones are
    # recovery's and must be unwrapped.
    recovery_npm = [
        c
        for c in unwrapped
        if c[:2] == ["npm", "ci"] or c[:3] == ["npm", "run", "build"]
    ]
    assert recovery_npm, "recovery should have rebuilt without the expendable tag"
    # The refresh itself, not just any uv call: `uv tool dir` runs unwrapped
    # on the forward pass too, so it cannot stand in for the recovery refresh.
    recovery_installs = [c for c in unwrapped if c[:3] == ["uv", "tool", "install"]]
    recovery_syncs = [c for c in unwrapped if c[:2] == ["uv", "sync"]]
    assert len(recovery_installs) == 2, "recovery should reinstall both tools untagged"
    assert recovery_syncs, "recovery should re-sync the venv untagged"


def test_the_expendable_wrapper_hands_the_command_its_argv_intact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real wrapper, not the test's stand-in: `exec "$@"` must not re-split
    # or glob what it is handed. It is a shell, so an argument carrying a space
    # or a metacharacter is exactly where this would go wrong -- and it would
    # go wrong silently, as a build that ran against the wrong path.
    bands = update_banding._load_bands(_WORKSPACE_ROOT)
    assert bands is not None, "the in-tree oom_priority package should be importable"
    monkeypatch.setattr(update_banding, "_BANDS", bands)

    wrapped = update_banding.as_expendable(["npm", "run", "build --out dir", "*"])

    assert wrapped[:2] == ["sh", "-c"]
    # argv is passed positionally, never interpolated into the script.
    assert "build --out dir" not in wrapped[2]
    assert wrapped[2].endswith('exec "$@"')
    assert wrapped[3:] == ["sh", "npm", "run", "build --out dir", "*"]
    # And the counterpart wrapper leaves the command exactly as it was.
    assert update_banding.keep_protected(wrapped) == wrapped


def test_a_tree_without_the_bands_package_runs_unbanded_rather_than_failing(
    tmp_path: Path,
) -> None:
    # The apply is staged and run against older trees by design, and one that
    # predates `oom_priority` must degrade to no banding -- with `as_expendable`
    # falling back to a plain passthrough rather than a wrapper naming a band
    # that does not exist.
    assert update_banding._load_bands(tmp_path) is None
    assert update_banding.as_expendable(["npm", "ci"]) == ["npm", "ci"]


def _write_bands_package(repo_root: Path, body: str) -> Path:
    package = repo_root / "system/services/oom_priority/src/oom_priority"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("")
    package.joinpath("bands.py").write_text(body)
    return package


def test_a_bands_module_predating_the_tag_helper_degrades_instead_of_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The live failure this guards: the module loads from the *pre-merge* tree,
    # so its surface is whatever release the workspace is updating from --
    # every release through minds-v0.4.1 shipped a bands.py with no
    # `oom_tag_shell_prefix`. Reading it unguarded raised an AttributeError at
    # the first expendable-wrapped step, which is past the merge and past the
    # snapshots, where only ApplyFailed is caught: the workspace was left
    # half-applied with a marker and no rollback. Importable-but-older must
    # degrade exactly like absent.
    monkeypatch.delitem(sys.modules, "oom_priority", raising=False)
    monkeypatch.delitem(sys.modules, "oom_priority.bands", raising=False)
    _write_bands_package(
        tmp_path,
        "AGENT_SUBPROCESS = 900\n"
        "SERVICE_BANDS = {'system_interface': 20}\n"
        "def set_oom_score_adj(pid, adj):\n"
        "    return False\n",
    )

    bands = update_banding._load_bands(tmp_path)

    assert bands is None
    monkeypatch.setattr(update_banding, "_BANDS", bands)
    assert update_banding.as_expendable(["npm", "ci"]) == ["npm", "ci"]


def test_a_bands_module_carrying_the_whole_surface_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other side of the refusal, so it cannot be satisfied by refusing
    # everything: a module carrying the whole required surface is accepted and
    # actually tagged with.
    monkeypatch.delitem(sys.modules, "oom_priority", raising=False)
    monkeypatch.delitem(sys.modules, "oom_priority.bands", raising=False)
    _write_bands_package(
        tmp_path,
        "AGENT_SUBPROCESS = 900\n"
        "SERVICE_BANDS = {'system_interface': 20}\n"
        "def set_oom_score_adj(pid, adj):\n"
        "    return False\n"
        "def oom_tag_shell_prefix(adj):\n"
        "    return f'tag {adj}; '\n",
    )

    bands = update_banding._load_bands(tmp_path)

    assert bands is not None
    monkeypatch.setattr(update_banding, "_BANDS", bands)
    assert update_banding.as_expendable(["npm", "ci"]) == [
        "sh",
        "-c",
        'tag 900; exec "$@"',
        "sh",
        "npm",
        "ci",
    ]


def test_the_apply_bands_itself_to_the_pre_merge_trees_system_interface_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The band is read off the *pre-merge* tree's own map rather than named by
    # this script, which is what lets an apply staged onto an older release
    # band itself the same way it does here. The fake tree deliberately puts
    # the system interface somewhere it is not in this tree, so a band this
    # script hardcoded -- its own constant, or the literal 20 -- would fail
    # here rather than silently agreeing with the current map.
    monkeypatch.delitem(sys.modules, "oom_priority", raising=False)
    monkeypatch.delitem(sys.modules, "oom_priority.bands", raising=False)
    monkeypatch.setattr(update_banding, "_BANDS", None)
    _write_bands_package(
        tmp_path,
        "AGENT_SUBPROCESS = 900\n"
        "SERVICE_BANDS = {'owner-exec': 5, 'system_interface': 33, 'cron': 55}\n"
        "BANDED_AS = []\n"
        "def set_oom_score_adj(pid, adj):\n"
        "    BANDED_AS.append(adj)\n"
        "    return True\n"
        "def oom_tag_shell_prefix(adj):\n"
        "    return f'tag {adj}; '\n",
    )

    update_banding.protect_from_memory_shed(tmp_path)

    bands = update_banding._BANDS
    assert bands is not None
    assert bands.BANDED_AS == [33]


def test_a_bands_module_whose_map_lacks_the_applys_service_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The band is a key read out of the pre-merge tree's map, so it is as
    # absent-able as an attribute of that tree's module. Unguarded it would be a
    # KeyError out of __main__, before main() -- taking `recover --if-stale`,
    # the boot and cron self-healing for a stale apply, with it.
    monkeypatch.delitem(sys.modules, "oom_priority", raising=False)
    monkeypatch.delitem(sys.modules, "oom_priority.bands", raising=False)
    monkeypatch.setattr(update_banding, "_BANDS", None)
    _write_bands_package(
        tmp_path,
        "AGENT_SUBPROCESS = 900\n"
        "SERVICE_BANDS = {'owner-exec': 5, 'cron': 55}\n"
        "def set_oom_score_adj(pid, adj):\n"
        "    return True\n"
        "def oom_tag_shell_prefix(adj):\n"
        "    return f'tag {adj}; '\n",
    )

    update_banding.protect_from_memory_shed(tmp_path)

    assert update_banding._BANDS is None
    assert update_banding.as_expendable(["npm", "ci"]) == ["npm", "ci"]


def test_every_bands_attribute_the_script_reads_is_declared_required() -> None:
    # The load-time refusal only protects what _REQUIRED_BANDS_ATTRIBUTES names.
    # A `_BANDS.<attr>` added later without being listed is the same
    # cross-version crash again, and it would surface in a half-applied
    # workspace rather than here. (An attribute reached through `getattr` with a
    # default is already guarded and does not match this form.)
    source = (_SCRIPTS_DIR / "update_banding.py").read_text()
    read_attributes = set(re.findall(r"(?<![A-Za-z0-9_])_BANDS\.(\w+)", source))
    assert read_attributes, "the attribute-read pattern stopped matching the script"

    undeclared = read_attributes - set(update_banding._REQUIRED_BANDS_ATTRIBUTES)

    assert not undeclared, (
        f"{sorted(undeclared)} are read off the pre-merge tree's bands module but are "
        "not in _REQUIRED_BANDS_ATTRIBUTES, so a tree predating them still loads"
    )


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["apply", "--merge-ref", "x"], "cwd"),
        (["recover", "--if-stale", "--grace-seconds", "0"], "cwd"),
        (["--repo-root", "/w", "apply", "--merge-ref", "x"], "/w"),
        (["apply", "--repo-root", "/w", "--merge-ref", "x"], "/w"),
        (["apply", "--repo-root=/w"], "/w"),
        # Only the two motions that can be interrupted half-way through
        # replacing what the workspace runs band themselves.
        (["resolve-target", "--local-tags"], None),
        (["classify-merge", "--target", "main"], None),
        ([], None),
        # A flag *value* that happens to spell a banded subcommand must not
        # promote a read-only command into a banded one.
        (["bootstrap-skill", "--ref", "apply"], None),
    ],
    ids=[
        "apply",
        "recover",
        "repo-root-before",
        "repo-root-after",
        "repo-root-equals",
        "resolve-target",
        "classify-merge",
        "no-args",
        "apply-as-a-flag-value",
    ],
)
def test_only_apply_and_recover_band_themselves(
    argv: list[str], expected: str | None
) -> None:
    # Getting this wrong runs the apply unbanded, so a memory shed can kill the
    # orchestrator mid-motion -- the half-applied state the marker and the
    # snapshots exist to prevent. It is a crude parse because banding has to
    # happen before argparse does anything.
    target = update_self._shed_protection_target(argv)

    if expected is None:
        assert target is None
    else:
        assert target == (Path.cwd() if expected == "cwd" else Path(expected))


# --- apply: the uv tool environments ------------------------------------------------
#
# The refresh rebuilds the two uv tool environments the workspace runs from.
# ``uv tool install --reinstall`` rebuilds a tool from its base package alone,
# so both halves of this are load-bearing: WHICH installation is rebuilt
# (``_uv_tool_env``, from the console script's own shebang) and WHAT it is
# rebuilt with (``_tool_extras``, read back out of uv's receipt). For the mngr
# tool those extras ARE its plugins.


def _with_receipt(
    runner: _RecordingRunner, tool_dir: Path, tool: str, body: str
) -> None:
    """Point ``uv tool dir`` at ``tool_dir`` and give ``tool`` a receipt there."""
    runner.respond(("uv", "tool", "dir"), _Result(stdout=f"{tool_dir}\n"))
    (tool_dir / tool).mkdir(parents=True, exist_ok=True)
    (tool_dir / tool / update_layout.RECEIPT).write_text(body)


def _install_argv(runner: _RecordingRunner, source_dir: str) -> list[str]:
    """The ``uv tool install`` call that re-pins ``source_dir``."""
    return next(
        argv
        for argv in runner.argvs_starting("uv", "tool", "install")
        if source_dir in argv
    )


def test_the_refresh_preserves_a_tools_registered_plugins(
    apply_repo: Path, tmp_path: Path
) -> None:
    # A bare --reinstall rebuilds a tool from its base package alone. For the
    # mngr tool the extras ARE its plugins, so dropping them leaves a CLI that
    # cannot parse its own plugin config -- an update that breaks the workspace
    # in a new way while reporting success.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    _with_receipt(
        runner,
        tmp_path / "tools",
        update_layout.MNGR_TOOL_NAME,
        """
        [tool]
        requirements = [
            { name = "imbue-mngr", editable = "/repo/system/vendor/mngr/libs/mngr" },
            { name = "imbue-mngr-claude", editable = "/repo/system/vendor/mngr/libs/mngr_claude" },
            { name = "imbue-mngr-wait", editable = "/repo/system/vendor/mngr/libs/mngr_wait" },
        ]
        """,
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert _install_argv(runner, update_layout.MNGR_DIR) == [
        "uv",
        "tool",
        "install",
        "-e",
        update_layout.MNGR_DIR,
        "--with-editable",
        "/repo/system/vendor/mngr/libs/mngr_claude",
        "--with-editable",
        "/repo/system/vendor/mngr/libs/mngr_wait",
        "--reinstall",
    ]


def test_the_refresh_registers_the_merged_trees_new_plugins(
    apply_repo: Path, tmp_path: Path
) -> None:
    # The receipt names only the plugins a tool was installed with last time.
    # A release that ships a new plugin (opencode, say) merges a settings.toml
    # its agent type needs, and a reinstall from the receipt alone leaves an
    # mngr that rejects its own config at the restart -- so the merged tree's
    # manifest is unioned in, for both tools, without repeating what the
    # receipt already has.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    _with_receipt(
        runner,
        tmp_path / "tools",
        update_layout.MNGR_TOOL_NAME,
        f"""
        [tool]
        requirements = [
            {{ name = "imbue-mngr", editable = "{apply_repo}/system/vendor/mngr/libs/mngr" }},
            {{ name = "imbue-mngr-claude", editable = "{apply_repo}/system/vendor/mngr/libs/mngr_claude" }},
        ]
        """,
    )
    manifest = apply_repo / update_layout.PLUGIN_MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        """
        [[plugins]]
        path = "system/vendor/mngr/libs/mngr_claude"
        tools = ["mngr", "system-interface"]

        [[plugins]]
        path = "system/vendor/mngr/libs/mngr_opencode"
        tools = ["mngr", "system-interface"]

        [[plugins]]
        path = "system/vendor/mngr/libs/mngr_wait"
        tools = ["mngr"]
        """
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert _install_argv(runner, update_layout.MNGR_DIR) == [
        "uv",
        "tool",
        "install",
        "-e",
        update_layout.MNGR_DIR,
        "--with-editable",
        f"{apply_repo}/system/vendor/mngr/libs/mngr_claude",
        "--with-editable",
        f"{apply_repo}/system/vendor/mngr/libs/mngr_opencode",
        "--with-editable",
        f"{apply_repo}/system/vendor/mngr/libs/mngr_wait",
        "--reinstall",
    ]
    assert _install_argv(runner, update_layout.SYSTEM_INTERFACE_DIR) == [
        "uv",
        "tool",
        "install",
        "-e",
        update_layout.SYSTEM_INTERFACE_DIR,
        "--with-editable",
        f"{apply_repo}/system/vendor/mngr/libs/mngr_claude",
        "--with-editable",
        f"{apply_repo}/system/vendor/mngr/libs/mngr_opencode",
        "--reinstall",
    ]


def test_the_refresh_repins_the_base_to_the_in_tree_source(
    apply_repo: Path, tmp_path: Path
) -> None:
    # A receipt that has lost its editable marker must not make us re-resolve
    # the base from the index -- that would silently swap the workspace's own
    # vendored code for a published release.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    _with_receipt(
        runner,
        tmp_path / "tools",
        update_layout.MNGR_TOOL_NAME,
        '[tool]\nrequirements = [{ name = "imbue-mngr" }]\n',
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert _install_argv(runner, update_layout.MNGR_DIR) == [
        "uv",
        "tool",
        "install",
        "-e",
        update_layout.MNGR_DIR,
        "--reinstall",
    ]


def test_the_refresh_targets_the_installation_actually_on_path(
    apply_repo: Path, tmp_path: Path
) -> None:
    # uv's default tool directory follows $HOME, which is not the one
    # build_workspace.sh installed under -- so defaulting rebuilds a shadow
    # copy nothing on PATH runs, and reports the stale tool everyone actually
    # executes as successfully refreshed.
    bin_dir = tmp_path / "root" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tools = tmp_path / "root" / ".local" / "share" / "uv" / "tools"
    (bin_dir / update_layout.MNGR_EXECUTABLE).write_text(
        f"#!{tools}/{update_layout.MNGR_TOOL_NAME}/bin/python3\nimport sys\n"
    )
    (tools / update_layout.MNGR_TOOL_NAME).mkdir(parents=True)
    (tools / update_layout.MNGR_TOOL_NAME / update_layout.RECEIPT).write_text(
        "[tool]\nrequirements = []\n"
    )
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    runner.executables[update_layout.MNGR_EXECUTABLE] = str(
        bin_dir / update_layout.MNGR_EXECUTABLE
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    envs = {
        argv[4]: env
        for argv, env in zip(runner.calls, runner.envs)
        if argv[:4] == ["uv", "tool", "install", "-e"] and env is not None
    }
    assert envs[update_layout.MNGR_DIR]["UV_TOOL_DIR"] == str(tools)
    assert envs[update_layout.MNGR_DIR]["UV_TOOL_BIN_DIR"] == str(bin_dir)
    # Targeting is per executable, not global: the other tool is not on PATH
    # here, so its install is left to uv's own default rather than aimed at the
    # directory that happens to hold mngr.
    assert "UV_TOOL_DIR" not in envs[update_layout.SYSTEM_INTERFACE_DIR]


def test_the_refresh_survives_a_tool_with_no_receipt(apply_repo: Path) -> None:
    # No readable receipt means the tool is not installed (or predates
    # receipts); the refresh must still run as the plain install it would
    # otherwise be, for both tools.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    runner.respond(("uv", "tool", "dir"), _Result(returncode=1))

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert len(runner.argvs_starting("uv", "tool", "install")) == 2


def test_the_refresh_survives_a_uv_that_cannot_be_run_at_all(
    apply_repo: Path, capsys
) -> None:
    # The same verdict as a non-zero `uv tool dir`, reached the other way: uv
    # missing from the PATH the apply inherited raises rather than exiting.
    # Reading the extras is best effort, so it must cost the extras and a
    # warning -- not roll a landed merge back through the last-resort catch.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    runner.respond(("uv", "tool", "dir"), FileNotFoundError(2, "No such file", "uv"))

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert len(runner.argvs_starting("uv", "tool", "install")) == 2
    assert "could not be run" in capsys.readouterr().err


def test_the_refresh_reports_a_receipt_it_cannot_read(
    apply_repo: Path, tmp_path: Path, capsys
) -> None:
    # A garbled receipt is not the fresh-install case: we had a tool and lost
    # the record of what it was installed with, so the reinstall below rebuilds
    # it without its plugins. Degrading silently would hand back exactly the
    # plugin-less CLI this refresh exists to prevent, and report success.
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)
    _with_receipt(
        runner,
        tmp_path / "tools",
        update_layout.MNGR_TOOL_NAME,
        "[tool]\nrequirements = [",
    )

    assert _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), apply_repo) == 0

    assert _install_argv(runner, update_layout.MNGR_DIR) == [
        "uv",
        "tool",
        "install",
        "-e",
        update_layout.MNGR_DIR,
        "--reinstall",
    ]
    reported = capsys.readouterr().err
    assert update_layout.MNGR_TOOL_NAME in reported and "drops any plugins" in reported


def test_tool_location_comes_from_the_console_scripts_shebang(tmp_path: Path) -> None:
    bin_dir = tmp_path / "root" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    tools = tmp_path / "root" / ".local" / "share" / "uv" / "tools"
    script = bin_dir / update_layout.MNGR_EXECUTABLE
    script.write_text(
        f"#!{tools}/{update_layout.MNGR_TOOL_NAME}/bin/python3\n"
        "# -*- coding: utf-8 -*-\nimport sys\n"
    )
    (tools / update_layout.MNGR_TOOL_NAME).mkdir(parents=True)
    (tools / update_layout.MNGR_TOOL_NAME / update_layout.RECEIPT).write_text(
        "[tool]\nrequirements = []\n"
    )

    location = update_environment._tool_location(script, update_layout.MNGR_TOOL_NAME)

    assert location == (tools, bin_dir)


@pytest.mark.parametrize(
    "contents",
    [
        "import sys\n",  # no shebang at all
        "#!\n",  # a shebang naming no interpreter
        "#!/python3\n",  # too shallow to name a tool directory
    ],
    ids=["no-shebang", "empty-shebang", "shallow-interpreter"],
)
def test_tool_location_declines_what_it_cannot_read(
    contents: str, tmp_path: Path
) -> None:
    # Anything we cannot interpret means we do not know which installation this
    # is, and the caller falls back to letting uv decide -- guessing a directory
    # would be worse than uv's own default.
    script = tmp_path / update_layout.MNGR_EXECUTABLE
    script.write_text(contents)

    assert (
        update_environment._tool_location(script, update_layout.MNGR_TOOL_NAME) is None
    )


def test_tool_location_declines_the_workspace_venvs_console_script(
    tmp_path: Path,
) -> None:
    # Both tool names are also `uv sync` members, so PATH can resolve to the
    # venv's own entrypoint. Deriving a "tool directory" from that would build a
    # tool environment inside the served checkout -- dirtying the tree the next
    # apply refuses to run on. No receipt, no deal.
    venv_bin = tmp_path / "workspace" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    script = venv_bin / update_layout.TOOL_NAME
    script.write_text(f"#!{venv_bin}/python3\nimport sys\n")

    assert update_environment._tool_location(script, update_layout.TOOL_NAME) is None


def test_tool_location_declines_a_script_it_cannot_open(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"

    assert (
        update_environment._tool_location(missing, update_layout.MNGR_TOOL_NAME) is None
    )


# --- snapshots (real directories) --------------------------------------------------


def test_snapshots_roundtrip_bundle_envs_and_node_modules(tmp_path: Path) -> None:
    repo_root = _make_apply_repo(tmp_path)
    _write_bundle(repo_root)
    (repo_root / update_layout.FRONTEND_DIR / "node_modules").mkdir(parents=True)
    (
        repo_root / update_layout.FRONTEND_DIR / "node_modules" / "left-pad.js"
    ).write_text("old")
    (repo_root / ".venv").mkdir()
    (repo_root / ".venv" / "marker.txt").write_text("old-venv")
    plan = _plan(
        [
            "system/apps/system_interface/frontend/src/App.ts",
            "system/apps/system_interface/frontend/package.json",
            "system/apps/system_interface/pyproject.toml",
        ]
    )
    runner = _RecordingRunner()  # no tools on PATH -> no tool-env targets

    snapshots = update_environment.take_snapshots(plan, repo_root, runner, [])

    assert {record.name for record in snapshots} == {"bundle", "node_modules", "venv"}
    # Destroy the originals, as the failed forward steps would.
    shutil.rmtree(repo_root / update_layout.STATIC_DIR)
    (repo_root / ".venv" / "marker.txt").write_text("wrecked")
    shutil.rmtree(repo_root / update_layout.FRONTEND_DIR / "node_modules")

    failed = update_environment.restore_snapshots(snapshots)

    assert failed == []
    assert _bundle_exists(repo_root)
    assert (repo_root / ".venv" / "marker.txt").read_text() == "old-venv"
    assert (
        repo_root / update_layout.FRONTEND_DIR / "node_modules" / "left-pad.js"
    ).read_text() == "old"


def test_existing_snapshot_copies_are_reused_not_overwritten(tmp_path: Path) -> None:
    # A resumed apply must not re-copy: by then the live state may already be
    # part-destroyed, and re-copying would overwrite the good copy with wreckage.
    repo_root = _make_apply_repo(tmp_path)
    _write_bundle(repo_root)
    plan = _plan(["system/apps/system_interface/frontend/src/App.ts"])
    runner = _RecordingRunner()
    first = update_environment.take_snapshots(plan, repo_root, runner, [])
    (repo_root / update_layout.FRONTEND_BUILD_INDEX).write_text("wrecked mid-apply")

    second = update_environment.take_snapshots(plan, repo_root, runner, first)

    assert [record.copy for record in second] == [record.copy for record in first]
    copy_index = Path(first[0].copy) / "index.html"
    assert "wrecked" not in copy_index.read_text()


def test_a_missing_snapshot_target_degrades_to_a_note(tmp_path: Path, capsys) -> None:
    repo_root = _make_apply_repo(tmp_path)  # no bundle was ever built
    plan = _plan(["system/apps/system_interface/frontend/src/App.ts"])

    snapshots = update_environment.take_snapshots(
        plan, repo_root, _RecordingRunner(), []
    )

    assert snapshots == []
    assert "nothing to copy aside" in capsys.readouterr().err


def test_a_copy_that_cannot_be_taken_degrades_to_a_warning(
    tmp_path: Path, capsys
) -> None:
    # Copying aside is a precaution, not a precondition: a workspace where it
    # cannot be done still gets its update, and a failed rollback falls back to
    # rebuilding. Refusing here instead would make the precaution the thing
    # that blocks the repair.
    repo_root = _make_apply_repo(tmp_path)
    _write_bundle(repo_root)
    # The copies' destination cannot be created: its parent is a regular file.
    (repo_root / update_apply_contract.STATE_DIR_REL).mkdir(parents=True)
    (
        repo_root
        / update_apply_contract.STATE_DIR_REL
        / update_apply_contract.SNAPSHOTS_DIRNAME
    ).write_text("not a directory")
    plan = _plan(["system/apps/system_interface/frontend/src/App.ts"])

    snapshots = update_environment.take_snapshots(
        plan, repo_root, _RecordingRunner(), []
    )

    assert snapshots == []
    assert "could not copy 'bundle' aside" in capsys.readouterr().err
    assert _bundle_exists(repo_root)  # the original is untouched


def test_the_recovery_rebuild_does_not_run_npm_ci_over_a_restored_node_modules(
    unbuilt_apply_repo: Path,
) -> None:
    # `npm ci` deletes node_modules before it installs, so running it during
    # recovery over the copy just put back would destroy the one thing the
    # rollback restored -- and then need a registry to get it back. This
    # workspace has never built a bundle, so recovery takes the rebuild branch
    # (there is no bundle copy to restore) with node_modules already back.
    node_modules = unbuilt_apply_repo / update_layout.FRONTEND_DIR / "node_modules"
    node_modules.mkdir(parents=True)
    (node_modules / "left-pad.js").write_text("restored")
    runner = _apply_runner(_FRONTEND_MANIFEST_DIFF + _FRONTEND_DIFF, unbuilt_apply_repo)
    # Only the forward build fails; recovery's rebuild of the known-good tree
    # succeeds, as it must for the rollback to be confirmed.
    runner.respond(
        ("npm", "run", "build"), [_Result(returncode=1, stderr="boom"), _Result()]
    )

    code = _apply(runner, _FakeHttp(_all_healthy), _FakeSpawner(), unbuilt_apply_repo)

    assert code == 2
    assert len(runner.argvs_starting("npm", "run", "build")) == 2  # forward, recovery
    # The forward pass ran the only `npm ci`; recovery rebuilt against the
    # restored node_modules rather than wiping it.
    assert len(runner.argvs_starting("npm", "ci")) == 1
    assert (node_modules / "left-pad.js").read_text() == "restored"


def test_a_rollback_rebuilds_the_tool_envs_it_could_not_copy_aside(
    apply_repo: Path, capsys
) -> None:
    # The venv copy alone is not the environment: the two uv tools are
    # recorded (and restored) separately, and one that could not be located at
    # snapshot time has no copy to put back. A rollback that keyed the rebuild
    # on the venv alone reported success with both tools still built from the
    # rolled-back-away tree -- the ModuleNotFoundError-on-mngr state.
    (apply_repo / ".venv").mkdir()
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)  # no tools on PATH
    spawner = _FakeSpawner(output="ImportError: boom", exited=True)

    code = _apply(
        runner,
        _FakeHttp(lambda url: 200 if _is_live(url) else None),
        spawner,
        apply_repo,
    )

    assert code == 2
    # The forward installs are wrapped expendable; recovery's are not, and
    # they must have happened even though the venv copy went back fine.
    recovery_installs = [
        c for c in runner.raw_calls if c[:3] == ["uv", "tool", "install"]
    ]
    assert len(recovery_installs) == 2
    err = capsys.readouterr().err
    assert "could not locate the uv tool environment behind 'mngr'" in err
    assert "could not locate the uv tool environment behind 'system-interface'" in err


def test_a_rollback_survives_a_plugin_manifest_it_cannot_read(
    apply_repo: Path, capsys
) -> None:
    # The rollback's own environment rebuild reads the merged tree's plugin
    # manifest. _recover_running_state catches only ApplyFailed and OSError, so
    # a TOMLDecodeError raised while parsing it escaped the rollback, the
    # apply's own handler, and apply_update itself -- leaving a live marker, an
    # unrestored environment and no emergency record, on the one path that is
    # supposed to be the last line of defense.
    (apply_repo / ".venv").mkdir()
    manifest = apply_repo / update_layout.PLUGIN_MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text("[[plugins]\npath = ")
    runner = _apply_runner(_BACKEND_MANIFEST_DIFF, apply_repo)  # no tools on PATH
    spawner = _FakeSpawner(output="ImportError: boom", exited=True)

    code = _apply(
        runner,
        _FakeHttp(lambda url: 200 if _is_live(url) else None),
        spawner,
        apply_repo,
    )

    assert code == 2
    assert update_apply_contract.read_marker(apply_repo) is None
    # The reinstalls still happened, from the receipt alone.
    assert len([c for c in runner.raw_calls if c[:3] == ["uv", "tool", "install"]]) == 2
    assert "skipping the plugin manifest" in capsys.readouterr().err


def test_the_spawner_captures_both_streams_of_a_real_child(tmp_path: Path) -> None:
    # The capture has to survive a real Popen: stderr is redirected onto
    # stdout's file and the parent closes its handle while the child keeps
    # writing. This models the case that matters -- a backend that dies on
    # import, whose traceback is the whole reason the pre-flight rejected the
    # merge, and which nothing else would ever record.
    output_path = tmp_path / "boot.log"

    spawned = update_runtime.Spawner().spawn(
        [
            sys.executable,
            "-c",
            "import sys; print('on stdout'); print('on stderr', file=sys.stderr)",
        ],
        cwd=str(tmp_path),
        env=dict(os.environ),
        output_path=output_path,
    )
    for _ in range(500):
        if spawned.has_exited():
            break
        time.sleep(0.01)
    spawned.terminate()

    assert spawned.has_exited()
    captured = spawned.read_output()
    assert "on stdout" in captured
    assert "on stderr" in captured


# --- the version-history ledger (real git) ------------------------------------


def _make_real_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "real-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "Initial workspace commit"],
        cwd=repo,
        check=True,
    )
    return repo


def _head_sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return int(out)


def test_the_starter_this_recreates_is_the_one_the_template_ships() -> None:
    # `publish-template`, `update-installed-template` and
    # update-published-template's ledger-entry reference all instruct an agent
    # to recreate a ledger "byte-identical to the shipped root file", and point
    # at this constant for the exact block. Nothing else compares the two, so a
    # preamble edited on one side alone would silently make that instruction
    # impossible to follow and hand a recreated ledger a different header.
    ledger = _WORKSPACE_ROOT / update_ledger._VERSION_HISTORY_REL
    # This file ships into every workspace made from the template, and there
    # the same path is that workspace's own ledger: entries are appended to it
    # (and a published template drops it entirely). The starter block is what
    # is left once the entry lines are taken back out, so the comparison
    # holds in a live workspace too rather than skipping for good after its
    # first update.
    if not ledger.exists():
        pytest.skip("no ledger here: a published template drops it")
    shipped = ledger.read_text()

    assert _without_ledger_entries(shipped) == _without_ledger_entries(
        update_ledger._VERSION_HISTORY_STARTER
    )


def _without_ledger_entries(text: str) -> list[str]:
    """The ledger's prose and headings, entry lines removed and blank runs
    collapsed -- what a workspace's appended entries leave untouched."""
    kept: list[str] = []
    for line in text.splitlines():
        if line.startswith(("- ", "### ")):
            continue
        if line.strip() == "" and kept and kept[-1] == "":
            continue
        kept.append(line)
    return kept


def test_ledger_creates_starter_seeds_origin_and_appends_idempotently(
    tmp_path: Path,
) -> None:
    repo = _make_real_repo(tmp_path)
    merge_sha = _head_sha(repo)
    runner = update_runtime.Runner()

    update_ledger.write_version_history_entry(
        repo, runner, "minds-v0.4.2", merge_sha, _TODAY
    )

    text = (repo / "docs/VERSION_HISTORY.md").read_text()
    lines = text.splitlines()
    workspace_at = lines.index("## Workspace")
    entries = [
        line
        for line in lines[workspace_at + 1 : lines.index("## Migrations")]
        if line.strip()
    ]
    # The origin seed is the FIRST line (the oldest event); the update follows.
    assert entries[0].startswith("- ") and "created from" in entries[0]
    assert "updated to minds-v0.4.2" in entries[1]
    short = merge_sha[:7]
    # Note padded to width 26 ("updated to minds-v0.4.2" is 23 chars -> 3 pad).
    assert entries[1] == f"- {_TODAY}  updated to minds-v0.4.2   {short}"
    # Committed as exactly one file, with the non-marker subject.
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert subject == "version history: updated to minds-v0.4.2"

    # A retried landing is a no-op: same note + same sha appends nothing and
    # commits nothing.
    commits_before = _commit_count(repo)
    update_ledger.write_version_history_entry(
        repo, runner, "minds-v0.4.2", merge_sha, _TODAY
    )
    assert (repo / "docs/VERSION_HISTORY.md").read_text() == text
    assert _commit_count(repo) == commits_before


def test_the_ledger_commit_skips_hooks_like_the_rollback_commit_does(
    tmp_path: Path,
) -> None:
    # This commit lands after the marker is cleared. A hook refusing it would
    # leave docs/VERSION_HISTORY.md staged, and every later apply and recover
    # would refuse the dirty tree until someone cleaned up by hand.
    repo = _make_real_repo(tmp_path)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    (hooks / "pre-commit").write_text("#!/bin/sh\necho refused >&2\nexit 1\n")
    (hooks / "pre-commit").chmod(0o755)
    _git_in(repo, "config", "core.hooksPath", str(hooks))
    merge_sha = _head_sha(repo)

    update_ledger.write_version_history_entry(
        repo, update_runtime.Runner(), "minds-v0.4.2", merge_sha, _TODAY
    )

    assert _git_in(repo, "log", "-1", "--format=%s") == (
        "version history: updated to minds-v0.4.2"
    )
    assert _git_in(repo, "status", "--porcelain") == ""


def test_a_ledger_commit_that_fails_leaves_the_file_unstaged_and_named(
    apply_repo: Path, capsys
) -> None:
    runner = _apply_runner(_DOCS_DIFF, apply_repo)
    runner.respond(
        ("git", "commit"),
        subprocess.CalledProcessError(128, ["git", "commit"], stderr="index.lock"),
    )

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    assert runner.ran("git", "reset", "-q", "--", "docs/VERSION_HISTORY.md")
    err = capsys.readouterr().err
    assert "could not be recorded" in err
    assert "docs/VERSION_HISTORY.md as an unstaged working-tree change" in err


def test_ledger_origin_names_the_release_when_one_is_reachable(tmp_path: Path) -> None:
    repo = _make_real_repo(tmp_path)
    subprocess.run(["git", "tag", "minds-v0.1.0"], cwd=repo, check=True)
    # A later commit ON TOP of the tagged base -- describe (reachability) must
    # still resolve the tag; a --points-at lookup would come up empty.
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "some ordinary work"],
        cwd=repo,
        check=True,
    )
    merge_sha = _head_sha(repo)

    update_ledger.write_version_history_entry(
        repo, update_runtime.Runner(), "minds-v0.2.0", merge_sha, _TODAY
    )

    text = (repo / "docs/VERSION_HISTORY.md").read_text()
    assert "created from minds-v0.1.0" in text


def test_apply_writes_the_ledger_and_runs_env_converge_post_success(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_DOCS_DIFF, apply_repo)

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    ledger = apply_repo / "docs/VERSION_HISTORY.md"
    assert ledger.exists()
    assert "updated to minds-v0.4.2" in ledger.read_text()
    assert runner.ran("git", "add", "docs/VERSION_HISTORY.md")
    assert runner.ran("uv", "run", "env-converge", "upgrade")
    # The converge comes after the ledger commit: it is post-success bookkeeping.
    add_index = runner.calls.index(["git", "add", "docs/VERSION_HISTORY.md"])
    converge_index = runner.calls.index(["uv", "run", "env-converge", "upgrade"])
    assert add_index < converge_index


def test_a_failed_apply_never_writes_the_ledger_or_moves_apt_state(
    apply_repo: Path,
) -> None:
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 2
    assert not (apply_repo / "docs/VERSION_HISTORY.md").exists()
    assert not runner.ran("uv", "run", "env-converge")


def test_env_converge_failure_is_a_warning_not_a_rollback(
    apply_repo: Path, capsys
) -> None:
    runner = _apply_runner(_DOCS_DIFF, apply_repo)
    runner.respond(
        ("uv", "run", "env-converge"), _Result(returncode=1, stderr="no network")
    )

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "env-converge upgrade` failed" in err
    assert not runner.ran("git", "checkout", _ROLLBACK, "--")


def test_an_env_converge_that_cannot_be_spawned_is_a_warning_not_a_traceback(
    apply_repo: Path, capsys
) -> None:
    # Post-success bookkeeping: an update that landed healthy must not turn
    # into a non-zero exit because `uv` could not be resolved afterwards.
    runner = _apply_runner(_DOCS_DIFF, apply_repo)
    runner.respond(("uv", "run", "env-converge"), FileNotFoundError("uv: not found"))

    code = _apply(
        runner,
        _FakeHttp(_all_healthy),
        _FakeSpawner(),
        apply_repo,
        target_ref="minds-v0.4.2",
    )

    assert code == 0
    err = capsys.readouterr().err
    assert "env-converge upgrade` failed" in err
    assert "uv: not found" in err


# --- recover -------------------------------------------------------------------


def _recover(
    runner: _RecordingRunner,
    http: _FakeHttp,
    repo_root: Path,
    *,
    if_stale: bool = False,
    grace_seconds: float = 600.0,
    no_restart: bool = False,
    now: Callable[[], float] = lambda: 10_000.0,
    is_pid_live: Callable[[int], bool] = lambda pid: False,
) -> int:
    # The rollback commit's "is anything staged" question: the restore has
    # staged the reverted paths by then.
    runner.respond(("git", "diff", "--cached", "--quiet"), _Result(returncode=1))
    return update_apply.recover(
        repo_root,
        if_stale=if_stale,
        grace_seconds=grace_seconds,
        no_restart=no_restart,
        runner=runner,
        http=http,
        sleeper=lambda _s: None,
        base_url=_LIVE_BASE,
        now=now,
        is_pid_live=is_pid_live,
    )


@pytest.mark.parametrize(
    ("has_marker", "is_pid_live", "now", "does_act"),
    [
        (False, False, 10_000.0, False),
        (True, True, 10_000.0, False),
        (True, False, 1000.0 + 60.0, False),
        (True, False, 10_000.0, True),
    ],
    ids=[
        "no-marker",
        "process-still-live",
        "within-the-grace-period",
        "dead-and-stale",
    ],
)
def test_recover_if_stale_acts_only_on_a_marker_that_is_really_stale(
    apply_repo: Path, has_marker: bool, is_pid_live: bool, now: float, does_act: bool
) -> None:
    # The unattended guard runs from cron every five minutes forever, so it has
    # to be a silent no-op in every normal state: a marker whose process is
    # still alive is a healthy apply mid-motion, and one that only just died is
    # the DRI agent's window to re-run the idempotent apply itself.
    if has_marker:
        _plant_marker(apply_repo)  # updated_at = 1000.0
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _recover(
        runner,
        _FakeHttp(_all_healthy),
        apply_repo,
        if_stale=True,
        now=lambda: now,
        is_pid_live=lambda pid: is_pid_live,
    )

    assert code == 0
    if does_act:
        assert runner.ran("git", "checkout", _ROLLBACK, "--")
        assert runner.ran("git", "commit", "--no-verify")
        assert not _marker_exists(apply_repo)
    else:
        assert runner.calls == []
        assert _marker_exists(apply_repo) is has_marker


def test_explicit_recover_refuses_a_live_apply(apply_repo: Path, capsys) -> None:
    _plant_marker(apply_repo)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _recover(
        runner, _FakeHttp(_all_healthy), apply_repo, is_pid_live=lambda pid: True
    )

    assert code == 1
    assert "still running" in capsys.readouterr().err
    assert runner.calls == []


def test_recover_claims_the_marker_with_its_own_pid_before_touching_the_tree(
    apply_repo: Path,
) -> None:
    # The apply's own concurrency check reads the marker's pid: a DRI agent
    # re-running the apply during a cron recover would find the dead apply's
    # pid, adopt the marker, and merge on top of the half-restored tree.
    _plant_snapshotted_marker(apply_repo, pid=12345)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    pid_at_restore: list[int] = []

    def observe(argv: list[str]) -> None:
        if argv[:2] == ["git", "checkout"] and not pid_at_restore:
            marker = update_apply_contract.read_marker(apply_repo)
            assert marker is not None
            pid_at_restore.append(marker.pid)

    runner.on_command = observe

    assert _recover(runner, _FakeHttp(_all_healthy), apply_repo) == 0

    assert pid_at_restore == [os.getpid()]


def test_recover_restores_snapshots_and_restarts_when_the_apply_had(
    apply_repo: Path,
) -> None:
    _plant_snapshotted_marker(apply_repo, live_service_restarted=True)
    # Wreck the bundle, as a kill mid-build leaves it.
    shutil.rmtree(apply_repo / update_layout.STATIC_DIR)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _recover(runner, _FakeHttp(_all_healthy), apply_repo)

    assert code == 0
    assert _bundle_exists(apply_repo)  # restored by copy, not rebuilt
    assert not runner.ran("npm")
    assert runner.ran(*_RESTART)  # the apply had restarted, so recovery must
    assert not _marker_exists(apply_repo)


def test_recover_no_restart_restores_disk_state_only(apply_repo: Path) -> None:
    _plant_snapshotted_marker(
        apply_repo, live_service_restarted=True, provisioner_ran=True
    )
    shutil.rmtree(apply_repo / update_layout.STATIC_DIR)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy)

    code = _recover(runner, http, apply_repo, no_restart=True)

    assert code == 0
    assert _bundle_exists(apply_repo)
    # Boot path: no restarts, no probes -- nothing is running yet. The
    # provisioner re-run still happens (it repairs the global toolchain).
    assert not runner.ran("mngr")
    assert http.get_urls == [] and http.page_urls == []
    assert runner.ran(*_PROVISION)
    # Forced past the provision guard, whose marker the rolled-back tree matches.
    provisioner_env = runner.envs[runner.calls.index(list(_PROVISION))]
    assert provisioner_env is not None and provisioner_env["PROVISION_FORCE"] == "1"
    assert not _marker_exists(apply_repo)


def test_recover_no_restart_keeps_the_copies_it_could_not_put_back(
    apply_repo: Path, capsys
) -> None:
    """A copy that would not restore is kept, and the report says so.

    Deleting it would destroy the only remaining way back -- a restore that
    failed for anything but a missing copy leaves the copy sitting right there.
    """
    snapshots_root = (
        apply_repo
        / update_apply_contract.STATE_DIR_REL
        / update_apply_contract.SNAPSHOTS_DIRNAME
    )
    copy = snapshots_root / "bundle"
    copy.mkdir(parents=True)
    (copy / "index.html").write_text("the pre-apply bundle")
    # The restore cannot land: the destination's own parent is a regular file.
    (apply_repo / "blocked").write_text("not a directory")
    _plant_marker(
        apply_repo,
        snapshots=[
            update_apply_contract.SnapshotRecord(
                name="bundle",
                source=str(apply_repo / "blocked" / "static"),
                copy=str(copy),
            )
        ],
    )
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    code = _recover(runner, _FakeHttp(_all_healthy), apply_repo, no_restart=True)

    # The boot path's emergency: nothing is running to probe, the marker is
    # gone, and the services are about to boot over a non-restored bundle --
    # so the exit code says so and the record is what keeps it visible.
    assert code == 3
    assert not _marker_exists(apply_repo)
    assert (copy / "index.html").read_text() == "the pre-apply bundle"
    err = capsys.readouterr().err
    assert "could not restore: bundle" in err
    assert str(snapshots_root) in err
    # Never the unqualified "the tree and pre-apply state are rolled back".
    assert "pre-apply state is NOT" in err
    record = _read_emergency(apply_repo)
    assert record["dri_agent"] == "the-lead"
    assert "could not restore: bundle" in record["reason"]
    assert record["snapshots_dir"] == str(snapshots_root)


def test_recover_reaches_the_same_end_state_as_the_in_process_rollback(
    tmp_path: Path,
) -> None:
    def _restore_relevant(calls: list[list[str]]) -> list[list[str]]:
        # The rollback commit's *message* names why (build failure vs
        # interruption) and legitimately differs; the motions must not.
        return [
            c[:3] if c[:3] == ["git", "commit", "--no-verify"] else c
            for c in calls
            if c[:2] in (["git", "checkout"], ["git", "rm"])
            or c[:3] == ["git", "commit", "--no-verify"]
            or c[:1] == ["mngr"]
        ]

    # In-process: a frontend apply whose build fails and rolls back.
    repo_a = tmp_path / "a" / "repo"
    (repo_a / update_layout.FRONTEND_DIR).mkdir(parents=True)
    _write_bundle(repo_a)
    runner_a = _apply_runner(_FRONTEND_DIFF, repo_a)
    runner_a.respond(("npm", "run", "build"), _Result(returncode=1, stderr="boom"))
    assert _apply(runner_a, _FakeHttp(_all_healthy), _FakeSpawner(), repo_a) == 2

    # Interrupted: the same apply killed right after its build destroyed the
    # bundle, recovered by `recover` from the marker instead.
    repo_b = tmp_path / "b" / "repo"
    (repo_b / update_layout.FRONTEND_DIR).mkdir(parents=True)
    _write_bundle(repo_b)
    _plant_snapshotted_marker(repo_b, phase=update_apply_contract.PHASE_SNAPSHOTTED)
    shutil.rmtree(repo_b / update_layout.STATIC_DIR)
    runner_b = _apply_runner(_FRONTEND_DIFF, repo_b)
    assert _recover(runner_b, _FakeHttp(_all_healthy), repo_b) == 0

    # Same restore motions, same served bundle back on disk.
    assert _restore_relevant(runner_a.calls) == _restore_relevant(runner_b.calls)
    index_a = (repo_a / update_layout.FRONTEND_BUILD_INDEX).read_text()
    index_b = (repo_b / update_layout.FRONTEND_BUILD_INDEX).read_text()
    assert index_a == index_b


def test_recover_reports_an_emergency_when_it_cannot_restore_health(
    apply_repo: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The counterpart of the apply's exit 3: the tree is rolled back but the
    # workspace will not come back healthy. The marker still comes down -- re-
    # running the same failed rollback from cron would not help -- so this exit
    # is the only signal, and the kept snapshots are the operator's way back.
    _plant_snapshotted_marker(apply_repo)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    # The environment is the wrong source for the record's DRI agent, and this
    # is the path that proves it: cron and bootstrap set no MNGR_AGENT_NAME at
    # all, and an agent running `recover` by hand is not the agent whose apply
    # failed. Only the marker knows, and it comes down on this same path.
    monkeypatch.setenv("MNGR_AGENT_NAME", "the-recovering-agent")

    code = _recover(runner, _FakeHttp(lambda _url: 500), apply_repo)

    assert code == 3  # the apply's own emergency code, distinct from exit 1
    err = capsys.readouterr().err
    assert "EMERGENCY" in err
    assert not _marker_exists(apply_repo)
    # The copies outlive the failure: putting one back needs no npm, no
    # registry and no working mngr.
    assert _snapshot_copy(apply_repo, "bundle").exists()
    record = _read_emergency(apply_repo)
    assert record["dri_agent"] == "the-lead"
    assert _MERGE_REF in record["reason"]


def test_recover_clears_the_emergency_record_when_it_confirms_health(
    apply_repo: Path,
) -> None:
    # The other half of the record's life: a recovery that ends with the live
    # workspace confirmed healthy is exactly the evidence the record is stale.
    _plant_emergency(apply_repo)
    _plant_snapshotted_marker(apply_repo)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    assert _recover(runner, _FakeHttp(_all_healthy), apply_repo) == 0
    assert not update_apply_contract.emergency_path(apply_repo).exists()


@pytest.mark.parametrize(
    ("frontend_expected", "expected_account"),
    [
        (False, "was not serving a working frontend when that apply began either"),
        (None, "killed before it recorded whether the live UI"),
    ],
    ids=["baseline-broken", "baseline-unmeasured"],
)
def test_recover_without_a_confirmed_frontend_leaves_the_record_standing(
    apply_repo: Path, capsys, frontend_expected: bool | None, expected_account: str
) -> None:
    # An interrupted apply with no working frontend to hold the rollback to is
    # recovered on the backend's health alone -- the frontend is never probed
    # -- so this recovery has no evidence that the state the record describes
    # is over, and its closing line must not sign off on a UI nobody looked
    # at. That line is often the only account of an unattended recovery, and
    # the two ways to get here are not the same account: a baseline measured
    # broken is a UI already down, while an unmeasured one (the apply was
    # killed before the probe, which follows the merge) says nothing about it.
    _plant_emergency(apply_repo)
    _plant_snapshotted_marker(apply_repo, frontend_expected=frontend_expected)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    http = _FakeHttp(_all_healthy, page_responder=_placeholder_page)

    assert _recover(runner, http, apply_repo) == 0
    assert update_apply_contract.emergency_path(apply_repo).exists()
    closing_line = capsys.readouterr().err.strip().splitlines()[-1]
    assert "confirmed healthy" not in closing_line
    assert "cannot confirm it" in closing_line
    assert expected_account in closing_line


def test_recover_keeps_the_marker_when_its_git_cannot_be_spawned(
    apply_repo: Path, capsys
) -> None:
    # Like a failed git command, a git that cannot be spawned leaves the tree
    # mid-motion: report it, keep the marker for the next pass, no traceback.
    _plant_snapshotted_marker(apply_repo)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("git", "checkout"), FileNotFoundError("git: not found"))

    code = _recover(runner, _FakeHttp(_all_healthy), apply_repo)

    assert code == 1
    assert "git: not found" in capsys.readouterr().err
    assert _marker_exists(apply_repo)


def test_recover_that_finds_a_working_ui_over_a_broken_baseline_clears_the_record(
    apply_repo: Path, capsys
) -> None:
    # The baseline only decides what the rollback is held to, not what it may
    # observe: a UI that works after the rollback is the evidence the record
    # is stale, even when the apply began over a broken one.
    _plant_emergency(apply_repo)
    _plant_snapshotted_marker(apply_repo, frontend_expected=False)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)

    assert _recover(runner, _FakeHttp(_all_healthy), apply_repo) == 0

    assert not update_apply_contract.emergency_path(apply_repo).exists()
    assert "confirmed healthy" in capsys.readouterr().err.strip().splitlines()[-1]


def test_recover_provisioner_failure_still_counts_as_recovered(
    apply_repo: Path, capsys
) -> None:
    _plant_snapshotted_marker(apply_repo, provisioner_ran=True)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(("bash",), _Result(returncode=1, stderr="no network"))

    code = _recover(runner, _FakeHttp(_all_healthy), apply_repo)

    assert code == 0
    err = capsys.readouterr().err
    assert "still counts as recovered" in err
    assert not _marker_exists(apply_repo)


@pytest.mark.parametrize("no_restart", [False, True], ids=["live", "boot"])
def test_a_hung_provisioner_does_not_wedge_recovery(
    apply_repo: Path, capsys, no_restart: bool
) -> None:
    # Both recovery re-runs used to call bash directly with no budget: a
    # provisioner that hung (rather than failed) wedged recovery for good, and
    # on the cron path held the flock forever. They go through the same
    # bounded runner as the forward run, and a hang is a named failure that
    # still counts as recovered.
    _plant_snapshotted_marker(apply_repo, provisioner_ran=True)
    runner = _apply_runner(_FRONTEND_DIFF, apply_repo)
    runner.respond(
        ("bash",), subprocess.TimeoutExpired(cmd="bash setup_system.sh", timeout=1800)
    )

    code = _recover(runner, _FakeHttp(_all_healthy), apply_repo, no_restart=no_restart)

    assert code == 0
    assert not _marker_exists(apply_repo)
    assert "did not finish within" in capsys.readouterr().err
    provisioner_timeouts = [
        timeout
        for argv, timeout in zip(runner.calls, runner.timeouts)
        if tuple(argv[: len(_PROVISION)]) == _PROVISION
    ]
    assert provisioner_timeouts == [update_environment._PROVISIONER_TIMEOUT_SECONDS]


# --- recover: an apply killed inside `git merge` (real git) ---------------------


def _git_in(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo_left_mid_merge(tmp_path: Path, *, is_conflicting: bool) -> tuple[Path, str]:
    """A real repo left exactly as an apply killed inside ``git merge`` leaves one.

    ``git merge`` writes MERGE_HEAD before it resolves anything, so the merge is
    staged in the index while HEAD is still the rollback point. Returns the repo
    and that rollback point.
    """
    repo = _make_real_repo(tmp_path)
    (repo / "shared.txt").write_text("base\n")
    # As in a real workspace, where the marker this test writes lives under the
    # gitignored data/ tree rather than showing up as an uncommitted change.
    (repo / ".gitignore").write_text("data/\n")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "base")
    local_branch = _git_in(repo, "rev-parse", "--abbrev-ref", "HEAD")

    _git_in(repo, "checkout", "-q", "-b", "worker")
    upstream_file = "shared.txt" if is_conflicting else "from-upstream.txt"
    (repo / upstream_file).write_text("upstream\n")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "upstream change")

    _git_in(repo, "checkout", "-q", local_branch)
    (repo / "shared.txt").write_text("local\n")
    _git_in(repo, "commit", "-q", "-am", "local change")
    rollback_to = _head_sha(repo)

    # The kill: the merge is staged, no merge commit was ever created.
    subprocess.run(
        ["git", "merge", "--no-commit", "--no-ff", "worker"],
        cwd=repo,
        capture_output=True,
    )
    assert _git_in(repo, "rev-parse", "--verify", "--quiet", "MERGE_HEAD")
    assert _head_sha(repo) == rollback_to
    return repo, rollback_to


def _recover_boot_path(repo: Path) -> int:
    """``recover --no-restart``: the boot path, which touches only disk state."""
    return update_apply.recover(
        repo,
        if_stale=False,
        grace_seconds=600.0,
        no_restart=True,
        runner=update_runtime.Runner(),
        http=_FakeHttp(_all_healthy),
        sleeper=lambda _s: None,
        base_url=_LIVE_BASE,
    )


@pytest.mark.parametrize("is_conflicting", [False, True])
def test_recover_aborts_a_merge_killed_before_it_committed(
    tmp_path: Path, is_conflicting: bool
) -> None:
    """A staged-but-uncommitted merge must be aborted, never committed.

    Committing on top of one makes git turn that commit into *the merge commit*,
    so the rollback would land the very merge it exists to undo -- and under a
    subject ``_has_rollback_since`` then reads as "already rolled back", which
    refuses every retry. The conflicting variant cannot be committed at all, so
    without the abort recovery never makes progress.
    """
    repo, rollback_to = _repo_left_mid_merge(tmp_path, is_conflicting=is_conflicting)
    update_apply_contract.write_marker(
        update_apply_contract.ApplyMarker(
            dri_agent="the-lead",
            rollback_to=rollback_to,
            merge_ref="worker",
            target_ref=None,
            ff_only=False,
            worker_bundle=None,
            phase=update_apply_contract.PHASE_STARTED,
            pid=12345,
            started_at=1.0,
            updated_at=1.0,
        ),
        repo,
        now=lambda: 2.0,
    )

    assert _recover_boot_path(repo) == 0

    assert _head_sha(repo) == rollback_to
    assert (repo / "shared.txt").read_text() == "local\n"
    assert not (repo / "from-upstream.txt").exists()
    assert _git_in(repo, "status", "--porcelain") == ""
    # The merge is genuinely undone, not landed under the rollback's subject.
    assert (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "worker", "HEAD"], cwd=repo
        ).returncode
        != 0
    )
    assert not _marker_exists(repo)


def test_recover_with_nothing_to_restore_commits_nothing_over_an_untracked_file(
    tmp_path: Path,
) -> None:
    # An apply killed before its merge landed leaves the tree already at the
    # rollback point, so there is nothing to commit -- and an untracked file
    # (any stray file in the workspace) must not turn that into a failed
    # `git commit` that keeps the marker, and so re-fails recovery, every boot.
    repo = _make_real_repo(tmp_path)
    (repo / ".gitignore").write_text("data/\n")
    _git_in(repo, "add", "-A")
    _git_in(repo, "commit", "-q", "-m", "base")
    (repo / "stray-notes.txt").write_text("untracked\n")
    rollback_to = _head_sha(repo)
    commits_before = _commit_count(repo)
    update_apply_contract.write_marker(
        update_apply_contract.ApplyMarker(
            dri_agent="the-lead",
            rollback_to=rollback_to,
            merge_ref="worker",
            target_ref=None,
            ff_only=True,
            worker_bundle=None,
            phase=update_apply_contract.PHASE_STARTED,
            pid=12345,
            started_at=1.0,
            updated_at=1.0,
        ),
        repo,
        now=lambda: 2.0,
    )

    assert _recover_boot_path(repo) == 0

    assert not _marker_exists(repo)
    assert _commit_count(repo) == commits_before
    assert (repo / "stray-notes.txt").exists()


# --- surface-chat-tab ------------------------------------------------------


def test_wait_and_open_chat_tab_stops_at_the_first_success() -> None:
    # No client for the first two tries (the user is still on their way in),
    # then one takes it: the loop must stop there rather than keep re-opening.
    answers = iter([False, False, True])
    calls = 0

    def try_open() -> bool:
        nonlocal calls
        calls += 1
        return next(answers)

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    assert update_self.wait_and_open_chat_tab(
        try_open,
        deadline_seconds=60.0,
        retry_seconds=5.0,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    assert calls == 3


def test_wait_and_open_chat_tab_gives_up_at_the_deadline() -> None:
    calls = 0

    def try_open() -> bool:
        nonlocal calls
        calls += 1
        return False

    clock = [0.0]

    def sleep(seconds: float) -> None:
        clock[0] += seconds

    assert not update_self.wait_and_open_chat_tab(
        try_open,
        deadline_seconds=12.0,
        retry_seconds=5.0,
        monotonic=lambda: clock[0],
        sleep=sleep,
    )
    # Tried at t=0, 5, 10; the check after the third failure sees 10 < 12 and sleeps to 15, then stops.
    assert calls == 4


# --- run-status (the Minds app's status contract) --------------------------


def test_run_status_start_and_verdict_round_trip(tmp_path, monkeypatch) -> None:
    # The whole app-facing contract: `start` records who is running, `verdict`
    # lands the outcome on the same record, and the run's start survives the
    # verdict write (the app tells runs apart by their start).
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-abc123")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.chat_agent_name == "update-abc123"
    assert status.verdict is None
    started_at = status.started_at

    assert (
        update_self.main(
            [
                "run-status",
                "verdict",
                "UPDATED_WITH_REBUILD_ITEMS",
                "--resulting-ref",
                "minds-v0.4.0",
                "--detail",
                "Landed; one rebuild item left.",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.verdict == "UPDATED_WITH_REBUILD_ITEMS"
    assert status.resulting_ref == "minds-v0.4.0"
    assert status.detail == "Landed; one rebuild item left."
    assert status.verdict_at is not None
    assert status.started_at == started_at


def test_run_status_start_supersedes_the_previous_runs_verdict(
    tmp_path, monkeypatch
) -> None:
    # One file per workspace: a new run's start must overwrite the last run's
    # record outright, or the app would read the old verdict as the new run's.
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-old")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    assert (
        update_self.main(
            ["run-status", "verdict", "REFUSED", "--repo-root", str(tmp_path)]
        )
        == 0
    )
    assert (
        update_self.main(
            [
                "run-status",
                "start",
                "--chat",
                "update-new",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.chat_agent_name == "update-new"
    assert status.verdict is None
    assert status.detail == ""


def test_run_status_verdict_is_recorded_under_the_agent_recording_it(
    tmp_path, monkeypatch, capsys
) -> None:
    # A pass that started and then stopped (a foreign lease, say) leaves its own
    # start in the file. The running pass's verdict must not be filed under that
    # pass's name: the app matches a record to a workspace's row by chat name,
    # so it would discard the verdict and read the real run as having died.
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-aborted")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0

    monkeypatch.setenv("MNGR_AGENT_NAME", "update-real")
    assert (
        update_self.main(
            ["run-status", "verdict", "UPDATED", "--repo-root", str(tmp_path)]
        )
        == 0
    )

    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.chat_agent_name == "update-real"
    assert status.verdict == "UPDATED"
    assert "records update-aborted, not update-real" in capsys.readouterr().err


def test_run_status_verdict_without_a_start_still_records(
    tmp_path, monkeypatch
) -> None:
    # A pass that skipped its start (an older skill copy, an agent that missed
    # the step) still owes the app its outcome; the env names the agent.
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-late")
    assert (
        update_self.main(
            [
                "run-status",
                "verdict",
                "NEEDS_RECREATION",
                "--in-place-compatible-ref",
                "minds-v0.3.9",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.chat_agent_name == "update-late"
    assert status.verdict == "NEEDS_RECREATION"
    assert status.in_place_compatible_ref == "minds-v0.3.9"


def test_run_status_start_requires_an_agent_name(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 1
    assert "no chat agent name" in capsys.readouterr().err
    assert update_apply_contract.read_run_status(tmp_path) is None


def test_read_run_status_ignores_a_corrupt_file(tmp_path, capsys) -> None:
    # Same lenience as the marker: status reporting must never wedge the pass
    # that would overwrite the bad file.
    path = update_apply_contract.run_status_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json")
    assert update_apply_contract.read_run_status(tmp_path) is None
    assert "not a valid run status" in capsys.readouterr().err


def test_run_status_hold_and_resume_round_trip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-lead")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    assert (
        update_self.main(
            [
                "run-status",
                "hold",
                "--detail",
                "Your dashboard widget has no place in the new layout.",
                "--repo-root",
                str(tmp_path),
            ]
        )
        == 0
    )
    held = update_apply_contract.read_run_status(tmp_path)
    assert held is not None
    assert held.is_holding
    assert held.hold_detail == "Your dashboard widget has no place in the new layout."
    assert held.verdict is None

    assert update_self.main(["run-status", "resume", "--repo-root", str(tmp_path)]) == 0
    resumed = update_apply_contract.read_run_status(tmp_path)
    assert resumed is not None
    assert not resumed.is_holding
    assert resumed.hold_detail == ""
    assert resumed.chat_agent_name == "update-lead"


def test_run_status_delegate_names_the_worker_until_the_verdict(
    tmp_path, monkeypatch
) -> None:
    # The lead's chat sits idle while the worker runs, which the app would
    # otherwise read as "waiting for the user"; the record names the worker
    # so the app can look at it instead, and the verdict takes the name off.
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-lead")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    assert (
        update_self.main(
            ["run-status", "delegate", "update-self", "--repo-root", str(tmp_path)]
        )
        == 0
    )
    delegated = update_apply_contract.read_run_status(tmp_path)
    assert delegated is not None
    assert delegated.worker_agent_name == "update-self"
    assert delegated.chat_agent_name == "update-lead"
    assert delegated.verdict is None

    assert (
        update_self.main(
            ["run-status", "verdict", "UPDATED", "--repo-root", str(tmp_path)]
        )
        == 0
    )
    ended = update_apply_contract.read_run_status(tmp_path)
    assert ended is not None
    assert ended.worker_agent_name is None


def test_run_status_start_clears_the_previous_runs_worker(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-lead")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    assert (
        update_self.main(
            ["run-status", "delegate", "update-self", "--repo-root", str(tmp_path)]
        )
        == 0
    )
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.worker_agent_name is None


def test_run_status_verdict_clears_a_hold(tmp_path, monkeypatch) -> None:
    # Declining at the hold ends the pass with REFUSED; the record must not
    # keep saying the run is waiting for an answer it already got.
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-lead")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0
    assert update_self.main(["run-status", "hold", "--repo-root", str(tmp_path)]) == 0
    assert (
        update_self.main(
            ["run-status", "verdict", "REFUSED", "--repo-root", str(tmp_path)]
        )
        == 0
    )
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.verdict == "REFUSED"
    assert not status.is_holding


def test_the_marker_mirrors_the_apply_into_the_run_record(
    tmp_path, monkeypatch
) -> None:
    """The app reads the apply's liveness from run.json alone, so every marker
    restamp lands there and clearing the marker clears it."""
    monkeypatch.setenv("MNGR_AGENT_NAME", "update-lead")
    assert update_self.main(["run-status", "start", "--repo-root", str(tmp_path)]) == 0

    _plant_marker(tmp_path, phase=update_apply_contract.PHASE_MERGED, updated_at=2000.0)
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.apply_phase == update_apply_contract.PHASE_MERGED
    assert status.apply_updated_at == 2000.0
    assert status.chat_agent_name == "update-lead"

    _plant_marker(tmp_path, phase=update_apply_contract.PHASE_BUILT, updated_at=2100.0)
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.apply_phase == update_apply_contract.PHASE_BUILT
    assert status.apply_updated_at == 2100.0

    update_apply_contract.clear_marker(tmp_path)
    status = update_apply_contract.read_run_status(tmp_path)
    assert status is not None
    assert status.apply_phase is None
    assert status.apply_updated_at is None


def test_the_marker_does_not_invent_a_run_record(tmp_path) -> None:
    # An apply run by hand outside a pass has no run to report to; a record
    # conjured here would name no chat and lock the app's row for good.
    _plant_marker(tmp_path)
    assert update_apply_contract.read_run_status(tmp_path) is None
    update_apply_contract.clear_marker(tmp_path)
    assert update_apply_contract.read_run_status(tmp_path) is None


def test_a_failed_preflight_rejects_the_merge_before_the_bundle_is_touched(
    apply_repo: Path,
) -> None:
    """A combined frontend+backend release whose backend cannot boot is
    rejected while the live bundle is still the one that was serving, rather
    than after the build has rewritten it."""
    runner = _apply_runner(_FRONTEND_DIFF + _BACKEND_DIFF, apply_repo)
    spawner = _FakeSpawner(output="Traceback: ImportError boom", exited=True)

    def only_live_healthy(url: str) -> int | None:
        return 200 if _is_live(url) else None

    code = _apply(runner, _FakeHttp(only_live_healthy), spawner, apply_repo)

    assert code == 2
    assert spawner.spawns == [[update_layout.TOOL_NAME]]
    assert not runner.ran("npm", "run", "build")
    assert not runner.ran(*_RESTART)
    assert _bundle_exists(apply_repo)
