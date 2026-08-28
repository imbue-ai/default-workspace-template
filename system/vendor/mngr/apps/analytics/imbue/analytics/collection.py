"""The in-workspace collection runner: SSH, injection, validation, lake writes.

One poll run syncs consent, enumerates online explorer workspaces, and
collects from each due workspace under a bounded thread pool: the runner
injects the current collection script (source of truth: this package's
``injected/`` modules) into ``data/.imbue/analytics/`` over SSH, executes it
with ``uv run --script``, validates its stdout as untrusted input (see
``protocol``), writes the validated rows straight into the lakes, and only
then advances the runner-owned cursors -- a cursor-write failure just causes
a re-collection deduped downstream by event id.

Trust model notes:

- The workspace (VM and container alike) is the USER'S space. A refused hop
  (removed authorized_keys entry, revoked key) is an audit row and a skip,
  never an error to fight.
- Workspace sshd host keys are rotated to user-generated keys at adoption,
  which the server never learns, so the runner cannot strictly pin them.
  Instead it records the presented key per (host, endpoint) and flags any
  change in the audit row (trust-on-first-use with change detection; the
  bake-time key from pool_hosts seeds the expectation when present).
- This module's own logs flow to the ops telemetry store, so no log line may
  ever carry record payloads, script stdout, or message text -- only ids,
  outcomes, counts, and durations (enforced by a project ratchet).
"""

import hashlib
import io
import json
import logging
import shlex
import uuid
from collections.abc import Callable
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Final

import paramiko
import psycopg2
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

import imbue.analytics.ops_db as ops_db
from imbue.analytics.consent import CollectableWorkspace
from imbue.analytics.consent import list_online_explorer_workspaces
from imbue.analytics.consent import read_explorer_accounts
from imbue.analytics.consent import sync_consent_ledger
from imbue.analytics.errors import CollectionError
from imbue.analytics.errors import LakeInsertError
from imbue.analytics.lake import METRICS_RAW_EVENTS_TABLE
from imbue.analytics.lake import TRANSCRIPTS_RAW_EVENTS_TABLE
from imbue.analytics.lake import insert_raw_records
from imbue.analytics.protocol import CollectedRecord
from imbue.analytics.protocol import ParsedCollectionOutput
from imbue.analytics.protocol import TRANSCRIPTS_SOURCE
from imbue.analytics.protocol import parse_collection_output
from imbue.analytics.settings import AnalyticsSettings
from imbue.analytics.settings import CollectionSettings

logger = logging.getLogger(__name__)

# Fixed workspace layout, per the default-workspace-template conventions.
WORKSPACE_ROOT: Final[str] = "/home/user/workspace"
WORKSPACE_HOST_DIR: Final[str] = "/home/user/.mngr"
WORKSPACE_ANALYTICS_DIR: Final[str] = f"{WORKSPACE_ROOT}/data/.imbue/analytics"

# Remote path (under the analytics dir) -> local file under this package's
# injected/ directory. The package skeleton makes ``imbue.analytics.injected``
# importable from the script's own directory inside its isolated environment.
_INJECTED_FILE_BY_REMOTE_RELPATH: Final[dict[str, str]] = {
    "collect.py": "collect.py",
    "imbue/__init__.py": "",
    "imbue/analytics/__init__.py": "",
    "imbue/analytics/injected/__init__.py": "",
    "imbue/analytics/injected/workspace_feeds.py": "workspace_feeds.py",
    "imbue/analytics/injected/workspace_redaction.py": "workspace_redaction.py",
}

_CURSORS_FILENAME: Final[str] = "cursors.json"

_SSH_CONNECT_TIMEOUT_SECONDS: Final[float] = 30.0
_VM_PROBE_TIMEOUT_SECONDS: Final[float] = 30.0

# Hard cap on how much script stdout one run will read; everything past it is
# discarded (the audit row records the truncation in its detail).
MAX_STDOUT_BYTES: Final[int] = 256 * 1024 * 1024
_STDOUT_CHUNK_BYTES: Final[int] = 1024 * 1024
_MAX_DETAIL_CHARS: Final[int] = 2000

