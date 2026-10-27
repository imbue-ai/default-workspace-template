"""A stand-in for the workspace's agents, for the ``run-status stop-chats`` / ``resume-chats`` tests.

A fake ``mngr`` on PATH keeps the agents in a JSON file: ``list`` renders the ``--format``
template it is given against them, and ``stop`` / ``start`` change an agent's state, so a
listing after a pause shows what the pause did. A recording ``message_chat.py`` in the tree
takes the resume notes. Every call is appended to one JSON-lines record.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

_FAKE_MNGR = """\
#!/usr/bin/env python3
import json, re, sys
state_path, record_path, failures_path = {state!r}, {record!r}, {failures!r}
argv = sys.argv[1:]
with open(record_path, "a") as handle:
    handle.write(json.dumps({{"tool": "mngr", "argv": argv}}) + "\\n")
agents = json.load(open(state_path))
failures = json.load(open(failures_path))
if argv[0] == "list":
    template = argv[argv.index("--format") + 1]
    def field(agent, name):
        if name.startswith("labels."):
            return agent["labels"].get(name[len("labels."):], "")
        return agent[name]
    for agent in agents:
        print(re.sub(r"{{([^}}]+)}}", lambda match: field(agent, match.group(1)), template))
    sys.exit(1 if "list" in failures else 0)
if argv[0] in ("stop", "start"):
    target = argv[1]
    if target in failures.get(argv[0], []):
        print(f"cannot {{argv[0]}} {{target}}", file=sys.stderr)
        sys.exit(1)
    for agent in agents:
        if target in (agent["id"], agent["name"]):
            agent["state"] = "STOPPED" if argv[0] == "stop" else "WAITING"
    json.dump(agents, open(state_path, "w"))
    sys.exit(0)
sys.exit(1)
"""

_FAKE_MESSAGE_CHAT = """\
#!/usr/bin/env python3
import json, sys
record_path, failures_path = {record!r}, {failures!r}
argv = sys.argv[1:]
text = open(argv[argv.index("--message-file") + 1]).read()
with open(record_path, "a") as handle:
    handle.write(json.dumps({{"tool": "message_chat", "chat_id": argv[0], "text": text}}) + "\\n")
failures = json.load(open(failures_path))
sys.exit(8 if argv[0] in failures.get("gone", []) else 1 if argv[0] in failures.get("message", []) else 0)
"""


def chat(agent_id: str, name: str, state: str, **labels: str) -> dict[str, Any]:
    """A chat agent as the chat app creates it."""
    return {
        "id": agent_id,
        "name": name,
        "state": state,
        "labels": {"user_created": "true", **labels},
    }


def worker(agent_id: str, name: str, state: str, lead_agent: str) -> dict[str, Any]:
    """A background worker as ``create_worker.py launch`` creates it."""
    return {
        "id": agent_id,
        "name": name,
        "state": state,
        "labels": {"agent_created": "true", "lead_agent": lead_agent},
    }


class FakeAgents:
    def __init__(self, bin_dir: Path, tree: Path, workdir: Path) -> None:
        self._state = workdir / "fake-agents.json"
        self._record = workdir / "fake-agent-calls.jsonl"
        self._failures = workdir / "fake-agent-failures.json"
        self.set_agents([])
        self.fail({})
        self._write_script(
            bin_dir / "mngr",
            _FAKE_MNGR.format(
                state=str(self._state),
                record=str(self._record),
                failures=str(self._failures),
            ),
        )
        self._write_script(
            tree / "system" / "scripts" / "message_chat.py",
            _FAKE_MESSAGE_CHAT.format(
                record=str(self._record), failures=str(self._failures)
            ),
        )

    def set_agents(self, agents: list[dict[str, Any]]) -> None:
        self._state.write_text(json.dumps(agents))

    def fail(self, failures: dict[str, list[str]]) -> None:
        """Make ``mngr stop``/``start`` fail for these agent ids, or a note to these chat ids
        (``{"stop": [...], "start": [...], "message": [...]}``), or answer a note to these
        chat ids as sent to a chat that no longer exists (``"gone"``); a ``list`` key makes the
        listing exit non-zero after printing every agent, as one provider's failure does."""
        self._failures.write_text(json.dumps(failures))

    def states(self) -> dict[str, str]:
        return {
            agent["name"]: agent["state"]
            for agent in json.loads(self._state.read_text())
        }

    def calls(self) -> list[dict[str, Any]]:
        if not self._record.exists():
            return []
        return [json.loads(line) for line in self._record.read_text().splitlines()]

    def mngr_verbs(self) -> list[list[str]]:
        """The ``mngr`` calls other than listings, as argv."""
        return [
            call["argv"]
            for call in self.calls()
            if call["tool"] == "mngr" and call["argv"][0] != "list"
        ]

    def notes(self) -> dict[str, str]:
        """The note each chat was sent, by chat id."""
        return {
            call["chat_id"]: call["text"]
            for call in self.calls()
            if call["tool"] == "message_chat"
        }

    def clear_calls(self) -> None:
        self._record.unlink(missing_ok=True)

    @staticmethod
    def _write_script(path: Path, body: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
