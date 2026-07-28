"""Unit tests for the deterministic update-self helpers.

Covers the pieces the flow relies on being exactly right: target-tag
resolution (latest stable, prereleases excluded, semver not lexical order), the
merged-vs-pulled-in classification, the path -> change-class mapping, and the
skill bootstrap that extracts the target ref's own copy of the flow.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("update_self.py")
_spec = importlib.util.spec_from_file_location("update_self", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
update_self = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(update_self)


# --- pick_latest_stable_tag / resolve_target -------------------------------


def test_pick_latest_stable_tag_ignores_prereleases() -> None:
    tags = [
        "minds-v0.3.5",
        "minds-v0.3.7",
        "minds-v0.3.7-rc1",
        "minds-v0.3.6",
    ]
    assert update_self.pick_latest_stable_tag(tags) == "minds-v0.3.7"


def test_pick_latest_stable_tag_uses_semver_not_lexical_order() -> None:
    # Lexically "0.3.9" > "0.3.10"; semantically 0.3.10 is newer.
    tags = ["minds-v0.3.9", "minds-v0.3.10", "minds-v0.4.0"]
    assert update_self.pick_latest_stable_tag(tags) == "minds-v0.4.0"
    tags_no_major = ["minds-v0.3.9", "minds-v0.3.10"]
    assert update_self.pick_latest_stable_tag(tags_no_major) == "minds-v0.3.10"


def test_pick_latest_stable_tag_returns_none_when_all_prerelease_or_empty() -> None:
    assert update_self.pick_latest_stable_tag([]) is None
    assert update_self.pick_latest_stable_tag(["minds-v0.3.7-rc1", "v1.2.3"]) is None


def test_resolve_target_defaults_to_latest_stable() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7", "minds-v0.3.7-rc1"]
    result = update_self.resolve_target(None, tags)
    assert result == update_self.ResolvedTarget("minds-v0.3.7", "tag")


def test_resolve_target_override_main_is_remote_qualified_branch() -> None:
    # Must resolve to the remote branch, not the stale local `main`.
    assert update_self.resolve_target("main", ["minds-v0.3.7"]) == (
        update_self.ResolvedTarget("upstream/main", "branch")
    )
    assert update_self.resolve_target(
        "main", ["minds-v0.3.7"], remote="official"
    ) == update_self.ResolvedTarget("official/main", "branch")


def test_resolve_target_override_known_tag_vs_arbitrary_ref() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.7"]
    assert update_self.resolve_target("minds-v0.3.6", tags).kind == "tag"
    # An override git can validate later but that is not a known tag/main.
    passthrough = update_self.resolve_target("abc1234", tags)
    assert passthrough == update_self.ResolvedTarget("abc1234", "ref")


def test_resolve_target_raises_when_no_stable_tag_and_no_override() -> None:
    try:
        update_self.resolve_target(None, ["minds-v0.3.7-rc1"])
    except ValueError as exc:
        assert "no stable minds-v* tag" in str(exc)
    else:
        raise AssertionError("expected ValueError when no stable tag and no override")


# --- the app-version ceiling -----------------------------------------------


def test_ceiling_caps_selection_at_the_app_version() -> None:
    # The headline case: upstream has moved past the app driving this workspace.
    tags = ["minds-v0.3.8", "minds-v0.3.9", "minds-v0.4.0", "minds-v0.4.1"]
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.9"
    )
    result = update_self.resolve_target(None, tags, ceiling="minds-v0.3.9")
    assert result.ref == "minds-v0.3.9"
    assert result.ceiling == "minds-v0.3.9"
    assert result.exceeds_ceiling is False


def test_ceiling_picks_the_newest_tag_below_it_when_the_exact_tag_is_absent() -> None:
    # The app's own tag need not exist upstream (a release whose template tag was
    # never cut); the newest tag below it is still safe to take.
    tags = ["minds-v0.3.8", "minds-v0.4.0"]
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.8"
    )


def test_ceiling_compares_by_semver_not_lexically() -> None:
    tags = ["minds-v0.3.9", "minds-v0.3.10"]
    # Lexically "0.3.10" < "0.3.9", so a lexical cap would wrongly admit 0.3.10.
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.3.9")
        == "minds-v0.3.9"
    )
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.3.10")
        == "minds-v0.3.10"
    )


def test_non_release_ceiling_imposes_no_cap() -> None:
    # A dev app reports its branch rather than a release tag; there is no version
    # to compare, so the flow behaves exactly as it did before the ceiling.
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    assert update_self.pick_latest_stable_tag(tags, ceiling="main") == "minds-v0.4.0"
    result = update_self.resolve_target(None, tags, ceiling="main")
    assert result.ref == "minds-v0.4.0"
    assert result.ceiling == "main"


def test_resolve_target_explains_when_every_tag_is_above_the_ceiling() -> None:
    # Distinct from "upstream has no stable tags at all": here the user's fix is
    # to update the app, so the message has to say so.
    try:
        update_self.resolve_target(None, ["minds-v0.4.0"], ceiling="minds-v0.3.9")
    except ValueError as exc:
        assert "newer than this workspace's minds app" in str(exc)
        assert "minds-v0.3.9" in str(exc)
    else:
        raise AssertionError("expected ValueError when every tag is above the ceiling")


def test_override_above_the_ceiling_is_flagged_but_not_blocked() -> None:
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    newer = update_self.resolve_target("minds-v0.4.0", tags, ceiling="minds-v0.3.9")
    assert newer.ref == "minds-v0.4.0"
    assert newer.exceeds_ceiling is True


def test_override_at_or_below_the_ceiling_is_not_flagged() -> None:
    tags = ["minds-v0.3.6", "minds-v0.3.9"]
    older = update_self.resolve_target("minds-v0.3.6", tags, ceiling="minds-v0.3.9")
    assert older.exceeds_ceiling is False
    at_ceiling = update_self.resolve_target(
        "minds-v0.3.9", tags, ceiling="minds-v0.3.9"
    )
    assert at_ceiling.exceeds_ceiling is False


def test_unprovable_overrides_are_flagged() -> None:
    # `main` and a bare commit carry no version, so the ceiling cannot vouch for
    # them; they must surface for confirmation rather than pass silently.
    tags = ["minds-v0.3.9"]
    assert (
        update_self.resolve_target("main", tags, ceiling="minds-v0.3.9").exceeds_ceiling
        is True
    )
    assert (
        update_self.resolve_target(
            "abc1234", tags, ceiling="minds-v0.3.9"
        ).exceeds_ceiling
        is True
    )
    # A prerelease, by contrast, *is* provable -- it carries a real version, and
    # 0.3.7-rc1 sits below the 0.3.9 ceiling -- so it is not flagged.
    assert (
        update_self.resolve_target(
            "minds-v0.3.7-rc1", tags, ceiling="minds-v0.3.9"
        ).exceeds_ceiling
        is False
    )


def test_overrides_are_never_flagged_without_a_ceiling() -> None:
    assert (
        update_self.resolve_target(
            "main", ["minds-v0.3.9"], ceiling=None
        ).exceeds_ceiling
        is False
    )


# --- fetch_app_template_ref ------------------------------------------------


def _install_fake_latchkey(
    monkeypatch, directory: Path, body: str, status: str, exit_code: int = 0
) -> None:
    """Put a stub ``latchkey`` on PATH that mimics the real curl passthrough.

    The real ``latchkey curl`` forwards its arguments to ``curl`` and passes
    curl's exit code, stdout and stderr back. The stub honors the two arguments
    the fetch depends on -- ``--output <file>`` for the body and ``--write-out
    %{http_code}`` for the status on stdout -- so the test exercises the actual
    subprocess call rather than a stand-in for it.
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


