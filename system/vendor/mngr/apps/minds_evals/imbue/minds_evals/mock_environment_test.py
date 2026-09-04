"""A concrete scripted BaseEnvironment implementation for driver/bridge unit tests: commands are
matched against ordered substring rules, each yielding a sequence of canned ExecResults (the last
one repeats). An optional ConversationModel additionally serves the workspace system_interface's
stateful sign-in, chat-creation, message, events, and agents endpoints, so the driver's whole
bring-up and turn loop can be exercised end to end."""

import asyncio
import json
import re
from pathlib import Path
from typing import Final

from harbor.environments.base import BaseEnvironment
from harbor.environments.base import ExecResult
from harbor.models.task.config import EnvironmentConfig
from harbor.models.trial.paths import TrialPaths

from imbue.minds_evals.errors import BoxCommandError


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


def curl_stdout(body: str, status: int = 200) -> str:
    """What `curl -w '\\n%{http_code}'` prints for one workspace call, wrapped in the mngr exec
    envelope the bridge parses."""
    return mngr_exec_json("{}\n{}".format(body, status))


# The account the mock workspace's sign-in endpoint mints; a chat created against anything else is
# bound to an account that does not exist.
MOCK_ACCOUNT_ID: Final[str] = "acct-mock-1"

# The exchange the workspace's first chat is given on creation. Every first chat gets it, so it is
# not a per-test choice: the `first` create template carries `/welcome` as the chat's initial
# message, and the workspace types it in once the agent is up.
WELCOME_EXCHANGE: Final[tuple[dict, ...]] = (
    {"type": "user_message", "content": "/welcome"},
    {"type": "assistant_message", "text": "Hi! Tell me what you would like to build."},
)


