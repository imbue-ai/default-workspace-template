"""Unit tests for the deterministic migrate-workspace helpers.

Covers the pieces the flow rests on being exactly right: source-layout
detection, the legacy path map (including the prefixes that are genuinely
ambiguous and must NOT be silently resolved), reference rewriting, template-base
resolution, branch merged/unmerged classification, agent-to-session resolution,
the recreate argv and its labels, port reconciliation, and the audit patterns.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).with_name("migrate_workspace.py")
_spec = importlib.util.spec_from_file_location("migrate_workspace", _MODULE_PATH)
assert _spec is not None and _spec.loader is not None
migrate_workspace = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migrate_workspace)


# --- detect_layout ---------------------------------------------------------


def test_detect_layout_recognizes_the_current_layout() -> None:
    roots = migrate_workspace.detect_layout(
        ["/home/user/workspace/system", "/home/user/workspace/data"]
    )
    assert roots.layout == migrate_workspace.LAYOUT_CURRENT
    assert roots.repo_root == "/home/user/workspace"
    assert roots.host_dir == "/home/user/.mngr"
    assert roots.worktrees_dir == "/home/user/worktrees"
    assert roots.reason


def test_detect_layout_recognizes_a_pre_declutter_source() -> None:
    roots = migrate_workspace.detect_layout(
        ["/mngr/code/runtime", "/mngr/code/supervisord.conf"]
    )
    assert roots.layout == migrate_workspace.LAYOUT_PRE_DECLUTTER
    assert roots.repo_root == "/mngr/code"
    assert roots.host_dir == "/mngr"
    assert roots.worktrees_dir == "/mngr/worktree"


def test_detect_layout_needs_only_one_pre_declutter_marker() -> None:
    # A workspace whose runtime/ was never written still has the root
    # supervisord.conf, and vice versa; either alone identifies the layout.
    assert (
        migrate_workspace.detect_layout(["/mngr/code/runtime"]).layout
        == migrate_workspace.LAYOUT_PRE_DECLUTTER
    )
    assert (
        migrate_workspace.detect_layout(["/mngr/code/supervisord.conf"]).layout
        == migrate_workspace.LAYOUT_PRE_DECLUTTER
    )


def test_detect_layout_refuses_to_guess_on_mixed_or_absent_evidence() -> None:
    mixed = migrate_workspace.detect_layout(
        ["/home/user/workspace/system", "/mngr/code/runtime"]
    )
    assert mixed.layout == migrate_workspace.LAYOUT_UNKNOWN
    assert "both layouts" in mixed.reason

    absent = migrate_workspace.detect_layout([])
    assert absent.layout == migrate_workspace.LAYOUT_UNKNOWN
    assert absent.reason


# --- map_legacy_path -------------------------------------------------------


def test_map_legacy_path_maps_the_state_tree_and_moved_root_files() -> None:
    cases = {
        "runtime/memory/note.md": "data/memories/note.md",
        "runtime/tickets/abc.json": "data/.tickets/abc.json",
        "runtime/oom_priority/events/shed.jsonl": "data/.state/oom_priority/events/shed.jsonl",
        "runtime/applications.toml": "data/.state/apps.toml",
        "runtime/backup.toml": "data/system/backup.toml",
        "runtime/secrets/restic.env": "data/.secrets/restic.env",
        "uploads/photo.png": "data/uploads/photo.png",
        "github_sync.toml": "data/system/github_sync.toml",
        "parent.toml": "system/config/parent.toml",
        "skills-lock.json": ".agents/skills-lock.json",
        "VERSION_HISTORY.md": "docs/VERSION_HISTORY.md",
        "supervisord.conf": "system/supervisord.conf",
        "scripts/forward_port.py": "system/scripts/forward_port.py",
        "vendor/mngr/pyproject.toml": "system/vendor/mngr/pyproject.toml",
        "apps/system_interface/pyproject.toml": "system/apps/system_interface/pyproject.toml",
    }
    for old, expected in cases.items():
        mapping = migrate_workspace.map_legacy_path(old)
        assert mapping.new_path == expected, old
        assert not mapping.is_ambiguous, old


def test_map_legacy_path_splits_the_builtin_packages_three_ways() -> None:
    assert migrate_workspace.map_legacy_path("libs/host_backup/README.md").new_path == (
        "system/services/host_backup/README.md"
    )
    assert migrate_workspace.map_legacy_path("libs/bootstrap/README.md").new_path == (
        "system/libs/bootstrap/README.md"
    )
    assert migrate_workspace.map_legacy_path("libs/browser/README.md").new_path == (
        "system/apps/browser/README.md"
    )


def test_map_legacy_path_prefers_the_longest_prefix() -> None:
    # `runtime/memory/` must beat the bare `runtime/` fallback, and
    # `libs/bootstrap/` must beat the bare `libs/` one -- otherwise both would
    # come back ambiguous.
    assert not migrate_workspace.map_legacy_path("runtime/memory/a.md").is_ambiguous
    assert not migrate_workspace.map_legacy_path("libs/bootstrap/a.py").is_ambiguous
    assert migrate_workspace.map_legacy_path("dev/changelog/x.md").new_path == (
        "system/changelog/x.md"
    )


def test_map_legacy_path_flags_the_overloaded_prefixes_as_ambiguous() -> None:
    user_app = migrate_workspace.map_legacy_path("libs/email_triage/runner.py")
    assert user_app.is_ambiguous
    assert user_app.new_path == "system/apps/email_triage/runner.py"
    assert user_app.alternatives == (
        "system/apps/email_triage/runner.py",
        "system/services/email_triage/runner.py",
        "system/libs/email_triage/runner.py",
    )

    app_data = migrate_workspace.map_legacy_path("runtime/email_triage/latest.json")
    assert app_data.is_ambiguous
    assert app_data.alternatives == (
        "data/.apps/email_triage/latest.json",
        "data/.skills/email_triage/latest.json",
        "data/.state/email_triage/latest.json",
    )


def test_map_legacy_path_leaves_current_layout_paths_alone() -> None:
    for path in (
        ".agents/skills/welcome/SKILL.md",
        "system/apps/browser/README.md",
        "data/memories/note.md",
    ):
        mapping = migrate_workspace.map_legacy_path(path)
        assert mapping.new_path == path
        assert mapping.rule == ""
        assert not mapping.is_ambiguous


def test_map_legacy_path_normalizes_without_eating_a_leading_dot() -> None:
    # A character-wise lstrip("./") would turn .agents/... into agents/...
    assert migrate_workspace.map_legacy_path("./runtime/memory/a.md").new_path == (
        "data/memories/a.md"
    )
    assert migrate_workspace.map_legacy_path("./.agents/skills-lock.json").new_path == (
        ".agents/skills-lock.json"
    )


# --- rewrite_legacy_references ---------------------------------------------


def test_rewrite_legacy_references_rewrites_absolute_roots_longest_first() -> None:
    text = "cat /mngr/code/runtime/oom_priority/events/shed.jsonl\nls /mngr/agents\n"
    rewritten, substitutions = migrate_workspace.rewrite_legacy_references(text)
    assert (
        "/home/user/workspace/data/.state/oom_priority/events/shed.jsonl" in rewritten
    )
    assert "/home/user/.mngr/agents" in rewritten
    assert "/mngr" not in rewritten
    assert substitutions


def test_rewrite_legacy_references_rewrites_the_old_safety_net_symlinks() -> None:
    rewritten, _ = migrate_workspace.rewrite_legacy_references(
        "cd /code && ls /worktree/x\n"
    )
    assert rewritten == "cd /home/user/workspace && ls /home/user/worktrees/x\n"


def test_rewrite_legacy_references_keeps_directory_prefixes_intact() -> None:
    rewritten, _ = migrate_workspace.rewrite_legacy_references(
        "python3 scripts/layout.py open\nDATA_DIR = Path('runtime/memory')\n"
    )
    assert "python3 system/scripts/layout.py open" in rewritten
    # A multi-segment legacy dir is rewritten with or without a trailing slash,
    # since code most often names it without one.
    assert "Path('data/memories')" in rewritten


def test_rewrite_legacy_references_rewrites_root_qualified_relative_moves() -> None:
    # The boundary rule that stops `runtime/` matching inside `data/runtime/`
    # also stops it matching right after a repo root, so the root-qualified form
    # needs its own rule -- without it this would stop at
    # /home/user/workspace/runtime/tickets.
    rewritten, _ = migrate_workspace.rewrite_legacy_references(
        "TICKETS_DIR=/mngr/code/runtime/tickets\n"
    )
    assert rewritten == "TICKETS_DIR=/home/user/workspace/data/.tickets\n"


def test_rewrite_legacy_references_leaves_single_segment_words_in_prose_alone() -> None:
    # A bare-word rule from `changelog/` or `scripts/` would fire on ordinary
    # English; only the slash-terminated form may.
    rewritten, substitutions = migrate_workspace.rewrite_legacy_references(
        "Add a changelog entry, then run the scripts and check the uploads.\n"
    )
    assert substitutions == []
    assert rewritten.startswith("Add a changelog entry")


def test_rewrite_legacy_references_does_not_match_mid_token_or_already_current_paths() -> (
    None
):
    text = "myruntime/x\n/coder/bin\ndata/runtime/x\n/home/user/workspace/system/scripts/a.py\n"
    rewritten, substitutions = migrate_workspace.rewrite_legacy_references(text)
    assert rewritten == text
    assert substitutions == []


def test_rewrite_legacy_references_reports_every_substitution_with_its_line() -> None:
    _, substitutions = migrate_workspace.rewrite_legacy_references(
        "line one is clean\nls /mngr/code\n"
    )
    assert [(sub.line_number, sub.old_text, sub.new_text) for sub in substitutions] == [
        (2, "/mngr/code", "/home/user/workspace")
    ]


def test_rewrite_legacy_references_leaves_ambiguous_prefixes_for_the_agent() -> None:
    text = "from libs.email_triage import run\nopen('runtime/email_triage/x.json')\n"
    rewritten, _ = migrate_workspace.rewrite_legacy_references(text)
    assert "runtime/email_triage" in rewritten


# --- find_template_base ----------------------------------------------------


def test_find_template_base_takes_the_newest_marker() -> None:
    log = [
        "aaa1111 Add the email triage app",
        "bbb2222 update-self: merge minds-v0.3.9",
        "ccc3333 Tweak the welcome skill",
        "ddd4444 update-self: merge minds-v0.3.6",
        "eee5555 Initial workspace commit",
    ]
    # The NEWEST marker is the template state the source last updated to, so the
    # diff against it is exactly the user's own work since then. (update-self's
    # origin-line walk takes the OLDEST from the same markers.)
    assert migrate_workspace.find_template_base(log) == "bbb2222"


def test_find_template_base_accepts_a_bootstrap_only_history() -> None:
    log = ["aaa1111 Build a dashboard", "bbb2222 Initial workspace commit"]
    assert migrate_workspace.find_template_base(log) == "bbb2222"


def test_find_template_base_returns_none_without_a_marker() -> None:
    assert migrate_workspace.find_template_base([]) is None
    assert (
        migrate_workspace.find_template_base(["aaa1111 Initial commit", "", "  "])
        is None
    )


def test_find_template_base_ignores_a_marker_that_is_not_the_subject_prefix() -> None:
    # A commit merely *mentioning* update-self is not a template-state marker;
    # only the `update-self:` subject prefix is.
    log = ["aaa1111 Fix the update-self skill's conflict triage"]
    assert migrate_workspace.find_template_base(log) is None


# --- parse_baseline_diff ---------------------------------------------------


def test_parse_baseline_diff_maps_paths_only_for_a_pre_declutter_source() -> None:
    lines = ["A\truntime/memory/a.md", "M\tCLAUDE.md", "R100\told.py\tlibs/dash/new.py"]
    pre = migrate_workspace.parse_baseline_diff(
        lines, migrate_workspace.LAYOUT_PRE_DECLUTTER
    )
    assert [(entry.status, entry.path, entry.mapped_path) for entry in pre] == [
        ("A", "runtime/memory/a.md", "data/memories/a.md"),
        ("M", "CLAUDE.md", "CLAUDE.md"),
        # A rename is reported at its new path -- that is where the content is.
        ("R100", "libs/dash/new.py", "system/apps/dash/new.py"),
    ]
    assert pre[2].is_ambiguous

    current = migrate_workspace.parse_baseline_diff(
        lines, migrate_workspace.LAYOUT_CURRENT
    )
    assert [entry.mapped_path for entry in current] == [
        "runtime/memory/a.md",
        "CLAUDE.md",
        "libs/dash/new.py",
    ]


# --- classify_branches -----------------------------------------------------


def test_classify_branches_splits_merged_from_unmerged() -> None:
    refs = [
        "aaa1111 mngr/add-dashboard",
        "bbb2222 mngr/fix-triage",
        "ccc3333 mngr/workspace-main",
    ]
    merged = ["  mngr/add-dashboard", "* mngr/workspace-main"]
    classification = migrate_workspace.classify_branches(
        refs, merged, "mngr/workspace-main"
    )
    assert classification.merged == [{"branch": "mngr/add-dashboard", "tip": "aaa1111"}]
    # Unmerged branches carry work the migrated tree does not contain.
    assert classification.unmerged == [{"branch": "mngr/fix-triage", "tip": "bbb2222"}]


def test_classify_branches_excludes_the_checked_out_branch() -> None:
    classification = migrate_workspace.classify_branches(
        ["aaa1111 mngr/workspace-main"],
        ["* mngr/workspace-main"],
        "mngr/workspace-main",
    )
    assert classification.merged == []
    assert classification.unmerged == []


def test_classify_branches_tolerates_worktree_and_blank_ref_lines() -> None:
    # `git branch --merged` marks a branch checked out in another worktree with
    # `+`, and both listings can carry blank lines.
    classification = migrate_workspace.classify_branches(
        ["aaa1111 mngr/a", "", "bbb2222 mngr/b"], ["+ mngr/a", ""], "mngr/head"
    )
    assert classification.merged == [{"branch": "mngr/a", "tip": "aaa1111"}]
    assert classification.unmerged == [{"branch": "mngr/b", "tip": "bbb2222"}]


# --- agents ----------------------------------------------------------------


def test_is_excluded_agent_excludes_the_source_primary_by_label_or_name() -> None:
    assert migrate_workspace.is_excluded_agent("anything", {"is_primary": "true"})
    assert migrate_workspace.is_excluded_agent(
        migrate_workspace.EXCLUDED_AGENT_NAME, {}
    )
    assert migrate_workspace.is_excluded_agent("chat-1", {"user_created": "true"}) == ""


def test_resolve_agent_sessions_maps_history_ids_to_files_in_order() -> None:
    history = "sess-a startup\nsess-b clear\nsess-c compact\n"
    paths = [
        "/mngr/agents/x/plugin/claude/anthropic/projects/-mngr-code/sess-c.jsonl",
        "/mngr/agents/x/plugin/claude/anthropic/projects/-mngr-code/sess-a.jsonl",
    ]
    files, unresolved = migrate_workspace.resolve_agent_sessions(history, paths)
    # History order is preserved so the LAST --adopt is the most recent session,
    # which is the one mngr resumes on startup.
    assert files == (
        "/mngr/agents/x/plugin/claude/anthropic/projects/-mngr-code/sess-a.jsonl",
        "/mngr/agents/x/plugin/claude/anthropic/projects/-mngr-code/sess-c.jsonl",
    )
    assert unresolved == ("sess-b",)


def test_resolve_agent_sessions_dedupes_repeated_ids() -> None:
    history = "sess-a startup\nsess-a resume\nsess-a resume\n"
    files, unresolved = migrate_workspace.resolve_agent_sessions(
        history, ["/p/sess-a.jsonl"]
    )
    assert files == ("/p/sess-a.jsonl",)
    assert unresolved == ()


def test_resolve_agent_sessions_handles_an_agent_that_never_ran() -> None:
    assert migrate_workspace.resolve_agent_sessions("", ["/p/sess-a.jsonl"]) == ((), ())
    assert migrate_workspace.resolve_agent_sessions("\n  \n", []) == ((), ())


def test_plan_agent_names_auto_suffixes_collisions() -> None:
    plan = migrate_workspace.plan_agent_names(
        ["dashboard", "dashboard-2"],
        [("id-1", "dashboard"), ("id-2", "triage"), ("id-3", "dashboard")],
    )
    assert plan["id-2"] == "triage"
    # Keyed by agent id, so two source agents sharing a name (one of them a
    # preserved agent whose name a later one reused) both get a distinct name
    # instead of collapsing into one entry.
    assert plan["id-1"] == "dashboard-3"
    assert plan["id-3"] == "dashboard-4"


def test_plan_agent_names_avoids_names_taken_earlier_in_the_same_plan() -> None:
    plan = migrate_workspace.plan_agent_names(
        [], [("id-1", "a"), ("id-2", "a"), ("id-3", "a")]
    )
    assert sorted(plan.values()) == ["a", "a-2", "a-3"]


def test_derive_recreate_labels_keeps_the_original_creation_band() -> None:
    assert migrate_workspace.derive_recreate_labels(
        {"agent_created": "true", "project": "dash", "is_primary": "false"}
    ) == ["agent_created=true", "project=dash"]
    assert migrate_workspace.derive_recreate_labels({"user_created": "true"}) == [
        "user_created=true"
    ]
    # No creation label at all (an early or hand-made agent) defaults to
    # user_created, so the recreated chat is not shed ahead of workers.
    assert migrate_workspace.derive_recreate_labels({"project": "dash"}) == [
        "user_created=true",
        "project=dash",
    ]


def test_build_recreate_argv_adopts_every_session_and_stays_dormant_capable() -> None:
    argv = migrate_workspace.build_recreate_argv(
        "dashboard", ["/s/a.jsonl", "/s/b.jsonl"], {"user_created": "true"}
    )
    assert argv[:3] == ["mngr", "create", "dashboard"]
    assert argv[argv.index("--template") + 1] == "chat"
    assert argv[argv.index("--transfer") + 1] == "none"
    assert "--no-connect" in argv
    adopted = [argv[i + 1] for i, token in enumerate(argv) if token == "--adopt"]
    assert adopted == ["/s/a.jsonl", "/s/b.jsonl"]
    assert argv[argv.index("--label") + 1] == "user_created=true"
    # A fresh id is minted deliberately: the source's id may still be live there.
    assert "--id" not in argv


# --- ports -----------------------------------------------------------------

_SUPERVISORD_SNIPPET = """
[program:dashboard]
command=python3 system/scripts/oom_tag_service.py user bash -c "python3 system/scripts/forward_port.py --url http://localhost:8091 --name dashboard && uv run dashboard"