# Ops-DB advisory lock key guarding against overlapping poll runs (a slow run
# crossing the next cron tick).
_COLLECTION_POLL_LOCK_KEY: Final[int] = 815321001

_CONTAINER_ENDPOINT: Final[str] = "container"
_VM_ENDPOINT: Final[str] = "vm"

OUTCOME_OK: Final[str] = "ok"
OUTCOME_SSH_REFUSED: Final[str] = "ssh_refused"
OUTCOME_TIMEOUT: Final[str] = "timeout"
OUTCOME_SCRIPT_FAILED: Final[str] = "script_failed"
OUTCOME_PROTOCOL_ERROR: Final[str] = "protocol_error"
OUTCOME_LAKE_ERROR: Final[str] = "lake_error"

# GNU timeout's exit status when it kills the command.
_TIMEOUT_EXIT_STATUS: Final[int] = 124


class SshCollectionResult(BaseModel):
    """Everything one workspace's SSH phase produced (no DB or lake access yet)."""

    model_config = ConfigDict(frozen=True)

    outcome: str = Field(description="ok / ssh_refused / timeout / script_failed / protocol_error")
    parsed: ParsedCollectionOutput | None = Field(description="Validated stdout, when the script ran")
    stdout_bytes: int = Field(description="Bytes of stdout read (post-truncation)")
    presented_container_key: str | None = Field(description="Container sshd key line presented this run")
    presented_vm_key: str | None = Field(description="VM sshd key line presented this run (when probed)")
    latchkey_record: dict[str, Any] | None = Field(description="Runner-built latchkey_state record, when present")
    detail: str = Field(description="Bounded failure/diagnostic detail for the audit row")


class WorkspaceCollectionOutcome(BaseModel):
    """One workspace's fully-processed collection attempt (the audit row's content)."""

    model_config = ConfigDict(frozen=True)

    run_id: str = Field(description="This attempt's unique id")
    outcome: str = Field(description="Final outcome including lake write failures")
    metrics_rows: int = Field(description="Rows committed to the metrics lake")
    transcript_rows: int = Field(description="Rows committed to the transcripts lake")
    dropped_lines: int = Field(description="Output lines the validator dropped")
    stdout_bytes: int = Field(description="Bytes of stdout read")
    is_host_key_changed: bool = Field(description="Whether a presented host key differed from the last seen")
    detail: str = Field(description="Bounded detail for the audit row")