def _init_repo_with_tags(root: Path, *tags: str) -> None:
    """Init a git repo at ``root`` with one empty commit carrying ``tags``."""

    def _git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)

    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-q")
    _git("config", "user.email", "test@example.com")
    _git("config", "user.name", "test")
    _git("commit", "--allow-empty", "-q", "-m", "root")
    for tag in tags:
        _git("tag", tag)


def test_fetch_app_template_ref_returns_the_apps_pinned_ref(
    tmp_path, monkeypatch
) -> None:
    _install_fake_latchkey(
        monkeypatch,
        tmp_path,
        body='{"workspace_template_ref": "minds-v0.3.9"}',
        status="200",
    )

    assert update_self.fetch_app_template_ref() == "minds-v0.3.9"


def test_fetch_app_template_ref_blocks_when_the_gateway_denies_the_route(
    tmp_path, monkeypatch
) -> None:
    """A 403 is the *likelier* old-app signal and must get the same message as a 404.

    The route and the gateway grant that reaches it ship together, so an app old
    enough to lack the route is also old enough to lack the grant -- and the
    gateway denies before the app is ever asked. Landing this in the generic
    "returned HTTP {status}" branch would give the most common old-app case the
    least actionable wording.
    """
    _install_fake_latchkey(
        monkeypatch, tmp_path, body='{"error": "request not permitted"}', status="403"
    )

    try:
        update_self.fetch_app_template_ref()
    except update_self.CeilingUnavailableError as exc:
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
        update_self.fetch_app_template_ref()
    except update_self.CeilingUnavailableError as exc:
        assert "too old to report its version" in str(exc)
    else:
        raise AssertionError("expected a 404 to block rather than return no ceiling")


