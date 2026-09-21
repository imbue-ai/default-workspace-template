"""The behaviour family's two compliance records, which share nothing with the readers they check.

- The diagnostic probe (D): one exec at collection, on a step that asks for it, printing the workspace's
  `tk` ticket files raw, its agents as its own `mngr list` names them, which launch-task reports exist,
  and every file under the uploads directory. `parse_diagnostic_probe` reads that output with its own
  few lines of parsing, never the collector's ticket or listing parsers.
- The event feed (E): the chat app's events the driver polled, whose tool results carry a `tk_stamp`,
  and the tool input of every tool call, which the feed's per-event detail endpoint serves.

A compliance fact computed from these cannot turn a regression in the tickets capture, the agent
listing, the captured trajectory or the verifier's renderers into `not followed`.
"""

import base64
import json
import re
import shlex
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any
from typing import Final

from imbue.imbue_common.pure import pure
from imbue.minds_evals.data_types import DiagnosticProbeReading
from imbue.minds_evals.data_types import ProbeAgent
from imbue.minds_evals.data_types import ProbeTicket

# The behaviour case's literals, as configs/eval-config-diagnostics-behaviour.json spells them; a test pins
# every one against the config. Each carries the nonce, so no template text can match one by accident.
NONCE: Final[str] = "7f3a"
STEP_TICKET_TITLES: Final[tuple[str, ...]] = ("DIAG alpha 7f3a", "DIAG beta 7f3a")
REGULAR_TICKET_TITLE: Final[str] = "DIAG regular ticket 7f3a"
NONCE_ECHO_COMMAND: Final[str] = "echo DIAG-7f3a-one"
NONCE_ECHO_OUTPUT: Final[str] = "DIAG-7f3a-one"
MISSING_COMMAND: Final[str] = "diag-missing-command-7f3a"
WORKER_NAME: Final[str] = "diag-worker-7f3a"
WORKER_REPORT_PATH: Final[str] = "data/.tasks/launch-task/diag-worker-7f3a/reports/report.md"
UPLOAD_MARKER_PATH: Final[str] = "data/uploads/diagupload7f3a/marker.txt"
UPLOAD_MARKER_TEXT: Final[str] = "DIAG-UPLOAD-7f3a"
# The skill the last item asks for, and the file a harness with no skill tool reads instead. The
# name is a real template skill rather than a nonce one, because a skill that does not exist can be
# neither invoked nor read.
SKILL_NAME: Final[str] = "build-app"
SKILL_FILE_PATH: Final[str] = ".agents/skills/build-app/SKILL.md"
# claude's own skill tool, as the feed labels the call.
SKILL_TOOL_NAME: Final[str] = "Skill"

# A shell no-op naming the probe in the trace, where the program itself is an opaque base64 string.
DIAGNOSTIC_PROBE_COMMAND_LABEL: Final[str] = ": minds_evals_diagnostic_probe;"
PRODUCTION_AGENT_LISTING_COMMAND: Final[str] = "mngr list --headless --format json"

_SECTION_PREFIX: Final[str] = "==== minds_evals_probe:"
_TICKETS_SECTION: Final[str] = "tickets"
_TICKET_SECTION_PREFIX: Final[str] = "ticket "
_AGENTS_SECTION: Final[str] = "agents"
_AGENTS_UNREADABLE_SECTION: Final[str] = "agents_unreadable"
_REPORTS_SECTION: Final[str] = "reports"
_UPLOADS_SECTION: Final[str] = "uploads"
# Printed last, so an output cut short by a timeout or a crash never reads as a workspace holding less.
_END_SECTION: Final[str] = "end"
# A tk ticket is a few hundred bytes; the bound keeps a runaway file within one exec's output.
_MAX_TICKET_FILE_BYTES: Final[int] = 8_000