class _RecordPresentedKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Accepts the presented host key and records it for TOFU change detection."""

    presented_key_line: str | None = None

    def missing_host_key(self, client: Any, hostname: str, key: Any) -> None:
        self.presented_key_line = f"{key.get_name()} {key.get_base64()}"


def load_injected_script_files() -> dict[str, str]:
    """The injected file set, keyed by remote path relative to the analytics dir."""
    injected_dir = Path(__file__).parent / "injected"
    files: dict[str, str] = {}
    for remote_relpath, local_name in _INJECTED_FILE_BY_REMOTE_RELPATH.items():
        files[remote_relpath] = (injected_dir / local_name).read_text() if local_name else ""
    return files


def compute_script_version(script_files: dict[str, str]) -> str:
    """Deterministic content hash over the injected file set (stamped on every row)."""
    digest = hashlib.sha256()
    for remote_relpath in sorted(script_files):
        digest.update(remote_relpath.encode())
        digest.update(b"\x00")
        digest.update(script_files[remote_relpath].encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def _open_ssh_client(
    address: str, port: int, user: str, key_pem: str, timeout_seconds: float
) -> tuple[Any, str | None]:
    """Connect with the pool key, accepting and recording the presented host key."""
    private_key = paramiko.Ed25519Key.from_private_key(io.StringIO(key_pem))
    client = paramiko.SSHClient()
    policy = _RecordPresentedKeyPolicy()
    client.set_missing_host_key_policy(policy)
    client.connect(
        hostname=address,
        port=port,
        username=user,
        pkey=private_key,
        timeout=timeout_seconds,
        look_for_keys=False,
        allow_agent=False,
    )
    return client, policy.presented_key_line


def _write_remote_files(client: Any, files_by_remote_relpath: dict[str, str]) -> None:
    """Write the injected files under the analytics dir via SFTP (dirs pre-made via exec)."""
    subdirectories = sorted(
        {str(Path(relpath).parent) for relpath in files_by_remote_relpath if str(Path(relpath).parent) != "."}
    )
    remote_directories = [
        WORKSPACE_ANALYTICS_DIR,
        *(f"{WORKSPACE_ANALYTICS_DIR}/{subdirectory}" for subdirectory in subdirectories),
    ]
    mkdir_command = " && ".join(f"mkdir -p {shlex.quote(directory)}" for directory in remote_directories)
    _stdin, stdout, stderr = client.exec_command(mkdir_command, timeout=_SSH_CONNECT_TIMEOUT_SECONDS)
    exit_status = stdout.channel.recv_exit_status()
    if exit_status != 0:
        raise paramiko.SSHException(f"mkdir for injection failed (exit {exit_status}): {stderr.read()[:200]!r}")
    sftp = client.open_sftp()
    try:
        for remote_relpath, content in files_by_remote_relpath.items():
            with sftp.open(f"{WORKSPACE_ANALYTICS_DIR}/{remote_relpath}", "w") as handle:
                handle.write(content)
    finally:
        sftp.close()


def _build_run_command(run_id: str, script_version: str, budget_bytes: int, timeout_seconds: int) -> str:
    """The remote invocation: GNU timeout bounds the run, uv resolves the script env."""
    arguments = [
        "uv",
        "run",
        "--script",
        f"{WORKSPACE_ANALYTICS_DIR}/collect.py",
        "--run-id",
        run_id,
        "--script-version",
        script_version,
        "--workspace-root",
        WORKSPACE_ROOT,
        "--host-dir",
        WORKSPACE_HOST_DIR,
        "--cursors-file",
        f"{WORKSPACE_ANALYTICS_DIR}/{_CURSORS_FILENAME}",
        "--budget-bytes",
        str(budget_bytes),
    ]
    quoted = " ".join(shlex.quote(argument) for argument in arguments)
    # Non-interactive SSH sessions get a minimal PATH; uv typically lives in
    # ~/.local/bin or /usr/local/bin in workspaces.
    return (
        f'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH" && '
        f"cd {shlex.quote(WORKSPACE_ROOT)} && timeout {timeout_seconds} {quoted}"
    )


def _read_bounded_stdout(stdout_file: Any) -> tuple[str, int, bool]:
    """Read the channel's stdout up to MAX_STDOUT_BYTES; returns (text, bytes, is_truncated)."""
    chunks: list[bytes] = []
    total_bytes = 0
    is_truncated = False
    for chunk in iter(lambda: stdout_file.read(_STDOUT_CHUNK_BYTES), b""):
        if total_bytes + len(chunk) > MAX_STDOUT_BYTES:
            chunks.append(chunk[: MAX_STDOUT_BYTES - total_bytes])
            total_bytes = MAX_STDOUT_BYTES
            is_truncated = True
            break
        chunks.append(chunk)
        total_bytes += len(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace"), total_bytes, is_truncated


def _read_stderr_tail(stderr_file: Any) -> str:
    """Drain the channel's stderr, keeping only the tail (where a traceback ends up).

    The script logs INFO diagnostics to stderr and uv prints environment
    resolution there, so the useful part of a failing run's stderr is the
    end, not the start. The rolling byte tail keeps memory bounded on
    arbitrarily large stderr (EOF is bounded by the remote GNU timeout).
    """
    tail = b""
    for chunk in iter(lambda: stderr_file.read(_STDOUT_CHUNK_BYTES), b""):
        # 4 bytes per char covers a lossy decode still yielding the full cap.
        tail = (tail + chunk)[-_MAX_DETAIL_CHARS * 4 :]
    return tail.decode("utf-8", errors="replace")[-_MAX_DETAIL_CHARS:]


def _probe_vm_latchkey_state(
    workspace: CollectableWorkspace, key_pem: str, run_id: str, collected_at: datetime
) -> tuple[dict[str, Any] | None, str | None, str]:
    """The optional VM hop: presence-level latchkey signals, only where a gateway exists.

    Returns (record or None, presented VM host key or None, detail). A refused
    or failed hop yields (None, None/key, note) -- independently revocable,
    never an error.
    """
    if workspace.ssh_port is None:
        return None, None, ""
    probe_command = (
        'd="$HOME/.latchkey";'
        ' if [ -d "$d" ]; then echo 1; else echo 0; fi;'
        ' if [ -f "$d/permissions.json" ]; then wc -c < "$d/permissions.json"; else echo -1; fi;'
        ' if [ -f "$d/credentials.json.enc" ]; then echo 1; else echo 0; fi'
    )
    try:
        client, presented_key = _open_ssh_client(
            workspace.vps_address,
            workspace.ssh_port,
            workspace.ssh_user,
            key_pem,
            _VM_PROBE_TIMEOUT_SECONDS,
        )
    except (paramiko.SSHException, OSError) as e:
        return None, None, f"vm hop refused: {str(e)[:200]}"
    try:
        _stdin, stdout, _stderr = client.exec_command(probe_command, timeout=_VM_PROBE_TIMEOUT_SECONDS)
        exit_status = stdout.channel.recv_exit_status()
        # Bounded read: three short numeric lines is all an honest VM prints.
        output_lines = stdout.read(4096).decode("utf-8", errors="replace").split("\n")
    except (paramiko.SSHException, OSError) as e:
        return None, presented_key, f"vm probe failed: {str(e)[:200]}"
    finally:
        client.close()
    if exit_status != 0 or len(output_lines) < 3:
        return None, presented_key, f"vm probe exited {exit_status}"
    is_gateway_dir_present = output_lines[0].strip() == "1"
    if not is_gateway_dir_present:
        # Desktop-anchored workspace: latchkey state lives on the user's
        # desktop; there is nothing to report.
        return None, presented_key, ""
    permissions_raw = output_lines[1].strip()
    record = {
        "timestamp": collected_at.isoformat(),
        "event_id": f"latchkey-state-{run_id}",
        "type": "latchkey_state",
        "source": "latchkey_state",
        "is_gateway_dir_present": True,
        "permissions_byte_count": int(permissions_raw) if permissions_raw.lstrip("-").isdigit() else -1,
        "is_credentials_store_present": output_lines[2].strip() == "1",
    }
    return record, presented_key, ""


def collect_over_ssh(
    workspace: CollectableWorkspace,
    key_pem: str,
    script_files: dict[str, str],
    script_version: str,
    cursors_json_text: str,
    run_id: str,
    workspace_timeout_seconds: int,
    budget_bytes: int,
) -> SshCollectionResult:
    """One workspace's SSH phase: inject, execute, read, validate. Never raises."""
    collected_at = datetime.now(timezone.utc)
    try:
        client, presented_container_key = _open_ssh_client(
            workspace.vps_address,
            workspace.container_ssh_port,
            workspace.ssh_user,
            key_pem,
            _SSH_CONNECT_TIMEOUT_SECONDS,
        )
    except (paramiko.SSHException, OSError) as e:
        return SshCollectionResult(
            outcome=OUTCOME_SSH_REFUSED,
            parsed=None,
            stdout_bytes=0,
            presented_container_key=None,
            presented_vm_key=None,
            latchkey_record=None,
            detail=f"container hop refused: {str(e)[:200]}",
        )
    try:
        _write_remote_files(client, {**script_files, _CURSORS_FILENAME: cursors_json_text})
        command = _build_run_command(run_id, script_version, budget_bytes, workspace_timeout_seconds)
        _stdin, stdout, stderr = client.exec_command(command, timeout=float(workspace_timeout_seconds) + 60.0)
        stdout_text, stdout_bytes, is_truncated = _read_bounded_stdout(stdout.channel.makefile("rb"))
        stderr_tail = _read_stderr_tail(stderr)
        exit_status = stdout.channel.recv_exit_status()
    except (paramiko.SSHException, OSError) as e:
        return SshCollectionResult(
            outcome=OUTCOME_SCRIPT_FAILED,
            parsed=None,
            stdout_bytes=0,
            presented_container_key=presented_container_key,
            presented_vm_key=None,
            latchkey_record=None,
            detail=f"injection/exec failed: {str(e)[:200]}",
        )
    finally:
        client.close()

    latchkey_record, presented_vm_key, vm_detail = _probe_vm_latchkey_state(workspace, key_pem, run_id, collected_at)

    if exit_status == _TIMEOUT_EXIT_STATUS:
        outcome = OUTCOME_TIMEOUT
    elif exit_status != 0:
        outcome = OUTCOME_SCRIPT_FAILED
    else:
        outcome = OUTCOME_OK
    parsed = parse_collection_output(stdout_text)
    if outcome == OUTCOME_OK and parsed.run_summary is None:
        outcome = OUTCOME_PROTOCOL_ERROR
    detail_parts = [part for part in (vm_detail,) if part]
    if is_truncated:
        detail_parts.append("stdout truncated at the runner cap")
    if outcome != OUTCOME_OK:
        detail_parts.append(f"exit={exit_status}; stderr tail: {stderr_tail.strip()}")
    if parsed.run_summary is not None and parsed.run_summary.error_by_source:
        detail_parts.append(f"feed errors: {json.dumps(parsed.run_summary.error_by_source, sort_keys=True)}")
    return SshCollectionResult(
        outcome=outcome,
        parsed=parsed,
        stdout_bytes=stdout_bytes,
        presented_container_key=presented_container_key,
        presented_vm_key=presented_vm_key,
        latchkey_record=latchkey_record,
        detail="; ".join(detail_parts)[:_MAX_DETAIL_CHARS],
    )


def _record_rows(
    records: tuple[CollectedRecord, ...],
    workspace: CollectableWorkspace,
    run_id: str,
    collected_at: datetime,
    script_version: str,
) -> list[tuple[Any, ...]]:
    return [
        (
            record.timestamp,
            record.event_id,
            record.record_type,
            record.record_source,
            record.feed_source,
            workspace.host_id,
            workspace.account_id,
            run_id,
            collected_at,
            script_version,
            record.payload,
        )
        for record in records
    ]


def _latchkey_row(
    latchkey_record: dict[str, Any],
    workspace: CollectableWorkspace,
    run_id: str,
    collected_at: datetime,
    script_version: str,
) -> tuple[Any, ...]:
    return (
        collected_at,
        str(latchkey_record["event_id"]),
        "latchkey_state",
        "latchkey_state",
        "latchkey_state",
        workspace.host_id,
        workspace.account_id,
        run_id,
        collected_at,
        script_version,
        json.dumps(latchkey_record, sort_keys=True),
    )


def _detect_and_record_host_keys(
    ops_connection: Any,
    workspace: CollectableWorkspace,
    ssh_result: SshCollectionResult,
    now: datetime,
) -> bool:
    """Record presented host keys; returns True when any differed from the expectation."""
    last_seen_by_endpoint = ops_db.read_host_keys(ops_connection, workspace.host_id)
    is_changed = False
    presented_by_endpoint = {
        _CONTAINER_ENDPOINT: (ssh_result.presented_container_key, workspace.container_host_public_key),
        _VM_ENDPOINT: (ssh_result.presented_vm_key, workspace.outer_host_public_key),
    }
    for endpoint, (presented_key, bake_time_key) in presented_by_endpoint.items():
        if presented_key is None:
            continue
        expected_key = last_seen_by_endpoint.get(endpoint) or bake_time_key
        if expected_key is not None and expected_key.strip() != presented_key.strip():
            is_changed = True
            logger.warning("Host key changed for %s endpoint %s since it was last seen", workspace.host_id, endpoint)
        ops_db.record_host_key(
            ops_connection, host_id=workspace.host_id, endpoint=endpoint, host_public_key=presented_key, now=now
        )
    return is_changed


def process_collection_result(
    lake_connection: Any,
    ops_connection: Any,
    workspace: CollectableWorkspace,
    ssh_result: SshCollectionResult,
    run_id: str,
    script_version: str,
    started_at: datetime,
) -> WorkspaceCollectionOutcome:
    """Main-thread half of one workspace's collection: lake writes, cursors, audit."""
    now = datetime.now(timezone.utc)
    is_host_key_changed = _detect_and_record_host_keys(ops_connection, workspace, ssh_result, now)

    outcome = ssh_result.outcome
    detail = ssh_result.detail
    metrics_rows = 0
    transcript_rows = 0
    dropped_lines = ssh_result.parsed.dropped_line_count if ssh_result.parsed is not None else 0
    cursor_by_source: dict[str, str] = {}
    if ssh_result.parsed is not None and ssh_result.parsed.run_summary is not None:
        cursor_by_source = dict(ssh_result.parsed.run_summary.cursor_by_source)

    if ssh_result.parsed is not None and outcome == OUTCOME_OK:
        metrics_row_tuples = _record_rows(ssh_result.parsed.metrics_records, workspace, run_id, now, script_version)
        if ssh_result.latchkey_record is not None:
            metrics_row_tuples.append(
                _latchkey_row(ssh_result.latchkey_record, workspace, run_id, now, script_version)
            )
        transcript_row_tuples = _record_rows(
            ssh_result.parsed.transcript_records, workspace, run_id, now, script_version
        )

        # Commit order per lake: insert batch, then advance that lake's
        # cursors. A failed insert leaves the cursors put so the next run
        # re-collects (deduped by event id downstream).
        try:
            insert_raw_records(lake_connection, METRICS_RAW_EVENTS_TABLE, metrics_row_tuples)
            metrics_rows = len(metrics_row_tuples)
            for source, cursor_value in cursor_by_source.items():
                if source != TRANSCRIPTS_SOURCE:
                    ops_db.write_cursor(
                        ops_connection, host_id=workspace.host_id, source=source, cursor_value=cursor_value, now=now
                    )
        except LakeInsertError as e:
            outcome = OUTCOME_LAKE_ERROR
            detail = f"{detail}; metrics insert failed: {e}"[:_MAX_DETAIL_CHARS]

        if outcome == OUTCOME_OK:
            try:
                insert_raw_records(lake_connection, TRANSCRIPTS_RAW_EVENTS_TABLE, transcript_row_tuples)
                transcript_rows = len(transcript_row_tuples)
                if TRANSCRIPTS_SOURCE in cursor_by_source:
                    ops_db.write_cursor(
                        ops_connection,
                        host_id=workspace.host_id,
                        source=TRANSCRIPTS_SOURCE,
                        cursor_value=cursor_by_source[TRANSCRIPTS_SOURCE],
                        now=now,
                    )
            except LakeInsertError as e:
                outcome = OUTCOME_LAKE_ERROR
                detail = f"{detail}; transcripts insert failed: {e}"[:_MAX_DETAIL_CHARS]
    audit_now = datetime.now(timezone.utc)
    ops_db.record_collection_run(
        ops_connection,
        run_id=run_id,
        host_id=workspace.host_id,
        account_id=workspace.account_id,
        started_at=started_at,
        finished_at=audit_now,
        outcome=outcome,
        script_version=script_version,
        metrics_rows=metrics_rows,
        transcript_rows=transcript_rows,
        dropped_lines=dropped_lines,
        stdout_bytes=ssh_result.stdout_bytes,
        is_host_key_changed=is_host_key_changed,
        detail=detail[:_MAX_DETAIL_CHARS],
    )
    return WorkspaceCollectionOutcome(
        run_id=run_id,
        outcome=outcome,
        metrics_rows=metrics_rows,
        transcript_rows=transcript_rows,
        dropped_lines=dropped_lines,
        stdout_bytes=ssh_result.stdout_bytes,
        is_host_key_changed=is_host_key_changed,
        detail=detail[:_MAX_DETAIL_CHARS],
    )


def _try_advisory_lock(ops_connection: Any) -> bool:
    """Session-scoped advisory lock so overlapping poll runs never double-collect."""
    with ops_connection.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (_COLLECTION_POLL_LOCK_KEY,))
        row = cursor.fetchone()
    return bool(row and row[0])


