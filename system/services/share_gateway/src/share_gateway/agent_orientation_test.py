import json
import subprocess
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest

from share_gateway.agent_orientation import build_changes_feed
from share_gateway.agent_orientation import build_skills_index
from share_gateway.agent_orientation import cached_skills_index
from share_gateway.agent_orientation import helper_source_url
from share_gateway.agent_orientation import render_instruction_block
from share_gateway.agent_orientation import render_primer
from share_gateway.agent_orientation import summarize_connected_accounts
from share_gateway.agent_orientation import template_commit
from share_gateway.agent_orientation import template_ref
from share_gateway.agent_orientation import template_source_ref
from share_gateway.caddyfile import RegisteredApp
from share_gateway.testing import commit_all
from share_gateway.testing import git_output
from share_gateway.testing import make_workspace_repo

_APPS = [
    RegisteredApp("owner-exec", "owner-exec-ex111111", "127.0.0.1", 8793, is_grantable=False),
    RegisteredApp(
        "chat",
        "chat-ch111111",
        "127.0.0.1",
        8010,
        is_grantable=True,
        description="The chats with the workspace's agents.",
        api_schema_path="/api/schema",
    ),
    RegisteredApp("ttyd", "ttyd-tt111111", "127.0.0.1", 7683, is_grantable=True),
]

_PRIMER_TEMPLATE = """# Visiting {{workspace_name}} ({{workspace_id}})

Scope: {{scope}}
<!-- exec-only:start -->
Exec paragraph.
<!-- exec-only:end -->
{{skills_table}}

{{apps_table}}

{{connected_accounts}}

{{changes}}
"""

_INVITE_TEMPLATE = """Join "{{workspace_name}}" ({{scope}}) at {{auth_origin}} with {{code}} before {{expires_at}} as "{{name_placeholder}}".
Install {{helper_source_url}} ({{helper_sha256}}).
<!-- exec-only:start -->
wsvisit run -- ls
<!-- exec-only:end -->
{{apps}}
"""


