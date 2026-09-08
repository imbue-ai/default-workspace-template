"""Access Minds CI flaky tests and reconcile MIND Linear tickets.

This is a thin tool for the flake skills (`detect-flakes`, `manage-flakes`,
`report-incidental-flakes`); it is always driven by one of them and does nothing
on its own but move data in and out:

  - `list-flakes`  -- list the tests flaking in CI over a window, with the failure
                      messages seen for each (raw; no clustering).
  - `list-tickets` -- list the team's existing flake tickets, with their bodies.
  - `create-ticket` / `update-ticket` / `close-ticket` -- mutate Linear.

It deliberately does NOT cluster or root-cause failures. That judgment belongs to
the calling agent, which can read the failure text and group by cause far more
reliably than any regex. CI is read via `gh`; Linear is read/written via `latchkey`.
"""

import argparse
import html
import json
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import auto
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.logging import setup_logging
from imbue.imbue_common.pure import pure


class FlakeReconcileError(Exception):
    """Base exception for the flake-reconcile tool."""


# --- Parsing one flake-aware check-run summary --------------------------------


class RunOutcome(UpperCaseStrEnum):
    """The final status of a single test within one check-run."""

    FLAKY_RECOVERED = auto()
    HARD_FAILURE = auto()
    PASSED = auto()
    SKIPPED = auto()
    UNKNOWN = auto()


class CheckRunTestRow(FrozenModel):
    """One test's aggregated row from a flake-aware check-run's retry/flaky table."""

    test: str = Field(description="The junit test id (path::name)")
    runs: int = Field(description="How many times the test executed in this check-run")
    status: RunOutcome = Field(description="The final status across all attempts")
    flaked_count: int = Field(description="Attempts that failed before one passed (flaky-recovered only)")
    passed_count: int = Field(description="Attempts that passed (flaky-recovered only)")
    is_marked_flaky: bool = Field(description="Whether the test carries @pytest.mark.flaky")


class CheckRunFailureLine(FrozenModel):
    """One failed attempt surfaced in a check-run's ## Failures section."""

    test: str = Field(description="The junit test id (path::name)")
    kind: str = Field(description="failure or error")
    first_line: str = Field(description="First line of the failure message (unescaped)")
    attempt: int = Field(description="1-based index of this failed attempt")
    total_attempts: int = Field(description="Total attempts the test ran in this check-run")


class ParsedCheckRun(FrozenModel):
    """The structured content parsed out of one flake-aware check-run summary."""

    rows: tuple[CheckRunTestRow, ...] = Field(description="One row per unique test in the run")
    failure_lines: tuple[CheckRunFailureLine, ...] = Field(description="One entry per failed attempt")
    flaky_recovered_count: int = Field(description="Header count of flaky-recovered tests")
    failing_final_count: int = Field(description="Header count of tests failing on their final attempt")


_TABLE_ROW_RE: Final[re.Pattern[str]] = re.compile(
    r"^\|\s*`([^`]+)`\s*\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(yes|no)\s*\|\s*$",
    re.MULTILINE,
)

_FAILURE_BLOCK_RE: Final[re.Pattern[str]] = re.compile(
    r"<code>(?P<name>.+?)</code>"
    r"(?:\s*\(attempt (?P<attempt>\d+)/(?P<total>\d+)\))?"
    r"\s*&mdash;\s*(?P<kind>failure|error)"
    r"(?::\s*(?P<message>[^<]*?))?"
    r"\s*</summary>"
)


@pure
def _parse_final_cell(final_cell: str) -> tuple[RunOutcome, int, int]:
    stripped_cell = final_cell.strip()
    flaked_match = re.match(r"flaked (\d+), passed (\d+)", stripped_cell)
    if flaked_match is not None:
        return RunOutcome.FLAKY_RECOVERED, int(flaked_match.group(1)), int(flaked_match.group(2))
    if stripped_cell in ("failed", "error"):
        return RunOutcome.HARD_FAILURE, 0, 0
    if stripped_cell.startswith("passed"):
        return RunOutcome.PASSED, 0, 0
    if stripped_cell.startswith("skipped"):
        return RunOutcome.SKIPPED, 0, 0
    return RunOutcome.UNKNOWN, 0, 0