[program:cron]
command=cron -f
"""


def test_parse_supervisord_ports_reads_the_forward_port_call() -> None:
    ports = migrate_workspace.parse_supervisord_ports(_SUPERVISORD_SNIPPET)
    assert [(port.name, port.port) for port in ports] == [("dashboard", 8091)]
    assert ports[0].found_in == "supervisord.conf"


def test_parse_apps_registry_accepts_both_registry_vintages() -> None:
    current = migrate_workspace.parse_apps_registry(
        '[[apps]]\nname = "dashboard"\nurl = "http://localhost:8091"\n'
    )
    legacy = migrate_workspace.parse_apps_registry(
        '[[applications]]\nname = "dashboard"\nurl = "http://localhost:8091"\n'
    )
    assert [(port.name, port.port) for port in current] == [("dashboard", 8091)]
    assert [(port.name, port.port) for port in legacy] == [("dashboard", 8091)]
    assert "applications" in legacy[0].found_in


def test_parse_apps_registry_skips_an_entry_with_no_parseable_port() -> None:
    assert (
        migrate_workspace.parse_apps_registry(
            '[[apps]]\nname = "x"\nurl = "unix:///tmp/x.sock"\n'
        )
        == []
    )


def test_reconcile_ports_never_auto_resolves_a_real_wiring_collision() -> None:
    source = [
        migrate_workspace.AppPort(
            "dashboard", 8091, "http://localhost:8091", "supervisord.conf"
        ),
        migrate_workspace.AppPort(
            "triage", 8092, "http://localhost:8092", "supervisord.conf"
        ),
        migrate_workspace.AppPort(
            "news", 8093, "http://localhost:8093", "supervisord.conf"
        ),
    ]
    local = [
        migrate_workspace.AppPort(
            "dashboard", 9000, "http://localhost:9000", "supervisord.conf"
        ),
        migrate_workspace.AppPort(
            "weather", 8092, "http://localhost:8092", "supervisord.conf"
        ),
    ]
    result = migrate_workspace.reconcile_ports(source, local)
    assert [entry["name"] for entry in result["name_collisions"]] == ["dashboard"]
    assert [entry["name"] for entry in result["port_collisions"]] == ["triage"]
    assert [entry["name"] for entry in result["free"]] == ["news"]
    assert result["port_collisions"][0]["collides_with"]["name"] == "weather"


# --- scheduled jobs --------------------------------------------------------


def test_parse_cron_entries_rewrites_paths_and_keeps_paused_lines() -> None:
    text = (
        "PATH=/usr/bin:/bin\n"
        "* * * * *   root   /mngr/code/scripts/with_agent_env.sh "
        "/mngr/code/scripts/run_job.sh news --every 1d >> /var/log/supervisor/news.log 2>&1\n"
        "# 30 9 * * 1   root   /mngr/code/scripts/weekly.sh\n"
        "\n"
    )
    entries = migrate_workspace.parse_cron_entries("news", text)
    assert len(entries) == 2
    assert "/home/user/workspace/system/scripts/run_job.sh" in entries[0].rewritten
    assert not entries[0].is_commented
    # A commented-out line is a paused job, not an absent one.
    assert entries[1].is_commented
    assert "/home/user/workspace/system/scripts/weekly.sh" in entries[1].rewritten
    assert all(entry.job_name == "news" for entry in entries)


# --- audit scanning --------------------------------------------------------


def test_scan_audit_finds_each_kind_of_call_site() -> None:
    files = {
        "system/apps/dash/runner.py": (
            "import litellm\n"
            "from claude_p import claude_p_task\n"
            "DATA = 'runtime/dash'\n"
        ),
        "system/scripts/fetch.sh": (
            "latchkey curl https://slack.com/api/conversations.list\n"
            "cat /mngr/code/uploads/x\n"
        ),
        ".agents/skills/dash/SKILL.md": "See the build-web-service skill and heal-artifact.\n",
    }
    findings = migrate_workspace.scan_audit(files)
    by_kind: dict[str, set[str]] = {}
    for finding in findings:
        by_kind.setdefault(finding.kind, set()).add(finding.path)
    assert by_kind["ai"] == {"system/apps/dash/runner.py"}
    assert by_kind["latchkey"] == {"system/scripts/fetch.sh"}
    assert by_kind["legacy-path"] == {
        "system/apps/dash/runner.py",
        "system/scripts/fetch.sh",
    }
    assert by_kind["retired-skill"] == {".agents/skills/dash/SKILL.md"}


def test_scan_audit_reports_one_finding_per_kind_per_line() -> None:
    # A line carrying two markers of the same kind is one finding, not two.
    findings = migrate_workspace.scan_audit(
        {"a.sh": "latchkey curl http://latchkey-self.invalid/permission-requests\n"},
        kinds=["latchkey"],
    )
    assert len(findings) == 1
    assert findings[0].line_number == 1


def test_scan_audit_respects_the_kind_filter() -> None:
    files = {"a.py": "import litellm\nDATA = 'runtime/x'\n"}
    assert {
        finding.kind for finding in migrate_workspace.scan_audit(files, kinds=["ai"])
    } == {"ai"}


def test_scan_audit_legacy_path_pattern_ignores_current_layout_paths() -> None:
    findings = migrate_workspace.scan_audit(
        {"a.py": "P = 'data/runtime/x'\nQ = 'data/uploads/y'\nR = 'myruntime/z'\n"},
        kinds=["legacy-path"],
    )
    assert findings == []


def test_scan_audit_flags_an_account_scoped_latchkey_call() -> None:
    findings = migrate_workspace.scan_audit(
        {
            "a.sh": "latchkey --account alice@example.com curl https://api.github.com/user\n"
        },
        kinds=["latchkey"],
    )
    assert len(findings) == 1


# --- ssh plumbing ----------------------------------------------------------


def test_build_ssh_argv_uses_batch_mode_and_the_brokered_key() -> None:
    argv = migrate_workspace.build_ssh_argv(
        "127.0.0.1", "root", 2222, "/tmp/mind_key", "git -C /mngr/code status"
    )
    assert argv[0] == "ssh"
    assert argv[argv.index("-i") + 1] == "/tmp/mind_key"
    assert argv[argv.index("-p") + 1] == "2222"
    # BatchMode keeps an expired grant from hanging on a password prompt.
    assert "BatchMode=yes" in argv
    assert argv[-3:] == ["root@127.0.0.1", "--", "git -C /mngr/code status"]


def test_split_file_stream_splits_batched_reads_and_omits_missing_files() -> None:
    sentinel = migrate_workspace._FILE_SENTINEL
    stream = f"{sentinel} /a\nline1\nline2\n{sentinel} /b\n"
    assert migrate_workspace._split_file_stream(stream) == {
        "/a": "line1\nline2",
        "/b": "",
    }
    assert migrate_workspace._split_file_stream("") == {}


def _simulate_shell_read(files: dict[str, str | None]) -> str:
    """Reproduce the stdout the batched read script produces on a real shell.

    ``files`` maps a path to its on-disk content, or ``None`` for a file that
    does not exist (the ``[ -f ]`` guard emits nothing). Content is written
    verbatim -- crucially including files with no trailing newline -- followed by
    the trailing ``echo`` the command appends.
    """
    sentinel = migrate_workspace._FILE_SENTINEL
    out: list[str] = []
    for path, content in files.items():
        if content is None:
            continue
        out.append(f"{sentinel} {path}\n")
        out.append(content)
        out.append("\n")  # the command's trailing `echo`
    return "".join(out)


def test_read_command_recovers_files_without_trailing_newline() -> None:
    # data.json-style content with no final newline must not swallow the next
    # file's sentinel line.
    files = {
        "/a/data.json": '{"id": "a"}',  # no trailing newline
        "/a/missing": None,
        "/b/data.json": '{"id": "b"}\n',  # trailing newline
        "/c/data.json": "line1\nline2",  # multi-line, no trailing newline
    }
    stream = _simulate_shell_read(files)
    recovered = migrate_workspace._split_file_stream(stream)
    # The point: a newline-less file no longer swallows the next file's sentinel,
    # so every present file is recovered and json-parses. The trailing `echo` may
    # leave at most one trailing newline, which is immaterial to json/grep callers.
    assert "/a/missing" not in recovered
    assert set(recovered) == {"/a/data.json", "/b/data.json", "/c/data.json"}
    assert recovered["/a/data.json"] == '{"id": "a"}'
    assert recovered["/c/data.json"] == "line1\nline2"
    assert recovered["/b/data.json"].rstrip("\n") == '{"id": "b"}'
    import json as _json

    assert {_json.loads(recovered[p])["id"] for p in ("/a/data.json", "/b/data.json")} == {
        "a",
        "b",
    }


def test_read_file_command_terminates_content_with_newline() -> None:
    command = migrate_workspace._read_file_command("/a/data.json")
    assert command.endswith("; echo; fi")
    assert "[ -f '/a/data.json' ]" in command


# --- CLI wiring ------------------------------------------------------------


def test_shared_options_survive_being_passed_before_the_subcommand() -> None:
    # A subparser re-applies its own defaults over the top-level parser's
    # namespace, so a concrete default on the shared parent would silently discard
    # a flag given before the subcommand -- writing checkpoints to the wrong
    # directory with no error. SUPPRESS plus _checkpoint_dir_value is what keeps
    # both positions working.
    parser = migrate_workspace.build_parser()

    before = parser.parse_args(
        ["--checkpoint-dir", "/tmp/BEFORE", "--refresh", "map-paths"]
    )
    assert migrate_workspace._checkpoint_dir_value(before) == Path("/tmp/BEFORE")
    assert getattr(before, "refresh", False) is True

    after = parser.parse_args(
        ["map-paths", "--checkpoint-dir", "/tmp/AFTER", "--refresh"]
    )
    assert migrate_workspace._checkpoint_dir_value(after) == Path("/tmp/AFTER")
    assert getattr(after, "refresh", False) is True

    neither = parser.parse_args(["map-paths"])
    assert migrate_workspace._checkpoint_dir_value(neither) == Path(
        migrate_workspace.DEFAULT_CHECKPOINT_DIR
    )
    assert getattr(neither, "refresh", False) is False