def _due_workspaces(
    workspaces: list[CollectableWorkspace],
    last_attempt_by_host_id: dict[str, datetime],
    interval_seconds: int,
    now: datetime,
) -> list[CollectableWorkspace]:
    """Workspaces whose last collection attempt (any outcome) is older than the interval."""
    threshold = now - timedelta(seconds=interval_seconds)
    due: list[CollectableWorkspace] = []
    for workspace in workspaces:
        last_attempt = last_attempt_by_host_id.get(workspace.host_id)
        if last_attempt is None or last_attempt <= threshold:
            due.append(workspace)
    return due


def _cursors_json_for_host(ops_connection: Any, host_id: str) -> str:
    """The cursors-file JSON handed to the injected script, from the stored cursors.

    A stored cursor that is not valid JSON (a legacy or hand-edited row; the
    protocol validator refuses to persist new ones) is skipped with a warning
    rather than sinking the whole poll -- that feed simply re-collects from
    scratch, deduped by event id downstream.
    """
    cursor_by_source: dict[str, Any] = {}
    for source, cursor_value in ops_db.read_cursors_for_host(ops_connection, host_id).items():
        try:
            cursor_by_source[source] = json.loads(cursor_value)
        except json.JSONDecodeError:
            logger.warning("Ignored an unparsable stored cursor for host %s source %s", host_id, source)
    return json.dumps(cursor_by_source, sort_keys=True)