@pure
def _read_header_count(summary_markdown: str, label: str) -> int:
    match = re.search(re.escape(label) + r"[^*]*\*\*(\d+)\*\*", summary_markdown)
    return int(match.group(1)) if match is not None else 0


@pure
def parse_check_run_summary(summary_markdown: str) -> ParsedCheckRun:
    # Parse the per-test retry/flaky table.
    rows: list[CheckRunTestRow] = []
    for row_match in _TABLE_ROW_RE.finditer(summary_markdown):
        status, flaked_count, passed_count = _parse_final_cell(row_match.group(3))
        rows.append(
            CheckRunTestRow(
                test=row_match.group(1),
                runs=int(row_match.group(2)),
                status=status,
                flaked_count=flaked_count,
                passed_count=passed_count,
                is_marked_flaky=row_match.group(4) == "yes",
            )
        )

    # Parse the per-failed-attempt detail blocks.
    failure_lines: list[CheckRunFailureLine] = []
    for failure_match in _FAILURE_BLOCK_RE.finditer(summary_markdown):
        attempt_group = failure_match.group("attempt")
        total_group = failure_match.group("total")
        failure_lines.append(
            CheckRunFailureLine(
                test=_unescape(failure_match.group("name")),
                kind=failure_match.group("kind"),
                first_line=_unescape((failure_match.group("message") or "").strip()),
                attempt=int(attempt_group) if attempt_group is not None else 1,
                total_attempts=int(total_group) if total_group is not None else 1,
            )
        )

    return ParsedCheckRun(
        rows=tuple(rows),
        failure_lines=tuple(failure_lines),
        flaky_recovered_count=_read_header_count(summary_markdown, "Flaky-recovered"),
        failing_final_count=_read_header_count(summary_markdown, "Failing (final)"),
    )


@pure
def _unescape(value: str) -> str:
    # The check-run summary HTML-escapes test names and messages; undo that.
    return html.unescape(value)


# --- Windowed flake data (raw; the agent does the clustering) -----------------


class CheckRunRecord(FrozenModel):
    """One flake-aware check-run: its CI metadata plus its parsed summary."""

    suite: str = Field(description="Check-run name, e.g. 'Acceptance Tests'")
    conclusion: str = Field(description="GitHub check conclusion (neutral/failure)")
    commit: str = Field(description="Head commit SHA the check ran on")
    branch: str = Field(description="Head branch of the run")
    occurred_at: str = Field(description="ISO-8601 UTC timestamp the check completed")
    url: str = Field(description="Link to the run/check")
    parsed: ParsedCheckRun = Field(description="Structured content of the summary")


class FlakyTest(FrozenModel):
    """A test that flaked in CI over the window, with the failures seen for it.

    No root cause is assigned -- `sample_failure_lines` is raw material for the
    calling agent to cluster by understanding.
    """

    test: str = Field(description="The junit test id (path::name)")
    flake_commit_count: int = Field(description="Distinct commits on which the test flaky-recovered")
    hard_fail_commit_count: int = Field(description="Distinct commits on which the test hard-failed")
    is_marked_flaky: bool = Field(description="Whether any observation carried @pytest.mark.flaky")
    suites: tuple[str, ...] = Field(description="Distinct check-run suites it flaked under")
    branches: tuple[str, ...] = Field(description="Distinct branches it flaked on")
    first_seen: str = Field(description="Earliest observation timestamp in the window")
    last_seen: str = Field(description="Latest observation timestamp in the window")
    sample_failure_lines: tuple[str, ...] = Field(description="Distinct failure first-lines seen for this test")


_MAX_SAMPLE_FAILURE_LINES: Final[int] = 8


@pure
def _earliest_failure_line_by_test(failure_lines: Sequence[CheckRunFailureLine]) -> dict[str, str]:
    # Keep the earliest-attempt failed line per test as its representative for the run.
    earliest_by_test: dict[str, CheckRunFailureLine] = {}
    for line in failure_lines:
        existing_line = earliest_by_test.get(line.test)
        if existing_line is None or line.attempt < existing_line.attempt:
            earliest_by_test[line.test] = line
    return {test: line.first_line for test, line in earliest_by_test.items() if line.first_line}