class ConversationModel:
    """A stateful model of the workspace's chat surface for the bridged system_interface calls.

    A workspace boots with no chat: the agents listing carries only ``system-services`` until a
    ``create-chat`` call makes one. That chat is listed, and reports WAITING, before its
    ``WELCOME_EXCHANGE`` has been delivered -- the workspace types `/welcome` in only once the agent
    is up, so ``welcome_delay_polls`` polls of the events endpoint see a chat that is WAITING and
    carries no messages at all. Each POST to ``/message`` appends that turn's
    ``turn_reply_events``, so the incremental events poll sees a new reply only after the send.

    A chat binds to a provider account at creation and only the sign-in endpoint mints one, so a
    create issued before sign-in is refused for want of an account -- the product behaviour that
    makes the driver's ordering load-bearing.

    ``box_lost_after_turn`` makes the box go away the moment that turn's events have been read:
    the workspace said something, the reader took it, and then the sandbox holding all of it
    vanished before the turn could finish. Every later command raises rather than answering, which
    is the difference between a box the host has lost and a workspace that merely refuses.
    """

    def __init__(
        self,
        chat_agent_id: str,
        turn_reply_events: list[list[dict]],
        trailing_events: list[dict] | None = None,
        is_first_create_answer_lost: bool = False,
        welcome_delay_polls: int = 0,
        box_lost_after_turn: int | None = None,
    ) -> None:
        self.chat_agent_id = chat_agent_id
        # The name the chat is listed under, which it only has once a create has given it one.
        self.chat_agent_name = ""
        self.events: list[dict] = []
        self._welcome_delay_polls = welcome_delay_polls
        self._is_welcome_delivered = False
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
        # Whether the workspace's agents endpoints answer at all. A workspace that was created but
        # never became usable answers nothing on any of them -- creating a chat included, not just
        # listing them -- so the trial never gets a chat to drive.
        self.is_agents_endpoint_up = True
        # The 1-based client message the message endpoint refuses to accept, if any. The driver
        # retries a send until its deadline, so this is how a send that never lands is exercised.
        self.refused_send_index: int | None = None
        self.signed_in_account_ids: list[str] = []
        # The chat, once one exists: the create-chat calls made, and the account it bound to.
        self.create_chat_commands: list[str] = []
        self.chat_account_id: str = ""
        self.is_chat_created = False
        # A create whose answer never came back, though the chat was made: the caller sees nothing
        # and asks again, and the second ask collides with the chat the first one left behind.
        self._is_first_create_answer_lost = is_first_create_answer_lost
        # Set to make every create refused, the way a workspace refuses one it cannot satisfy.
        self.create_chat_refusal_detail = ""
        # The state the created chat reports; a chat that never reaches WAITING is one the driver
        # can never send a turn to.
        self.chat_state = "WAITING"
        # The 1-based client message after which the box goes, or None for a box that stays.
        self._box_lost_after_turn = box_lost_after_turn
        # Whether the box has gone away; MockBoxEnvironment raises on every command once it has.
        self.is_box_lost = False

    def _deliver_welcome_if_due(self) -> None:
        """The welcome the workspace gives its first chat, once it is due."""
        if self._is_welcome_delivered or not self.is_chat_created:
            return
        if self._welcome_delay_polls > 0:
            self._welcome_delay_polls -= 1
            return
        self.events.extend(WELCOME_EXCHANGE)
        self._is_welcome_delivered = True

    def _agents_listing(self) -> list[dict]:
        # The listing carries the canonical name and the state, and no labels: labels reach clients
        # over the workspace's WebSocket, which this bridge has no way to read.
        agents: list[dict] = [{"id": "sys-1", "name": "system-services", "state": "WAITING"}]
        if self.is_chat_created:
            agents.append({"id": self.chat_agent_id, "name": self.chat_agent_name, "state": self.chat_state})
        return agents

    def _handle_create_chat(self, command: str) -> str:
        self.create_chat_commands.append(command)
        # The create body, picked out of a command that also carries curl's own `%{http_code}`.
        body_match = re.search(r'\{"name"[^{}]*\}', command)
        assert body_match is not None, "a create-chat call carries a JSON body naming the chat"
        payload = json.loads(body_match.group(0))
        requested_name = str(payload.get("name") or "")
        if self.create_chat_refusal_detail:
            return curl_stdout(json.dumps({"detail": self.create_chat_refusal_detail}), status=400)
        if self.is_chat_created:
            return curl_stdout(
                json.dumps({"detail": "A chat named '{}' already exists".format(requested_name)}), status=409
            )
        requested_account_id = str(payload.get("account_id") or "")
        # A chat runs on the account it binds to, and only the sign-in endpoint mints one: with no
        # account to bind to, the workspace refuses the create rather than making a chat that could
        # never take a turn.
        if requested_account_id and requested_account_id not in self.signed_in_account_ids:
            return curl_stdout(json.dumps({"detail": "no account {}".format(requested_account_id)}), status=400)
        if not requested_account_id and not self.signed_in_account_ids:
            return curl_stdout(json.dumps({"detail": "no provider accounts exist yet"}), status=400)
        self.chat_account_id = requested_account_id or self.signed_in_account_ids[-1]
        # The workspace lists a chat under the true name it derives from the requested one.
        self.chat_agent_name = requested_name.replace(" ", "-")
        self.is_chat_created = True
        if self._is_first_create_answer_lost:
            self._is_first_create_answer_lost = False
            return mngr_exec_json("")
        created = {"agent_id": self.chat_agent_id, "name": self.chat_agent_name, "display_name": requested_name}
        return curl_stdout(json.dumps(created), status=201)

    def handle(self, command: str) -> str | None:
        """Return the curl-body stdout for a system_interface call, or None if this command is not
        one (so the caller falls back to scripted rules)."""
        if "/api/claude-auth/submit-credentials" in command:
            self.submitted_credential_commands.append(command)
            # An account exists only where the workspace really ended up in an authenticated mode.
            if self.expected_auth_mode != "none":
                self.signed_in_account_ids.append(MOCK_ACCOUNT_ID)
            signed_in = {
                "account_id": MOCK_ACCOUNT_ID,
                "display": "eval",
                "logged_in": True,
                "auth_mode": self.expected_auth_mode,
            }
            return curl_stdout(json.dumps(signed_in))
        if "/api/claude-auth/status" in command:
            if not self.is_auth_endpoint_up:
                # No status line at all is what the bridge sees while the endpoint is still coming up.
                return mngr_exec_json("")
            return curl_stdout(json.dumps({"logged_in": False, "auth_mode": "none"}))
        if "/api/agents" in command and not self.is_agents_endpoint_up:
            # No status line at all: the bridged call reached nothing that could answer it.
            return mngr_exec_json("")
        if "/api/agents/create-chat" in command:
            return self._handle_create_chat(command)
        if "/api/agents/{}/message".format(self.chat_agent_id) in command:
            if self.refused_send_index == self._turn_index + 1:
                # An unparseable body is what the bridge sees when a send does not land.
                return mngr_exec_json("")
            # The welcome turn is queued ahead of anything sent afterwards, so a send that beats it
            # is answered behind it rather than instead of it.
            self._welcome_delay_polls = 0
            self._deliver_welcome_if_due()
            if self._turn_index < len(self._turn_reply_events):
                self.events.extend(self._turn_reply_events[self._turn_index])
                self._turn_index += 1
            return curl_stdout(json.dumps({"ok": True}))
        events_match = re.search(
            r"/api/agents/{}/events\?offset=(\d+)&limit=(\d+)".format(re.escape(self.chat_agent_id)), command
        )
        if events_match:
            self._deliver_welcome_if_due()
            offset, limit = int(events_match.group(1)), int(events_match.group(2))
            served = self.events[offset : offset + limit]
            # Armed only once a sent turn's events have all been handed over, so this answer is the
            # last one the reader gets: the events reach it, and the box is gone by its next call.
            if (
                self._box_lost_after_turn is not None
                and self._turn_index >= self._box_lost_after_turn
                and served
                and offset + len(served) == len(self.events)
            ):
                self.is_box_lost = True
            return curl_stdout(json.dumps({"total": len(self.events), "events": served}))
        if "/api/agents" in command and "curl" in command:
            if self._trailing_events and self._turn_index >= len(self._turn_reply_events):
                self.events.extend(self._trailing_events)
                self._trailing_events = []
            return curl_stdout(json.dumps({"agents": self._agents_listing()}))
        return None