def run_collection_poll_with_connections(
    collection_settings: CollectionSettings,
    lake_connection: Any,
    ops_connection: Any,
    rsc_connection: Any,
    # The per-workspace SSH phase, with collect_over_ssh's signature.
    # Production passes collect_over_ssh; tests substitute a network-free fake.
    collect_fn: Callable[..., SshCollectionResult],
) -> dict[str, int]:
    """One poll pass over injected connections (the testable core of the cron).

    Raises CollectionError on infrastructure failures (consent sync,
    enumeration, ops-database access); individual workspace failures only
    land in the audit.
    """
    try:
        return _run_locked_collection_poll(
            collection_settings=collection_settings,
            lake_connection=lake_connection,
            ops_connection=ops_connection,
            rsc_connection=rsc_connection,
            collect_fn=collect_fn,
        )
    except psycopg2.Error as e:
        # The poll's own ops-DB access (lock, cursors, host keys, audit) must
        # surface as an AnalyticsError so the job records a failure row.
        raise CollectionError("Ops-database access failed during the collection poll") from e


def _run_locked_collection_poll(
    collection_settings: CollectionSettings,
    lake_connection: Any,
    ops_connection: Any,
    rsc_connection: Any,
    collect_fn: Callable[..., SshCollectionResult],
) -> dict[str, int]:
    if not _try_advisory_lock(ops_connection):
        logger.info("Skipped collection poll: a previous run still holds the advisory lock")
        return {"skipped_overlapping": 1}
    now = datetime.now(timezone.utc)
    account_id_by_prefix = read_explorer_accounts(rsc_connection)
    consent_result = sync_consent_ledger(ops_connection, set(account_id_by_prefix.values()), now)
    workspaces = list_online_explorer_workspaces(rsc_connection, account_id_by_prefix)
    last_attempt_by_host_id = ops_db.read_last_collection_attempts(ops_connection)
    due = _due_workspaces(workspaces, last_attempt_by_host_id, collection_settings.interval_seconds, now)
    logger.info(
        "Collection poll: %d consenting accounts, %d online workspaces, %d due",
        consent_result.consenting_account_count,
        len(workspaces),
        len(due),
    )

    script_files = load_injected_script_files()
    script_version = compute_script_version(script_files)
    key_pem = collection_settings.pool_ssh_private_key.get_secret_value()

    counters = {
        "workspaces_due": len(due),
        "workspaces_collected": 0,
        "workspaces_failed": 0,
        "metrics_rows": 0,
        "transcript_rows": 0,
        "dropped_lines": 0,
    }
    # SSH runs in a bounded pool; every DB and lake write happens back on this
    # thread (DuckDB connections and psycopg2 connections are not shared
    # across threads).
    with ThreadPoolExecutor(max_workers=collection_settings.parallelism) as executor:
        future_by_workspace: dict[Any, tuple[CollectableWorkspace, str, datetime]] = {}
        for workspace in due:
            run_id = uuid.uuid4().hex
            started_at = datetime.now(timezone.utc)
            cursors_json_text = _cursors_json_for_host(ops_connection, workspace.host_id)
            future = executor.submit(
                collect_fn,
                workspace,
                key_pem,
                script_files,
                script_version,
                cursors_json_text,
                run_id,
                collection_settings.workspace_timeout_seconds,
                collection_settings.run_budget_bytes,
            )
            future_by_workspace[future] = (workspace, run_id, started_at)
        for future, (workspace, run_id, started_at) in future_by_workspace.items():
            ssh_result = _ssh_result_or_timeout(future, collection_settings.workspace_timeout_seconds)
            outcome = process_collection_result(
                lake_connection=lake_connection,
                ops_connection=ops_connection,
                workspace=workspace,
                ssh_result=ssh_result,
                run_id=run_id,
                script_version=script_version,
                started_at=started_at,
            )
            if outcome.outcome == OUTCOME_OK:
                counters["workspaces_collected"] += 1
            else:
                counters["workspaces_failed"] += 1
                logger.info("Collection from %s finished with outcome %s", workspace.host_id, outcome.outcome)
            counters["metrics_rows"] += outcome.metrics_rows
            counters["transcript_rows"] += outcome.transcript_rows
            counters["dropped_lines"] += outcome.dropped_lines
    return counters


