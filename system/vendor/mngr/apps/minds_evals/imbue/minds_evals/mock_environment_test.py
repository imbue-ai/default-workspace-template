"""A concrete scripted BaseEnvironment implementation for driver/bridge unit tests: commands are
matched against ordered substring rules, each yielding a sequence of canned ExecResults (the last
one repeats). An optional ConversationModel additionally serves the workspace system_interface's
stateful message/events/agents endpoints so the driver's turn loop can be exercised end to end."""

import json
import re
from pathlib import Path

from harbor.environments.base import BaseEnvironment
from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths


class ScriptedExecRule:
    """One substring-matched rule with a sequence of canned results (the last repeats)."""

    def __init__(self, substring: str, results: list[ExecResult]) -> None:
        assert results, "a scripted rule needs at least one result"
        self.substring = substring
        self.results = results
        self.hit_count = 0

    def next_result(self) -> ExecResult:
        result = self.results[min(self.hit_count, len(self.results) - 1)]
        self.hit_count += 1
        return result


def ok_result(stdout: str = "") -> ExecResult:
    return ExecResult(stdout=stdout, stderr="", return_code=0)


def failed_result(stderr: str = "boom") -> ExecResult:
    return ExecResult(stdout="", stderr=stderr, return_code=1)


def mngr_exec_json(stdout: str) -> str:
    """The envelope `mngr exec --format json` prints for a single-agent command."""
    return json.dumps({"results": [{"agent": "ws-1", "stdout": stdout, "stderr": "", "success": True}]})


class ConversationModel:
    """A stateful model of the workspace chat agent for the bridged system_interface calls.

    Starts with ``pre_events`` (e.g. a /welcome exchange) already present; each POST to
    ``/message`` appends that turn's ``turn_reply_events`` (the events the agent produces in
    response), so the incremental events poll first sees the new reply only after the send. The
    agent state is always WAITING (the driver polls the reply itself, not the state edge)."""

    def __init__(
        self,
        chat_agent_id: str,
        chat_agent_name: str,
        pre_events: list[dict],
        turn_reply_events: list[list[dict]],
        trailing_events: list[dict] | None = None,
    ) -> None:
        self.chat_agent_id = chat_agent_id
        self.chat_agent_name = chat_agent_name
        self.events: list[dict] = list(pre_events)
        self._turn_reply_events = turn_reply_events
        self._turn_index = 0
        # Work the agent does *after* reporting WAITING on the final turn -- the workspace's own
        # turn-end flow behaves this way. Appended when the state is queried and every turn has
        # already replied, which is exactly the window in which the driver could stop reading.
        self._trailing_events = list(trailing_events or [])
        # The workspace sign-in surface: whether the auth endpoint answers at all, and the raw
        # submit commands, so tests can assert what the driver posted.
        self.is_auth_endpoint_up = True
        self.submitted_credential_commands: list[str] = []
        # What the workspace reports after a submit; a mode other than the one the driver asked for
        # is how a bad credential shows up, since the endpoint itself never validates.
        self.expected_auth_mode = "api_key"

    def handle(self, command: str) -> str | None:
        """Return the curl-body stdout for a system_interface call, or None if this command is not
        one (so the caller falls back to scripted rules)."""
        if "/api/claude-auth/submit-credentials" in command:
            self.submitted_credential_commands.append(command)
            return mngr_exec_json(json.dumps({"logged_in": True, "auth_mode": self.expected_auth_mode}))
        if "/api/claude-auth/status" in command:
            if not self.is_auth_endpoint_up:
                # An unparseable body is what the bridge sees while the endpoint is still coming up.
                return mngr_exec_json("")
            return mngr_exec_json(json.dumps({"logged_in": False, "auth_mode": "none"}))
        if "/api/agents/{}/message".format(self.chat_agent_id) in command:
            if self._turn_index < len(self._turn_reply_events):
                self.events.extend(self._turn_reply_events[self._turn_index])
                self._turn_index += 1
            return mngr_exec_json(json.dumps({"ok": True}))
        events_match = re.search(
            r"/api/agents/{}/events\?offset=(\d+)&limit=(\d+)".format(re.escape(self.chat_agent_id)), command
        )
        if events_match:
            offset, limit = int(events_match.group(1)), int(events_match.group(2))
            body = {"total": len(self.events), "events": self.events[offset : offset + limit]}
            return mngr_exec_json(json.dumps(body))
        if "/api/agents" in command and "curl" in command:
            if self._trailing_events and self._turn_index >= len(self._turn_reply_events):
                self.events.extend(self._trailing_events)
                self._trailing_events = []
            body = {
                "agents": [
                    {"id": "sys-1", "name": "system-services", "state": "WAITING"},
                    {"id": self.chat_agent_id, "name": self.chat_agent_name, "state": "WAITING"},
                ]
            }
            return mngr_exec_json(json.dumps(body))
        return None


class MockBoxEnvironment(BaseEnvironment):
    """Scripted in-memory box environment; records exec commands, env, and uploads."""

    def __init__(
        self,
        tmp_path: Path,
        rules: list[ScriptedExecRule],
        conversation: ConversationModel | None = None,
    ) -> None:
        trial_dir = tmp_path / "trial"
        trial_dir.mkdir(parents=True, exist_ok=True)
        environment_dir = tmp_path / "environment"
        environment_dir.mkdir(parents=True, exist_ok=True)
        super().__init__(
            environment_dir=environment_dir,
            environment_name="mock-box",
            session_id="mock-box__test__env",
            trial_paths=TrialPaths(trial_dir=trial_dir),
            task_env_config=EnvironmentConfig(),
        )
        self.rules = rules
        self.conversation = conversation
        self.exec_commands: list[str] = []
        self.exec_envs: list[dict[str, str] | None] = []
        self.uploaded_content_by_target: dict[str, str] = {}

    @staticmethod
    def type() -> str:
        return "mock"

    def _validate_definition(self) -> None:
        pass

    async def start(self, force_build: bool) -> None:
        pass

    async def stop(self, delete: bool) -> None:
        pass

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        self.uploaded_content_by_target[target_path] = Path(source_path).read_text()

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        pass

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        pass

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        pass

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        self.exec_commands.append(command)
        self.exec_envs.append(env)
        if self.conversation is not None:
            handled = self.conversation.handle(command)
            if handled is not None:
                return ok_result(handled)
        for rule in self.rules:
            if rule.substring in command:
                return rule.next_result()
        return ok_result()