def test_fetch_app_template_ref_blocks_when_the_gateway_call_fails(
    tmp_path, monkeypatch
) -> None:
    _install_fake_latchkey(monkeypatch, tmp_path, body="", status="000", exit_code=7)

    try:
        update_self.fetch_app_template_ref()
    except update_self.CeilingUnavailableError as exc:
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
        update_self.fetch_app_template_ref()
    except update_self.CeilingUnavailableError as exc:
        assert "could not be parsed" in str(exc)
    else:
        raise AssertionError("expected an unparseable body to block")


def test_resolve_target_cli_reads_the_ceiling_from_the_app(
    tmp_path, monkeypatch, capsys
) -> None:
    """End to end: with no ``--ceiling``, the CLI asks the app and caps on the answer.

    ``latest_available`` reports the release that was held back, which is what the
    approval message tells the user about.
    """
    repo = tmp_path / "repo"
    _init_repo_with_tags(repo, "minds-v0.3.9", "minds-v0.4.0")
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
        "system/apps/system_interface/src/App.tsx": update_self.CLASS_SYSTEM_INTERFACE,
        "system/supervisord.conf": update_self.CLASS_SERVICE,
        "system/libs/bootstrap/src/bootstrap/main.py": update_self.CLASS_SERVICE,
        "system/vendor/mngr/libs/mngr/foo.py": update_self.CLASS_EDITABLE_TOOL,
        "system/scripts/forward_port.py": update_self.CLASS_SHARED_RUNTIME,
        ".agents/skills/update-self/SKILL.md": update_self.CLASS_SHARED_RUNTIME,
        "system/services/oom_priority/src/oom_priority/ledger.py": update_self.CLASS_SHARED_RUNTIME,
        # Provisioning files: pinned-toolchain scripts (would otherwise read as
        # shared_runtime under system/scripts/) and the .mngr/ create config (would
        # otherwise fall through to other) -- both need the provisioner reveal.
        "system/scripts/setup_system.sh": update_self.CLASS_PROVISIONER,
        "system/scripts/install_secret_scanners.sh": update_self.CLASS_PROVISIONER,
        "system/scripts/_provision_guard.sh": update_self.CLASS_PROVISIONER,
        ".mngr/settings.toml": update_self.CLASS_PROVISIONER,
        "system/Dockerfile": update_self.CLASS_DOCKERFILE,
        "CLAUDE.md": update_self.CLASS_DOCS,
        "changelog/some-entry.md": update_self.CLASS_DOCS,
        "system/config/parent.toml": update_self.CLASS_OTHER,
        # A README is docs even under a prefix with its own reveal class --
        # it must never trigger that class's reveal action (e.g. a service
        # restart for system/libs/bootstrap/README.md).
        "system/libs/bootstrap/README.md": update_self.CLASS_DOCS,
        "system/apps/system_interface/README.md": update_self.CLASS_DOCS,
        "system/vendor/mngr/README.md": update_self.CLASS_DOCS,
    }
    for path, expected in cases.items():
        assert update_self.classify_path(path).reveal_class == expected, path


def test_classify_path_project_mapping() -> None:
    assert (
        update_self.classify_path("system/apps/system_interface/foo.py").project
        == "system/apps/system_interface"
    )
    assert (
        update_self.classify_path("system/vendor/mngr/x.py").project
        == "system/vendor/mngr"
    )
    assert update_self.classify_path("system/scripts/forward_port.py").project == "."


def test_classify_path_manifest_flag() -> None:
    assert update_self.classify_path(
        "system/apps/system_interface/pyproject.toml"
    ).is_manifest
    assert update_self.classify_path(
        "system/vendor/mngr/libs/mngr/pyproject.toml"
    ).is_manifest
    assert not update_self.classify_path("system/scripts/forward_port.py").is_manifest


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
    result = update_self.classify_merge(upstream_changed, local_changed)

    merged_paths = [entry["path"] for entry in result.merged]
    pulled_paths = [entry["path"] for entry in result.pulled_in]
    assert merged_paths == ["system/apps/system_interface/src/App.tsx"]
    assert pulled_paths == ["system/scripts/forward_port.py", "system/supervisord.conf"]
    # A file only local changed is not surfaced as an upstream update at all.
    assert "PURPOSE.md" not in merged_paths + pulled_paths


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
    result = update_self.classify_merge(upstream_changed, local_changed)
    assert result.reveal_classes_merged == [
        update_self.CLASS_EDITABLE_TOOL,
        update_self.CLASS_SYSTEM_INTERFACE,
    ]
    assert result.reveal_classes_pulled_in == [update_self.CLASS_SHARED_RUNTIME]
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
    result = update_self.classify_merge(
        ["system/scripts/setup_system.sh", ".mngr/settings.toml"], []
    )
    assert result.reveal_classes_pulled_in == [update_self.CLASS_PROVISIONER]
    assert [entry["reveal_class"] for entry in result.pulled_in] == [
        update_self.CLASS_PROVISIONER,
        update_self.CLASS_PROVISIONER,
    ]