def _ssh_result_or_timeout(
    future: "Future[SshCollectionResult]", workspace_timeout_seconds: int
) -> SshCollectionResult:
    """Resolve one SSH future with a generous backstop past the remote timeout."""
    try:
        return future.result(timeout=float(workspace_timeout_seconds) + 120.0)
    except FutureTimeoutError:
        return SshCollectionResult(
            outcome=OUTCOME_TIMEOUT,
            parsed=None,
            stdout_bytes=0,
            presented_container_key=None,
            presented_vm_key=None,
            latchkey_record=None,
            detail="ssh phase exceeded the runner-side backstop timeout",
        )


def run_collection_poll(
    settings: AnalyticsSettings,
    collection_settings: CollectionSettings,
    lake_connection: Any,
) -> dict[str, int]:
    """The production poll body: real ops and connector connections around the core.

    Raises CollectionError when either database cannot be reached.
    """
    try:
        ops_connection = ops_db.get_ops_db_connection(settings.ops_dsn.get_secret_value())
    except psycopg2.Error as e:
        raise CollectionError("Cannot connect to the analytics ops database") from e
    try:
        try:
            rsc_connection = ops_db.get_ops_db_connection(settings.rsc_readonly_dsn.get_secret_value())
        except psycopg2.Error as e:
            raise CollectionError("Cannot connect to the connector database (read-only)") from e
        try:
            return run_collection_poll_with_connections(
                collection_settings=collection_settings,
                lake_connection=lake_connection,
                ops_connection=ops_connection,
                rsc_connection=rsc_connection,
                collect_fn=collect_over_ssh,
            )
        finally:
            rsc_connection.close()
    finally:
        # Closing the session releases the advisory lock.
        ops_connection.close()