class MockBoxEnvironment(BaseEnvironment):
    """Scripted in-memory box environment; records exec commands, env, and uploads."""

    def __init__(
        self,
        tmp_path: Path,
        rules: list[ScriptedExecRule],
        conversation: ConversationModel | None = None,
        raising_substrings: tuple[str, ...] = (),
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
        # Commands whose exec raises rather than answering. A canned ExecResult can only express a
        # command that ran and failed; this is the transport itself failing, which is what a box the
        # host has lost contact with looks like.
        self.raising_substrings = raising_substrings
        self.exec_commands: list[str] = []
        self.exec_envs: list[dict[str, str] | None] = []
        self.uploaded_content_by_target: dict[str, str] = {}
        # Every upload in order. The mapping above keeps only the latest write to each path, which
        # cannot say whether a later step wrote its own copy of a file an earlier one already had.
        self.uploaded_targets: list[str] = []
        # What a `download_file` of a box path yields; a path not listed here is a missing file.
        self.downloadable_content_by_source: dict[str, str] = {}
        # An upload whose content contains this fails the way a transport hiccup does; empty means
        # every upload lands.
        self.rejected_upload_content_substring: str = ""

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
        content = Path(source_path).read_text()
        if self.rejected_upload_content_substring and self.rejected_upload_content_substring in content:
            raise RuntimeError("upload of {} failed".format(target_path))
        self.uploaded_content_by_target[target_path] = content
        self.uploaded_targets.append(target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        pass

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if source_path not in self.downloadable_content_by_source:
            raise FileNotFoundError(source_path)
        Path(target_path).write_text(self.downloadable_content_by_source[source_path])

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        prefix = source_dir.rstrip("/") + "/"
        matching = {
            key: content for key, content in self.downloadable_content_by_source.items() if key.startswith(prefix)
        }
        if not matching:
            raise FileNotFoundError(source_dir)
        for key, content in matching.items():
            path = Path(target_dir) / key[len(prefix) :]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)

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
        # A real exec is a network round trip and always yields; without this every scripted answer
        # is instant and a test of two concurrent drivers would never actually interleave them.
        await asyncio.sleep(0)
        for substring in self.raising_substrings:
            if substring in command:
                raise BoxCommandError("mock transport failure for {!r}".format(substring))
        if self.conversation is not None and self.conversation.is_box_lost:
            raise BoxCommandError("the box is gone; nothing in it can be reached")
        if self.conversation is not None:
            handled = self.conversation.handle(command)
            if handled is not None:
                return ok_result(handled)
        for rule in self.rules:
            if rule.substring in command:
                return rule.next_result()
        return ok_result()