def test_skills_index_reads_frontmatter_and_hashes_every_tracked_file(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    index = build_skills_index(repo, "minds-v0.8.6")
    skills = index["skills"]
    assert isinstance(skills, list)
    assert [skill["name"] for skill in skills] == ["build-app", "notify-user"]
    build_app = skills[0]
    assert build_app["description"] == "Build an app, in quotes"
    assert build_app["path"] == ".agents/skills/build-app/SKILL.md"
    assert build_app["last_changed_at"]
    before = build_app["sha256"]
    (repo / ".agents" / "skills" / "build-app" / "scripts" / "run.py").write_text("print('changed')\n")
    commit_all(repo, "Change the script")
    changed_skills = build_skills_index(repo, "x")["skills"]
    assert isinstance(changed_skills, list) and changed_skills[0]["sha256"] != before
    assert index["template_ref"] == "minds-v0.8.6"


def test_cached_skills_index_is_keyed_by_head(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    cache = tmp_path / "state" / "skills_index.json"
    first = cached_skills_index(repo, cache, "ref")
    assert cache.exists()
    assert cached_skills_index(repo, cache, "ref") == first
    (repo / ".agents" / "skills" / "notify-user" / "SKILL.md").write_text(
        "---\nname: notify-user\ndescription: Changed.\n---\n"
    )
    commit_all(repo, "update-self: merge upstream")
    refreshed_skills = cached_skills_index(repo, cache, "ref")["skills"]
    assert isinstance(refreshed_skills, list) and refreshed_skills[1]["description"] == "Changed."


def test_a_corrupt_skills_index_cache_is_logged_and_rebuilt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    cache = tmp_path / "state" / "skills_index.json"
    cache.parent.mkdir()
    cache.write_text("{not json")
    index = cached_skills_index(repo, cache, "ref")
    skills = index["skills"]
    assert isinstance(skills, list) and [skill["name"] for skill in skills] == ["build-app", "notify-user"]
    assert json.loads(cache.read_text())["index"] == index
    assert f"Could not read the skills index cache at {cache}" in capsys.readouterr().err


def test_changes_feed_lists_commits_template_updates_and_touched_skills(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    (repo / ".agents" / "skills" / "notify-user" / "SKILL.md").write_text(
        "---\nname: notify-user\ndescription: Changed.\n---\n"
    )
    (repo / "README.md").write_text("hello\n")
    commit_all(repo, "update-self: merge upstream v0.8.7")
    feed = build_changes_feed(repo, datetime.now(timezone.utc) - timedelta(days=1))
    commits, template_updates = feed["commits"], feed["template_updates"]
    assert isinstance(commits, list) and isinstance(template_updates, list)
    assert [entry["subject"] for entry in commits] == [
        "update-self: merge upstream v0.8.7",
        "Initial workspace commit",
    ]
    assert commits[0]["paths"] == [".agents", "README.md"]
    assert [entry["subject"] for entry in template_updates] == ["update-self: merge upstream v0.8.7"]
    assert feed["skills_changed"] == ["build-app", "notify-user"]
    assert build_changes_feed(repo, datetime.now(timezone.utc) + timedelta(days=1))["commits"] == []


def test_template_ref_prefers_a_tag(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    assert len(template_ref(repo)) >= 7
    subprocess.run(["git", "tag", "minds-v0.9.0"], cwd=repo, check=True)
    assert template_ref(repo) == "minds-v0.9.0"


def test_a_failing_git_is_logged_and_the_orientation_degrades(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    not_a_checkout = tmp_path / "plain"
    not_a_checkout.mkdir()
    assert template_ref(not_a_checkout) == "unknown"
    assert build_changes_feed(not_a_checkout, datetime.now(timezone.utc) - timedelta(days=1))["commits"] == []
    logged = capsys.readouterr().err
    assert f"git log --first-parent --format=%H%x09%P%x09%s HEAD in {not_a_checkout} exited 128" in logged
    assert "git log" in logged


def test_a_missing_reference_file_is_logged_and_a_stub_rendered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    (repo / ".agents" / "shared" / "references" / "visiting-agents.md").unlink()
    (repo / ".agents" / "shared" / "references" / "visiting-agents-invite.md").unlink()
    index = build_skills_index(repo, "ref")
    changes = build_changes_feed(repo, datetime.now(timezone.utc) - timedelta(days=1))
    primer = render_primer(repo, "workspace", "Lab", "agent-1", index, _APPS, "alpha.example.com", "ACCOUNTS", changes)
    assert "The primer prose is missing from this workspace." in primer and "| build-app |" in primer
    expires = datetime(2026, 10, 15, 18, 15, tzinfo=timezone.utc)
    block = render_instruction_block(
        repo,
        "https://auth-x.alpha.example.com",
        "C",
        expires,
        "workspace",
        "Lab",
        "",
        [],
        helper_source_url="https://raw.example.com/v1/wsvisit.cjs",
        helper_sha256="f" * 64,
    )
    assert "wsvisit join https://auth-x.alpha.example.com C" in block
    logged = capsys.readouterr().err
    assert "Could not read the primer prose at" in logged and "Could not read the instruction block at" in logged


def test_render_primer_fills_every_placeholder_and_cuts_exec_text_for_app_scope(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    index = build_skills_index(repo, "ref")
    changes = build_changes_feed(repo, datetime.now(timezone.utc) - timedelta(days=1))
    full = render_primer(
        repo, "workspace", "Research Lab", "agent-1", index, _APPS, "alpha.example.com", "ACCOUNTS", changes
    )
    assert "Visiting Research Lab (agent-1)" in full
    assert "Exec paragraph." in full
    assert "| build-app | Build an app, in quotes | .agents/skills/build-app/SKILL.md |" in full
    assert (
        "| chat | The chats with the workspace's agents. | http://127.0.0.1:8010 | "
        "https://chat-ch111111.alpha.example.com | https://chat-ch111111.alpha.example.com/api/schema |"
    ) in full
    assert (
        "| ttyd | (no description) | http://127.0.0.1:7683 | https://ttyd-tt111111.alpha.example.com | (none) |"
        in full
    )
    assert "owner-exec" not in full
    assert "ACCOUNTS" in full and "Initial workspace commit" in full and "{{" not in full
    assert "exec-only" not in full
    reduced = render_primer(
        repo, ["chat"], "Research Lab", "agent-1", index, _APPS, "alpha.example.com", "n/a", changes
    )
    assert "Exec paragraph." not in reduced
    assert "these apps only: chat" in reduced


def test_render_instruction_block_substitutes_the_invite_and_cuts_exec_lines_for_app_scope(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    expires = datetime(2026, 10, 15, 18, 15, tzinfo=timezone.utc)
    apps = [{"name": "chat", "origin": "https://chat-ch111111.alpha.example.com"}]
    block = render_instruction_block(
        repo,
        "https://auth-x.alpha.example.com",
        "7K3M-QX9A-2PFH",
        expires,
        "workspace",
        "Lab",
        "",
        apps,
        helper_source_url="https://raw.example.com/v1/wsvisit.cjs",
        helper_sha256="f" * 64,
    )
    assert (
        'Join "Lab" (the whole workspace) at https://auth-x.alpha.example.com with 7K3M-QX9A-2PFH before 18:15 UTC'
        in block
    )
    assert f"Install https://raw.example.com/v1/wsvisit.cjs ({'f' * 64})." in block
    assert "<a short name for yourself>" in block and "wsvisit run -- ls" in block
    assert "exec-only" not in block
    assert "- chat: https://chat-ch111111.alpha.example.com" in block
    reduced = render_instruction_block(
        repo,
        "https://auth-x.alpha.example.com",
        "C",
        expires,
        ["chat"],
        "Lab",
        "Ada",
        apps,
        helper_source_url="https://raw.example.com/v1/wsvisit.cjs",
        helper_sha256="f" * 64,
    )
    assert "wsvisit run" not in reduced and 'as "Ada"' in reduced and "these apps: chat" in reduced


def test_the_helper_source_is_the_template_repository_at_the_exact_tag_or_else_the_full_commit(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    commit = git_output(repo, "rev-parse", "HEAD")
    assert template_source_ref(repo) == commit and len(commit) == 40
    assert helper_source_url(repo) == (
        "https://raw.githubusercontent.com/imbue-ai/default-workspace-template/"
        f"{commit}/system/services/share_gateway/src/share_gateway/helper/wsvisit.cjs"
    )
    git_output(repo, "tag", "v9.9.9")
    assert template_source_ref(repo) == "v9.9.9"
    assert helper_source_url(repo).startswith("https://raw.githubusercontent.com/imbue-ai/default-workspace-template/v9.9.9/")
    # Work the workspace does after its initial commit does not move the template commit.
    (repo / "later.txt").write_text("x\n")
    commit_all(repo, "A commit after the tag")
    assert template_source_ref(repo) == "v9.9.9"
    assert template_source_ref(tmp_path / "not-a-repo") == "main"


def test_the_template_commit_is_the_one_the_workspace_was_created_from_or_last_updated_to(tmp_path: Path) -> None:
    repo = make_workspace_repo(tmp_path, _PRIMER_TEMPLATE, _INVITE_TEMPLATE)
    # The fixture's only commit is the initial one, so with no template beneath it, it is the template.
    first = git_output(repo, "rev-parse", "HEAD")
    assert template_commit(repo) == first
    # A workspace made from a template: bootstrap's initial commit sits on the template's commit.
    (repo / "template.txt").write_text("from the template\n")
    commit_all(repo, "Template work")
    template = git_output(repo, "rev-parse", "HEAD")
    git_output(repo, "tag", "v2.0.0")
    (repo / "data.txt").write_text("the workspace's own\n")
    commit_all(repo, "Initial workspace commit")
    (repo / "more.txt").write_text("later work\n")
    commit_all(repo, "Later work in the workspace")
    assert template_commit(repo) == template
    assert template_source_ref(repo) == "v2.0.0"
    assert template_ref(repo) == "v2.0.0"
    assert helper_source_url(repo).startswith("https://raw.githubusercontent.com/imbue-ai/default-workspace-template/v2.0.0/")
    # An update-self merge names the upstream commit it merged as its second parent.
    git_output(repo, "checkout", "-q", "-b", "upstream", template)
    (repo / "upstream.txt").write_text("newer template\n")
    commit_all(repo, "Newer template work")
    upstream = git_output(repo, "rev-parse", "HEAD")
    git_output(repo, "checkout", "-q", "main")
    git_output(repo, "merge", "-q", "--no-ff", "-m", "update-self: merge upstream template", "upstream")
    assert template_commit(repo) == upstream
    assert template_source_ref(repo) == upstream
    assert template_ref(repo).startswith("v2.0.0-1-g")


def test_summarize_connected_accounts_without_a_gateway_says_so() -> None:
    assert "no latchkey gateway" in summarize_connected_accounts({})