def test_classify_merge_empty() -> None:
    result = update_self.classify_merge([], [])
    assert result.merged == []
    assert result.pulled_in == []
    assert result.projects_to_validate == []


# --- CLI wiring --------------------------------------------------------------


def test_repo_root_flag_accepted_before_and_after_subcommand(tmp_path, capsys) -> None:
    # `--repo-root` must work both before and after the subcommand. Each
    # ordering has broken in its own way: a value after the subcommand errored
    # when the option lived only on the top parser, and a value *before* it was
    # silently clobbered back to cwd by the subparser's default on
    # Python < 3.13 (bpo-9351). Asserting on the resolved tag (which only
    # exists in the tmp repo) catches both -- a clobber would resolve against
    # the real repo and either fail or print a different ref.
    _init_repo_with_tags(tmp_path, "minds-v0.1.0")

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
    # Importing the script drops __pycache__/*.pyc into system/scripts/. Those are
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
        update_self.is_held_back_by_ceiling(
            resolved_ref="minds-v0.3.9",
            latest_available="minds-v0.4.0",
            ceiling="minds-v0.3.9",
            has_override=False,
        )
        is True
    )
    # Already on the newest release: nothing was held back.
    assert (
        update_self.is_held_back_by_ceiling(
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
        update_self.is_held_back_by_ceiling(
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
    `None` -- is the shape the CLI actually produces here. It reaches `False` by
    a different route than a `None` ceiling does: the branch parses to no
    version, so the selection was never bounded and `latest_available` is the
    resolved ref. Both routes are asserted.
    """
    assert (
        update_self.is_held_back_by_ceiling(
            resolved_ref="minds-v0.4.0",
            latest_available="minds-v0.4.0",
            ceiling="main",
            has_override=False,
        )
        is False
    )
    # No ceiling supplied at all -- only a direct caller does this.
    assert (
        update_self.is_held_back_by_ceiling(
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
    workspace on an rc app update arbitrarily far past it -- the exact outcome
    the ceiling exists to prevent, reached by the guard meant to enforce it.
    """
    tags = ["minds-v0.3.9", "minds-v0.4.0", "minds-v0.4.1"]
    # Semver: 0.4.0-rc1 precedes 0.4.0, so 0.4.0 itself is above this ceiling.
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.4.0-rc1")
        == "minds-v0.3.9"
    )
    result = update_self.resolve_target(None, tags, ceiling="minds-v0.4.0-rc1")
    assert result.ref == "minds-v0.3.9"
    assert result.ceiling == "minds-v0.4.0-rc1"


def test_a_prerelease_ceiling_still_admits_its_own_earlier_releases() -> None:
    tags = ["minds-v0.3.9", "minds-v0.4.0"]
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.4.1-rc1")
        == "minds-v0.4.0"
    )


def test_capping_by_a_prerelease_does_not_make_prereleases_selectable() -> None:
    # The ceiling widening to prereleases must not widen *candidate* selection:
    # the default target is still only ever a stable release.
    tags = ["minds-v0.3.9", "minds-v0.4.0-rc1", "minds-v0.4.0-rc2"]
    assert (
        update_self.pick_latest_stable_tag(tags, ceiling="minds-v0.4.0-rc2")
        == "minds-v0.3.9"
    )


def test_parse_version_orders_prereleases_semver_style() -> None:
    below = update_self.parse_version("minds-v0.4.0-rc1")
    above = update_self.parse_version("minds-v0.4.0")
    assert below is not None and above is not None
    # A prerelease sorts below the release it precedes.
    assert below < above
    # Numeric identifiers compare numerically, not lexically: rc.10 follows rc.2.
    rc2 = update_self.parse_version("minds-v0.4.0-rc.2")
    rc10 = update_self.parse_version("minds-v0.4.0-rc.10")
    assert rc2 is not None and rc10 is not None
    assert rc2 < rc10
    # A branch or bare commit has no version at all, and stays uncomparable.
    assert update_self.parse_version("main") is None
    assert update_self.parse_version("abc1234") is None