# Reads the workspace from its repo root, and the agent listing from the file the shell wrote it to, which
# the program is handed as its one argument. The output is printed whole at the end, so a program that
# fails part way prints no end section.
_PROBE_PROGRAM: Final[str] = """
import glob, json, os, sys
listing_path = sys.argv[1]
os.chdir({repo_root!r})
lines = [{prefix!r} + {tickets!r}]
for path in sorted(glob.glob("data/.tickets/*.md")):
    with open(path, "rb") as handle:
        lines.extend([{prefix!r} + {ticket_prefix!r} + path, handle.read({ticket_limit}).decode("utf-8", "replace")])
try:
    with open(listing_path, "rb") as handle:
        payload = json.loads(handle.read().decode("utf-8", "replace"))
except (OSError, ValueError):
    payload = None
agents = payload.get("agents") if isinstance(payload, dict) else None
if isinstance(agents, list):
    lines.append({prefix!r} + {agents_section!r})
    lines.extend(
        json.dumps({{"name": agent.get("name"), "type": agent.get("type"), "labels": agent.get("labels")}})
        for agent in agents
        if isinstance(agent, dict)
    )
else:
    lines.append({prefix!r} + {agents_unreadable!r})
lines.append({prefix!r} + {reports!r})
lines.extend(sorted(glob.glob("data/.tasks/launch-task/*/reports/report.md")))
lines.append({prefix!r} + {uploads!r})
lines.extend(sorted(os.path.join(directory, name) for directory, _, names in os.walk("data/uploads") for name in names))
lines.append({prefix!r} + {end!r})
print("\\n".join(lines))
"""

# One line of `tk`'s own output naming a step id and its title: `Created <id>: <title>` from a create,
# and the `tk-step <id> title: <title>` that `tk start` and `tk close` print.
_CREATED_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^Created (?P<id>\S+): (?P<title>.+)$", re.MULTILINE)
_STEP_TITLE_LINE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^tk-step (?P<id>\S+) title: (?P<title>.*)$", re.MULTILINE
)


@pure
def diagnostic_probe_command(repo_root: str, agent_listing_command: str) -> str:
    """The probe as one line of shell, run from the workspace repo; `agent_listing_command` prints the
    agent listing as JSON (`PRODUCTION_AGENT_LISTING_COMMAND` in a workspace)."""
    program = _PROBE_PROGRAM.format(
        repo_root=repo_root,
        prefix=_SECTION_PREFIX,
        tickets=_TICKETS_SECTION,
        ticket_prefix=_TICKET_SECTION_PREFIX,
        ticket_limit=_MAX_TICKET_FILE_BYTES,
        agents_section=_AGENTS_SECTION,
        agents_unreadable=_AGENTS_UNREADABLE_SECTION,
        reports=_REPORTS_SECTION,
        uploads=_UPLOADS_SECTION,
        end=_END_SECTION,
    )
    encoded = base64.b64encode(program.encode()).decode("ascii")
    # The listing runs in a subshell of its own, so a listing command that exits cannot end the probe.
    return (
        '{label} listing=$(mktemp); ({listing_command}) > "$listing" 2>/dev/null; '
        'printf \'%s\' {program} | base64 -d | python3 - "$listing"; rm -f "$listing"'
    ).format(label=DIAGNOSTIC_PROBE_COMMAND_LABEL, listing_command=agent_listing_command, program=shlex.quote(encoded))


@pure
def _split_probe_sections(output: str) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    for line in output.splitlines():
        if line.startswith(_SECTION_PREFIX):
            sections.append((line[len(_SECTION_PREFIX) :], []))
        elif sections:
            sections[-1][1].append(line)
        else:
            # Anything the exec printed ahead of the first section is not the probe's.
            continue
    return sections


@pure
def _probe_ticket(lines: Sequence[str]) -> ProbeTicket | None:
    """A ticket file's frontmatter status and step flag and its first heading; None for a file that opens
    with no frontmatter, such as the directory's README."""
    stripped = [line.strip() for line in lines]
    if not stripped or stripped[0] != "---" or "---" not in stripped[1:]:
        return None
    closing_index = stripped.index("---", 1)
    frontmatter = {
        key.strip(): value.strip() for key, _, value in (line.partition(":") for line in stripped[1:closing_index])
    }
    title = next((line[2:].strip() for line in lines[closing_index + 1 :] if line.startswith("# ")), "")
    return ProbeTicket(
        title=title, status=frontmatter.get("status", ""), is_step=frontmatter.get("step", "").lower() == "true"
    )


@pure
def _probe_agent(line: str) -> ProbeAgent | None:
    try:
        raw_agent = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw_agent, dict):
        return None
    raw_labels = raw_agent.get("labels")
    return ProbeAgent(
        name=str(raw_agent.get("name") or ""),
        agent_type=str(raw_agent.get("type") or ""),
        labels={str(key): str(value) for key, value in raw_labels.items()} if isinstance(raw_labels, dict) else {},
    )


