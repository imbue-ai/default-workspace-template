import json
import subprocess
from pathlib import Path

from inline_snapshot import snapshot

from imbue.analytics.injected.workspace_feeds import append_collections_audit_record
from imbue.analytics.injected.workspace_feeds import missing_workspace_layout_detail
from imbue.analytics.injected.workspace_feeds import read_client_activity_feed
from imbue.analytics.injected.workspace_feeds import read_git_numstat_feed
from imbue.analytics.injected.workspace_feeds import read_jsonl_tail
from imbue.analytics.injected.workspace_feeds import read_registration_feed
from imbue.analytics.injected.workspace_feeds import read_transcript_feed
from imbue.analytics.injected.workspace_feeds import read_workspace_state_snapshot
from imbue.analytics.injected.workspace_feeds import write_readme_if_absent


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records))


def _event(event_id: str, event_type: str, source: str, **extra: object) -> dict:
    return {
        "timestamp": "2026-08-18T12:00:00.000000000Z",
        "event_id": event_id,
        "type": event_type,
        "source": source,
        **extra,
    }


def test_read_jsonl_tail_resumes_from_offset_and_leaves_partial_lines(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    first_line = json.dumps({"event_id": "evt-1"}) + "\n"
    second_line = json.dumps({"event_id": "evt-2"}) + "\n"
    events_file.write_text(first_line + second_line + '{"partial": tru')

    first_read = read_jsonl_tail(events_file, start_offset=0, budget_bytes=10_000)
    assert len(first_read.lines) == 2
    assert first_read.new_offset == len(first_line) + len(second_line)

    # Resuming from the returned offset re-reads nothing (the partial trailing
    # line stays unconsumed until it is completed).
    second_read = read_jsonl_tail(events_file, start_offset=first_read.new_offset, budget_bytes=10_000)
    assert second_read.lines == ()
    assert second_read.new_offset == first_read.new_offset


def test_read_jsonl_tail_resets_when_the_file_shrank_and_respects_the_budget(tmp_path: Path) -> None:
    events_file = tmp_path / "events.jsonl"
    line = json.dumps({"event_id": "evt-1"}) + "\n"
    events_file.write_text(line * 3)

    # An offset past EOF (the file was truncated/replaced) restarts from 0.
    reset_read = read_jsonl_tail(events_file, start_offset=10_000, budget_bytes=10_000)
    assert len(reset_read.lines) == 3

    # A budget that fits only one complete line consumes exactly that line.
    budget_read = read_jsonl_tail(events_file, start_offset=0, budget_bytes=len(line) + 3)
    assert len(budget_read.lines) == 1
    assert budget_read.new_offset == len(line)
    assert budget_read.read_bytes == len(line)

    # A budget too small for any complete line consumes nothing.
    tiny_read = read_jsonl_tail(events_file, start_offset=0, budget_bytes=3)
    assert tiny_read.lines == ()
    assert tiny_read.read_bytes == 0


def test_transcript_feed_annotates_agent_ids_and_advances_cursors(tmp_path: Path) -> None:
    host_dir = tmp_path / ".mngr"
    events_relpath = "agents/agent-abc/events/claude/common_transcript/events.jsonl"
    _write_jsonl(
        host_dir / events_relpath,
        [
            _event("evt-1", "user_message", "claude", content="hello"),
            _event("evt-2", "assistant_message", "claude", text="hi"),
        ],
    )

    first_output = read_transcript_feed(host_dir, cursor={}, budget_bytes=10_000)

    assert [record["event_id"] for record in first_output.records] == ["evt-1", "evt-2"]
    assert {record["agent_id"] for record in first_output.records} == {"agent-abc"}
    assert first_output.read_bytes > 0

    # Re-running with the advanced cursor yields nothing new.
    second_output = read_transcript_feed(host_dir, cursor=first_output.cursor, budget_bytes=10_000)
    assert second_output.records == ()


def test_client_activity_feed_drops_chat_text_at_the_source(tmp_path: Path) -> None:
    host_dir = tmp_path / ".mngr"
    _write_jsonl(
        host_dir / "agents/agent-abc/workspace_layout/events/client_activity/events.jsonl",
        [
            _event(
                "evt-msg",
                "message",
                "client_activity",
                client_id="client-1",
                message_text="secret sauce text",
                is_message_truncated=False,
            ),
            _event("evt-switch", "layout_switch", "client_activity", client_id="client-1"),
        ],
    )

    output = read_client_activity_feed(host_dir, cursor={}, budget_bytes=10_000)

    message_record = next(record for record in output.records if record["event_id"] == "evt-msg")
    assert "message_text" not in message_record
    assert message_record["message_text_length"] == len("secret sauce text")
    assert message_record["client_id"] == "client-1"


def test_registration_feed_reads_services_and_servers_paths(tmp_path: Path) -> None:
    host_dir = tmp_path / ".mngr"
    _write_jsonl(
        host_dir / "agents/agent-abc/events/services/events.jsonl",
        [_event("evt-svc", "service_registered", "services", name="terminal")],
    )
    _write_jsonl(
        host_dir / "agents/agent-abc/events/servers/events.jsonl",
        [_event("evt-srv", "server_registered", "servers", name="docs")],
    )

    services_output = read_registration_feed(host_dir, "services", cursor={}, budget_bytes=10_000)
    servers_output = read_registration_feed(host_dir, "servers", cursor={}, budget_bytes=10_000)

    assert [record["event_id"] for record in services_output.records] == ["evt-svc"]
    assert [record["event_id"] for record in servers_output.records] == ["evt-srv"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.test",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.test",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(repo),
        },
    )