@pure
def aggregate_flaky_tests(records: Sequence[CheckRunRecord]) -> tuple[FlakyTest, ...]:
    # Gather, per test, the distinct commits it flaked / hard-failed on plus context.
    flake_commits_by_test: dict[str, set[str]] = defaultdict(set)
    hard_fail_commits_by_test: dict[str, set[str]] = defaultdict(set)
    is_marked_by_test: dict[str, bool] = defaultdict(bool)
    suites_by_test: dict[str, set[str]] = defaultdict(set)
    branches_by_test: dict[str, set[str]] = defaultdict(set)
    timestamps_by_test: dict[str, set[str]] = defaultdict(set)
    failure_lines_by_test: dict[str, list[str]] = defaultdict(list)

    for record in records:
        representative_line_by_test = _earliest_failure_line_by_test(record.parsed.failure_lines)
        for row in record.parsed.rows:
            if row.status is RunOutcome.FLAKY_RECOVERED:
                flake_commits_by_test[row.test].add(record.commit)
            elif row.status is RunOutcome.HARD_FAILURE:
                hard_fail_commits_by_test[row.test].add(record.commit)
            else:
                continue
            is_marked_by_test[row.test] = is_marked_by_test[row.test] or row.is_marked_flaky
            suites_by_test[row.test].add(record.suite)
            branches_by_test[row.test].add(record.branch)
            timestamps_by_test[row.test].add(record.occurred_at)
            representative_line = representative_line_by_test.get(row.test)
            if representative_line is not None:
                failure_lines_by_test[row.test].append(representative_line)

    # A test qualifies as a flake only if it recovered on at least one commit; this
    # deliberately drops pure hard failures (ruff/type/docs gates) that never flake.
    flaky_tests: list[FlakyTest] = []
    for test in flake_commits_by_test:
        sorted_timestamps = sorted(timestamps_by_test[test])
        distinct_failure_lines = tuple(dict.fromkeys(failure_lines_by_test.get(test, [])))[:_MAX_SAMPLE_FAILURE_LINES]
        flaky_tests.append(
            FlakyTest(
                test=test,
                flake_commit_count=len(flake_commits_by_test[test]),
                hard_fail_commit_count=len(hard_fail_commits_by_test.get(test, set())),
                is_marked_flaky=is_marked_by_test[test],
                suites=tuple(sorted(suites_by_test[test])),
                branches=tuple(sorted(branches_by_test[test])),
                first_seen=sorted_timestamps[0],
                last_seen=sorted_timestamps[-1],
                sample_failure_lines=distinct_failure_lines,
            )
        )
    return tuple(sorted(flaky_tests, key=lambda flaky_test: (-flaky_test.flake_commit_count, flaky_test.test)))


class ClusterStatus(UpperCaseStrEnum):
    """The status a cluster's flake evidence argues for (before reconciling with Linear)."""

    READY = auto()
    BACKLOG = auto()
    UNKNOWN = auto()


@pure
def preferred_status_for_branches(branches: AbstractSet[str]) -> ClusterStatus:
    """The branch filter: does a cluster's flake evidence justify working it *now*?

    A flake seen on `main` is a live main problem -> ready. A flake never on main
    but on more than one feature branch is branch-independent (systemic) -> ready.
    A flake seen only on a single unmerged feature branch is most likely that
    branch's own bug -- a fixer could not reproduce it on main -> backlog.
    """
    if not branches:
        return ClusterStatus.UNKNOWN
    if "main" in branches:
        return ClusterStatus.READY
    if len(branches) > 1:
        return ClusterStatus.READY
    return ClusterStatus.BACKLOG


# --- Linear tickets (read model) ----------------------------------------------


class FlakeTicket(FrozenModel):
    """An existing flake ticket in the team, returned verbatim for the agent to read."""

    identifier: str = Field(description="Human ticket key, e.g. MIND-200")
    issue_id: str = Field(description="Linear issue UUID used for mutations")
    url: str = Field(description="Link to the ticket")
    state_type: str = Field(description="Workflow state type (triage/backlog/unstarted/started/completed/canceled)")
    is_open: bool = Field(description="Whether the ticket is in a non-terminal state")
    description: str = Field(description="Full ticket body (the agent reads its own markers from this)")