@pure
def parse_diagnostic_probe(output: str) -> DiagnosticProbeReading | None:
    """The probe's output as a reading; None when it did not run to its end."""
    sections = _split_probe_sections(output)
    if not sections or sections[-1][0] != _END_SECTION:
        return None
    tickets: list[ProbeTicket] = []
    agents: list[ProbeAgent] | None = None
    report_paths: list[str] = []
    upload_paths: list[str] = []
    for header, lines in sections:
        if header.startswith(_TICKET_SECTION_PREFIX):
            ticket = _probe_ticket(lines)
            tickets.extend([ticket] if ticket is not None else [])
        elif header == _AGENTS_SECTION:
            agents = [agent for agent in (_probe_agent(line) for line in lines if line.strip()) if agent is not None]
        elif header == _REPORTS_SECTION:
            report_paths.extend(line.strip() for line in lines if line.strip())
        elif header == _UPLOADS_SECTION:
            upload_paths.extend(line.strip() for line in lines if line.strip())
        elif header in (_TICKETS_SECTION, _AGENTS_UNREADABLE_SECTION, _END_SECTION):
            continue
        else:
            # A section this parser does not know is left unread rather than guessed at.
            continue
    return DiagnosticProbeReading(
        tickets=tuple(tickets),
        agents=tuple(agents) if agents is not None else None,
        report_paths=tuple(report_paths),
        upload_paths=tuple(upload_paths),
    )


# --- the event feed ---


@pure
def feed_tool_calls(events: Sequence[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Every tool call the feed shows, as (the id of the event that carries it, its tool call id)."""
    return [
        (str(event.get("event_id") or ""), str(call.get("tool_call_id") or ""))
        for event in events
        if event.get("type") == "assistant_message"
        for call in event.get("tool_calls") or []
        if isinstance(call, Mapping)
    ]


@pure
def feed_tool_name_by_call_id(events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The tool each call the feed shows was made with, by call id, as the feed labels it."""
    return {
        str(call.get("tool_call_id") or ""): str(call.get("tool_name") or "")
        for event in events
        if event.get("type") == "assistant_message"
        for call in event.get("tool_calls") or []
        if isinstance(call, Mapping)
    }


@pure
def is_skill_invocation(tool_name: str, tool_input: str) -> bool:
    """Whether one tool call, as the feed records it, reached for `SKILL_NAME`.

    Two spellings, one per harness family: claude's own skill tool, which names the skill in its
    input, and any other call whose input opens the skill's file, which is how a harness without
    such a tool loads one. Read from the feed alone, never from the trajectory reader this is the
    compliance record for.
    """
    return SKILL_FILE_PATH in tool_input or (tool_name == SKILL_TOOL_NAME and SKILL_NAME in tool_input)


@pure
def feed_tool_input_by_call_id(
    detail_by_event_id: Mapping[str, Mapping[str, Any] | None],
) -> dict[str, str]:
    """Each tool call's full input text, from the feed's per-event detail payloads."""
    input_by_call_id: dict[str, str] = {}
    for detail in detail_by_event_id.values():
        raw_inputs = detail.get("inputs_by_tool_call_id") if isinstance(detail, Mapping) else None
        if isinstance(raw_inputs, Mapping):
            input_by_call_id.update({str(key): str(value) for key, value in raw_inputs.items()})
    return input_by_call_id


@pure
def feed_tk_stamps(events: Sequence[Mapping[str, Any]]) -> list[str]:
    """Every tool result's `tk_stamp`, in feed order."""
    return [
        str(event["tk_stamp"])
        for event in events
        if event.get("type") == "tool_result" and isinstance(event.get("tk_stamp"), str)
    ]


@pure
def step_ids_by_title(texts: Sequence[str], titles: Sequence[str]) -> list[str]:
    """The sorted step ids that `tk` output in the given texts names under one of the given titles."""
    wanted_titles = {title.strip() for title in titles}
    return sorted(
        {
            match.group("id")
            for text in texts
            for pattern in (_CREATED_LINE_PATTERN, _STEP_TITLE_LINE_PATTERN)
            for match in pattern.finditer(text)
            if match.group("title").strip() in wanted_titles
        }
    )