def test_git_numstat_feed_emits_counts_only_and_resumes_from_the_cursor(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "a.txt").write_text("one\ntwo\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "sensitive commit message 48151")

    first_output = read_git_numstat_feed(repo, cursor={}, budget_bytes=10_000)

    assert len(first_output.records) == 1
    record = first_output.records[0]
    assert record["type"] == "git_commit"
    assert record["insertions"] == 2
    assert record["file_count"] == 1
    # No paths, no messages, no author identities.
    assert "a.txt" not in json.dumps(record)
    assert "sensitive commit message" not in json.dumps(record)
    assert "t@example.test" not in json.dumps(record)

    # Resume: nothing new until another commit lands.
    second_output = read_git_numstat_feed(repo, cursor=first_output.cursor, budget_bytes=10_000)
    assert second_output.records == ()

    (repo / "b.txt").write_text("three\n")
    _git(repo, "add", "b.txt")
    _git(repo, "commit", "--quiet", "-m", "second")
    third_output = read_git_numstat_feed(repo, cursor=first_output.cursor, budget_bytes=10_000)
    assert len(third_output.records) == 1
    assert third_output.records[0]["insertions"] == 1


def test_git_numstat_feed_falls_back_to_the_full_log_on_an_unknown_cursor(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "--quiet", "-m", "first")

    output = read_git_numstat_feed(repo, cursor={"last_sha": "f" * 40}, budget_bytes=10_000)

    assert len(output.records) == 1


def test_workspace_state_snapshot_reports_presence_booleans_and_names_only(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    host_dir = tmp_path / ".mngr"
    (workspace_root / "data" / ".secrets").mkdir(parents=True)
    (workspace_root / "data" / ".secrets" / "share.env").write_text("RELAY_TOKEN=super-secret-token-52396\n")
    (workspace_root / "data" / ".state").mkdir(parents=True)
    (workspace_root / "data" / ".state" / "apps.toml").write_text(
        '[[apps]]\nname = "terminal"\nurl = "http://localhost:7681"\nlabel = "terminal-x7k9q2w1"\n'
    )
    (workspace_root / "system" / "config").mkdir(parents=True)
    (workspace_root / "system" / "config" / "parent.toml").write_text(
        'url = "https://github.com/imbue-ai/default-workspace-template.git"\nbranch = "main"\n'
    )
    (host_dir / "agents" / "agent-1").mkdir(parents=True)
    (host_dir / "agents" / "agent-1" / "data.json").write_text(json.dumps({"type": "claude"}))

    output = read_workspace_state_snapshot(workspace_root, host_dir, run_id="run-1")

    assert len(output.records) == 1
    record = output.records[0]
    assert record["is_sharing_enabled"] is True
    assert record["is_owner_email_present"] is False
    assert record["installed_app_names"] == ["terminal"]
    assert record["agent_count"] == 1
    assert record["agent_type_counts"] == {"claude": 1}
    assert record["template_url"] == "https://github.com/imbue-ai/default-workspace-template.git"
    # This fixture has an agents dir but no git repo at the workspace root.
    assert record["is_workspace_repo_present"] is False
    assert record["is_host_agents_dir_present"] is True
    # Secret material and unguessable labels never enter the record.
    serialized = json.dumps(record)
    assert "super-secret-token" not in serialized
    assert "terminal-x7k9q2w1" not in serialized


def test_missing_workspace_layout_detail_fires_only_when_both_markers_are_absent(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    host_dir = tmp_path / ".mngr"

    # Neither marker exists: the hollow-collection case.
    hollow_detail = missing_workspace_layout_detail(workspace_root, host_dir)
    assert hollow_detail is not None
    assert str(workspace_root) in hollow_detail

    # Either marker alone means the layout is (at least partially) present.
    (host_dir / "agents").mkdir(parents=True)
    assert missing_workspace_layout_detail(workspace_root, host_dir) is None


def test_collections_audit_append_and_readme_seed(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"

    write_readme_if_absent(workspace_root)
    readme_path = workspace_root / "data" / ".imbue" / "analytics" / "README.md"
    assert "collect.py" in readme_path.read_text()
    readme_path.write_text("user-edited")
    write_readme_if_absent(workspace_root)
    assert readme_path.read_text() == "user-edited"

    append_collections_audit_record(
        workspace_root=workspace_root,
        run_id="run-1",
        script_version="abc123",
        record_count_by_source={"transcripts": 2},
        error_by_source={},
        read_bytes=512,
    )
    audit_lines = (workspace_root / "data" / ".imbue" / "analytics" / "collections.jsonl").read_text().splitlines()
    assert len(audit_lines) == 1
    audit_record = json.loads(audit_lines[0])
    assert audit_record["type"] == snapshot("collection_run")
    assert audit_record["record_count_by_source"] == {"transcripts": 2}
    assert audit_record["script_version"] == "abc123"