# --- I/O boundary: `gh` for CI, `latchkey` for Linear -------------------------


_DEFAULT_REPO: Final[str] = "imbue-ai/mngr-internal"
_DEFAULT_TEAM_KEY: Final[str] = "MIND"
_DEFAULT_SUITES: Final[tuple[str, ...]] = (
    "Unit + Integration Tests",
    "Acceptance Tests",
    "Minds Snapshot Resume Tests",
)
_DEFAULT_WINDOW_DAYS: Final[int] = 14
_DEFAULT_RUN_LIMIT: Final[int] = 2000
_FLAKY_CLUSTER_LABEL: Final[str] = "flaky-cluster"
_LINEAR_GRAPHQL_URL: Final[str] = "https://api.linear.app/graphql"
_GH_TIMEOUT_SECONDS: Final[int] = 180
_LINEAR_TIMEOUT_SECONDS: Final[int] = 60
_TERMINAL_STATE_TYPES: Final[tuple[str, ...]] = ("completed", "canceled")


class _LinearIds(FrozenModel):
    """Resolved Linear UUIDs needed to create/close flake tickets in a team."""

    team_id: str = Field(description="Linear team UUID")
    label_id: str = Field(description="UUID of the flaky-cluster label")
    closed_state_id: str = Field(description="UUID of the workflow state used to close")
    ready_state_id: str = Field(
        description="UUID of the 'ready to work on' state (first unstarted), or empty for team default"
    )
    backlog_state_id: str = Field(description="UUID of the backlog state (first backlog-type), or empty if none")


@pure
def _parse_iso_timestamp(value: str) -> datetime:
    normalized_value = value.replace("Z", "+00:00")
    try:
        parsed_timestamp = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise FlakeReconcileError(f"Invalid ISO-8601 timestamp: {value!r}") from error
    if parsed_timestamp.tzinfo is None:
        return parsed_timestamp.replace(tzinfo=timezone.utc)
    return parsed_timestamp


def _run_json(command: Sequence[str], timeout_seconds: int) -> Any:
    try:
        completed = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        raise FlakeReconcileError(f"Command timed out after {timeout_seconds}s: {command[0]}") from error
    except FileNotFoundError as error:
        raise FlakeReconcileError(f"Command not found: {command[0]} (is it installed and on PATH?)") from error
    if completed.returncode != 0:
        raise FlakeReconcileError(
            f"Command failed ({completed.returncode}): {' '.join(command[:4])} ...\n{completed.stderr[:500]}"
        )
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise FlakeReconcileError(f"Expected JSON from {command[0]} but could not parse it") from error


def _linear_graphql(payload: Mapping[str, Any], timeout_seconds: int) -> Any:
    result = _run_json(
        [
            "latchkey",
            "curl",
            "-sS",
            "-X",
            "POST",
            _LINEAR_GRAPHQL_URL,
            "-H",
            "Content-Type: application/json",
            "-d",
            json.dumps(payload),
        ],
        timeout_seconds=timeout_seconds,
    )
    if isinstance(result, Mapping) and "errors" in result:
        raise FlakeReconcileError(f"Linear GraphQL error: {json.dumps(result['errors'])[:400]}")
    return result


def fetch_check_run_records(
    repo: str,
    since: datetime,
    until: datetime,
    suites: Sequence[str],
    run_limit: int,
) -> tuple[CheckRunRecord, ...]:
    # List recent CI runs and keep the latest run metadata per unique commit in window.
    runs = _run_json(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            "CI",
            "--limit",
            str(run_limit),
            "--json",
            "headSha,headBranch,createdAt,url",
        ],
        timeout_seconds=_GH_TIMEOUT_SECONDS,
    )
    meta_by_commit: dict[str, Mapping[str, str]] = {}
    for run in runs:
        created_at = run["createdAt"]
        if not (since <= _parse_iso_timestamp(created_at) <= until):
            continue
        commit = run["headSha"]
        existing_meta = meta_by_commit.get(commit)
        if existing_meta is None or created_at > existing_meta["createdAt"]:
            meta_by_commit[commit] = run
    logger.info("Reading check-runs for {} commit(s) in the last window", len(meta_by_commit))

    suite_names = set(suites)
    records: list[CheckRunRecord] = []
    for commit, meta in meta_by_commit.items():
        check_data = _run_json(
            ["gh", "api", f"repos/{repo}/commits/{commit}/check-runs", "--paginate"],
            timeout_seconds=_GH_TIMEOUT_SECONDS,
        )
        for check in check_data.get("check_runs", []):
            if check.get("name") not in suite_names or check.get("conclusion") not in ("neutral", "failure"):
                continue
            summary = (check.get("output") or {}).get("summary") or ""
            if not summary:
                continue
            records.append(
                CheckRunRecord(
                    suite=check["name"],
                    conclusion=check["conclusion"],
                    commit=commit,
                    branch=meta["headBranch"],
                    occurred_at=meta["createdAt"],
                    url=meta["url"],
                    parsed=parse_check_run_summary(summary),
                )
            )
    logger.info("Captured {} flake-aware check-run(s)", len(records))
    return tuple(records)


def fetch_flake_tickets(team_key: str) -> tuple[FlakeTicket, ...]:
    query = (
        'query { issues(filter: { team: { key: { eq: "'
        + team_key
        + '" } }, labels: { name: { eq: "'
        + _FLAKY_CLUSTER_LABEL
        + '" } } }, first: 250) { nodes { identifier id url description state { type } } } }'
    )
    data = _linear_graphql({"query": query}, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    tickets: list[FlakeTicket] = []
    for node in data["data"]["issues"]["nodes"]:
        state_type = (node.get("state") or {}).get("type", "")
        tickets.append(
            FlakeTicket(
                identifier=node["identifier"],
                issue_id=node["id"],
                url=node.get("url") or "",
                state_type=state_type,
                is_open=state_type not in _TERMINAL_STATE_TYPES,
                description=node.get("description") or "",
            )
        )
    return tuple(tickets)


def _resolve_linear_ids(team_key: str) -> _LinearIds:
    query = (
        'query { teams(filter: { key: { eq: "'
        + team_key
        + '" } }, first: 1) { nodes { id states { nodes { id name type position } } labels { nodes { id name } } } } }'
    )
    data = _linear_graphql({"query": query}, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    team_nodes = data["data"]["teams"]["nodes"]
    if not team_nodes:
        raise FlakeReconcileError(f"No Linear team with key {team_key!r}")
    team = team_nodes[0]

    label_id = next(
        (label["id"] for label in team["labels"]["nodes"] if label["name"] == _FLAKY_CLUSTER_LABEL),
        None,
    )
    if label_id is None:
        label_id = _create_flaky_cluster_label(team["id"])

    completed_states = sorted(
        (state for state in team["states"]["nodes"] if state["type"] == "completed"),
        key=lambda state: state["position"],
    )
    if not completed_states:
        raise FlakeReconcileError(f"Team {team_key!r} has no completed-type workflow state to close into")

    # "Ready to work on" == the first unstarted state (e.g. Todo); empty if the
    # team has none, in which case new tickets land in the team's default state.
    unstarted_states = sorted(
        (state for state in team["states"]["nodes"] if state["type"] == "unstarted"),
        key=lambda state: state["position"],
    )
    ready_state_id = unstarted_states[0]["id"] if unstarted_states else ""
    backlog_states = sorted(
        (state for state in team["states"]["nodes"] if state["type"] == "backlog"),
        key=lambda state: state["position"],
    )
    backlog_state_id = backlog_states[0]["id"] if backlog_states else ""
    return _LinearIds(
        team_id=team["id"],
        label_id=label_id,
        closed_state_id=completed_states[0]["id"],
        ready_state_id=ready_state_id,
        backlog_state_id=backlog_state_id,
    )


@pure
def _state_id_for_status(linear_ids: _LinearIds, status: str) -> str:
    return linear_ids.backlog_state_id if status == "backlog" else linear_ids.ready_state_id


def _create_flaky_cluster_label(team_id: str) -> str:
    payload = {
        "query": "mutation($input: IssueLabelCreateInput!){issueLabelCreate(input:$input){success issueLabel{id}}}",
        "variables": {"input": {"name": _FLAKY_CLUSTER_LABEL, "teamId": team_id, "color": "#e5484d"}},
    }
    data = _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    return data["data"]["issueLabelCreate"]["issueLabel"]["id"]


def create_ticket(team_key: str, title: str, body: str, status: str) -> dict[str, str]:
    linear_ids = _resolve_linear_ids(team_key)
    # File the ticket into the status the branch filter prefers: "ready" (Todo) for
    # a live/systemic flake, "backlog" for one only ever seen on a single branch.
    create_input: dict[str, object] = {
        "teamId": linear_ids.team_id,
        "title": title,
        "description": body,
        "labelIds": [linear_ids.label_id],
    }
    state_id = _state_id_for_status(linear_ids, status)
    if state_id:
        create_input["stateId"] = state_id
    payload = {
        "query": "mutation($input: IssueCreateInput!){issueCreate(input:$input){success issue{identifier url}}}",
        "variables": {"input": create_input},
    }
    data = _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    issue = data["data"]["issueCreate"]["issue"]
    logger.info("Created {} in {} ({})", issue["identifier"], team_key, status)
    return {"identifier": issue["identifier"], "url": issue["url"]}


def set_ticket_status(team_key: str, issue_id: str, status: str) -> None:
    linear_ids = _resolve_linear_ids(team_key)
    state_id = _state_id_for_status(linear_ids, status)
    if not state_id:
        raise FlakeReconcileError(f"Team {team_key!r} has no {status!r} workflow state")
    payload = {
        "query": "mutation($id: String!, $input: IssueUpdateInput!){issueUpdate(id:$id, input:$input){success}}",
        "variables": {"id": issue_id, "input": {"stateId": state_id}},
    }
    _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    logger.info("Set {} to {}", issue_id, status)


def comment_ticket(issue_id: str, body: str) -> None:
    payload = {
        "query": "mutation($input: CommentCreateInput!){commentCreate(input:$input){success comment{id}}}",
        "variables": {"input": {"issueId": issue_id, "body": body}},
    }
    _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    logger.info("Commented on {}", issue_id)


def update_ticket(issue_id: str, body: str, title: str | None) -> None:
    # Title is optional so a plain refresh leaves it alone, but a re-scope (split
    # or merge during reclustering) can rename the ticket to its new, narrower cause.
    update_input: dict[str, str] = {"description": body}
    if title is not None:
        update_input["title"] = title
    payload = {
        "query": "mutation($id: String!, $input: IssueUpdateInput!){issueUpdate(id:$id, input:$input){success}}",
        "variables": {"id": issue_id, "input": update_input},
    }
    _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    logger.info("Updated {}", issue_id)


def close_ticket(team_key: str, issue_id: str) -> None:
    linear_ids = _resolve_linear_ids(team_key)
    payload = {
        "query": "mutation($id: String!, $input: IssueUpdateInput!){issueUpdate(id:$id, input:$input){success}}",
        "variables": {"id": issue_id, "input": {"stateId": linear_ids.closed_state_id}},
    }
    _linear_graphql(payload, timeout_seconds=_LINEAR_TIMEOUT_SECONDS)
    logger.info("Closed {}", issue_id)


# --- CLI ----------------------------------------------------------------------


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Access Minds CI flaky tests and reconcile MIND Linear tickets (driven by the flake skills)."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_flakes = subparsers.add_parser("list-flakes", help="print flaking tests + their failures as JSON")
    list_flakes.add_argument("--repo", default=_DEFAULT_REPO, help="owner/name of the GitHub repo whose CI to read")
    list_flakes.add_argument(
        "--window-days", type=int, default=_DEFAULT_WINDOW_DAYS, help="days of CI history to read"
    )
    list_flakes.add_argument("--run-limit", type=int, default=_DEFAULT_RUN_LIMIT, help="max CI runs to list")
    list_flakes.add_argument(
        "--suite", dest="suites", action="append", help="check-run name to scan (repeatable; default: three suites)"
    )

    list_tickets = subparsers.add_parser("list-tickets", help="print existing flake tickets as JSON")
    list_tickets.add_argument("--team", dest="team_key", default=_DEFAULT_TEAM_KEY, help="Linear team key")

    create = subparsers.add_parser("create-ticket", help="create a flake ticket")
    create.add_argument("--team", dest="team_key", default=_DEFAULT_TEAM_KEY, help="Linear team key")
    create.add_argument("--title", required=True, help="ticket title")
    create.add_argument("--body-file", required=True, help="path to a file holding the ticket body markdown")
    create.add_argument(
        "--status", choices=["ready", "backlog"], default="ready", help="file into ready (Todo) or backlog"
    )

    update = subparsers.add_parser("update-ticket", help="replace a ticket's body (and optionally its title)")
    update.add_argument("--id", dest="issue_id", required=True, help="Linear issue UUID")
    update.add_argument("--body-file", required=True, help="path to a file holding the new ticket body markdown")
    update.add_argument("--title", default=None, help="optional new title (use when re-scoping a cluster)")

    close = subparsers.add_parser("close-ticket", help="move a ticket to a completed state")
    close.add_argument("--team", dest="team_key", default=_DEFAULT_TEAM_KEY, help="Linear team key")
    close.add_argument("--id", dest="issue_id", required=True, help="Linear issue UUID")

    set_status = subparsers.add_parser("set-status", help="move a ticket to ready (Todo) or backlog")
    set_status.add_argument("--team", dest="team_key", default=_DEFAULT_TEAM_KEY, help="Linear team key")
    set_status.add_argument("--id", dest="issue_id", required=True, help="Linear issue UUID")
    set_status.add_argument("--status", choices=["ready", "backlog"], required=True, help="target status")

    comment = subparsers.add_parser("comment-ticket", help="add a comment to a ticket")
    comment.add_argument("--id", dest="issue_id", required=True, help="Linear issue UUID")
    comment.add_argument("--body-file", required=True, help="path to a file holding the comment markdown")

    preferred = subparsers.add_parser(
        "preferred-status", help="branch filter: print ready/backlog/unknown for a cluster's branches"
    )
    preferred.add_argument(
        "--branch", dest="branches", action="append", default=[], help="a branch the cluster flaked on (repeatable)"
    )

    return parser.parse_args(list(argv))


def main() -> int:
    setup_logging(level="INFO")
    args = _parse_args(sys.argv[1:])
    now = datetime.now(timezone.utc)

    if args.command == "list-flakes":
        since = now - timedelta(days=args.window_days)
        suites = tuple(args.suites) if args.suites else _DEFAULT_SUITES
        records = fetch_check_run_records(args.repo, since, now, suites, args.run_limit)
        flaky_tests = aggregate_flaky_tests(records)
        print(json.dumps([flaky_test.model_dump() for flaky_test in flaky_tests], indent=2))
    elif args.command == "list-tickets":
        tickets = fetch_flake_tickets(args.team_key)
        print(json.dumps([ticket.model_dump() for ticket in tickets], indent=2))
    elif args.command == "create-ticket":
        created = create_ticket(args.team_key, args.title, Path(args.body_file).read_text(), args.status)
        print(json.dumps(created, indent=2))
    elif args.command == "update-ticket":
        update_ticket(args.issue_id, Path(args.body_file).read_text(), args.title)
        print(json.dumps({"updated": args.issue_id}))
    elif args.command == "close-ticket":
        close_ticket(args.team_key, args.issue_id)
        print(json.dumps({"closed": args.issue_id}))
    elif args.command == "set-status":
        set_ticket_status(args.team_key, args.issue_id, args.status)
        print(json.dumps({"set": args.issue_id, "status": args.status}))
    elif args.command == "comment-ticket":
        comment_ticket(args.issue_id, Path(args.body_file).read_text())
        print(json.dumps({"commented": args.issue_id}))
    elif args.command == "preferred-status":
        print(preferred_status_for_branches(set(args.branches)).name.lower())
    else:
        raise FlakeReconcileError(f"Unknown command: {args.command}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
