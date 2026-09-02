"""Helpers that reach the box's Minds HTTP API and the workspace's system_interface through
``environment.exec`` (ported from the old harness's minds_client).

Everything here runs commands inside the harbor environment (the box) or, bridged one level deeper
via ``mngr exec``, inside the trial's nested workspace sandbox. The functions are async because
harbor's environment API is async; this module and driver.py are the only async code in the app.
"""

import asyncio
import json
import re
import shlex
import time
import tomllib
from http import HTTPStatus
from importlib import resources
from pathlib import Path
from typing import Any
from typing import Final

from harbor.environments.base import BaseEnvironment
from harbor.environments.base import ExecResult
from loguru import logger
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.minds_evals.errors import BoxCommandError
from imbue.minds_evals.errors import WorkspaceCreateError

BOX_MNGR_DIR: Final[str] = "/work/mngr"
BOX_LOGS_DIR: Final[str] = "/logs/agent"
# Scripts this app runs inside the box, shipped in the package and uploaded per trial. They are not
# baked into the box image because it is layer-cached per mngr SHA and has to stay byte-identical
# across a dataset, so changing them would cost a rebuild -- and because the image is built from the
# pinned mngr SHA, which predates them.
_RESOURCES = resources.files("imbue.minds_evals") / "resources"
BOX_REVERSE_TUNNEL_FILENAME: Final[str] = "box_reverse_tunnel.py"
BOX_REVERSE_TUNNEL_PATH: Final[str] = "/tmp/box_reverse_tunnel.py"
BOX_PROXY_HOOKS_FILENAME: Final[str] = "box_proxy_hooks.py"
BOX_FLOW_STEP_FILENAME: Final[str] = "box_flow_step.py"
# The request and result models both sides share. It rides into the box beside the step script
# and is imported by it as a plain module, so the two files must land in the same directory.
BOX_FLOW_PROTOCOL_FILENAME: Final[str] = "flow_step_protocol.py"
BOX_PROXY_DIR: Final[str] = "/tmp/eval_proxy"
PROXY_CONFIG_FILENAME: Final[str] = "proxy_config.yaml"
BOX_PROXY_USAGE_LOG_PATH: Final[str] = "/tmp/eval_proxy/usage_proxy.jsonl"
TUNNEL_LOG_FILENAME: Final[str] = "reverse_tunnel.log"
PROXY_LOG_FILENAME: Final[str] = "proxy.log"
# Read by the uploaded hooks; named here so the two sides cannot drift apart.
PROXY_KEY_ENV_VAR: Final[str] = "MINDS_EVAL_PROXY_KEY"
PROXY_USAGE_LOG_ENV_VAR: Final[str] = "MINDS_EVAL_PROXY_USAGE_LOG"
# The workspace's own system_interface. Loopback, so it is reachable only from
# inside the workspace sandbox, which is why it needs no authentication.
WORKSPACE_SYSTEM_INTERFACE: Final[str] = "http://127.0.0.1:8000"

# The workspace's own claude sign-in API -- the endpoints the product's in-UI login modal posts to.
# Authenticating through these rather than through the create-time host env keeps the workspace in
# the same shared-config regime real workspaces run in.
CLAUDE_AUTH_STATUS_PATH: Final[str] = "/api/claude-auth/status"
CLAUDE_AUTH_SUBMIT_PATH: Final[str] = "/api/claude-auth/submit-credentials"
# The endpoint the product's new-tab screen posts to when a user starts a chat. A workspace boots
# with no chat at all, so the driver's own chat is made here.
CREATE_CHAT_PATH: Final[str] = "/api/agents/create-chat"
AGENTS_PATH: Final[str] = "/api/agents"
ANTHROPIC_API_KEY_ENV_VAR: Final[str] = "ANTHROPIC_API_KEY"
ANTHROPIC_BASE_URL_ENV_VAR: Final[str] = "ANTHROPIC_BASE_URL"
# The auth mode the workspace derives from which credential keys it was given: a key on its own is
# "api_key", a key plus a base URL (the proxy form) is "imbue".
AUTH_MODE_API_KEY: Final[str] = "api_key"
AUTH_MODE_IMBUE: Final[str] = "imbue"

_QUICK_EXEC_TIMEOUT_SECONDS: Final[int] = 180
_SLOW_EXEC_TIMEOUT_SECONDS: Final[int] = 900

# Providers other than Modal are unusable in the box; exec'd mngr commands need
# the same disables the entrypoint exports for the backend.
_DISABLED_PROVIDERS: Final[tuple[str, ...]] = (
    "DOCKER",
    "AZURE",
    "AWS",
    "VULTR",
    "LIMA",
    "IMBUE_CLOUD",
    "GCP",
    "OVH",
)


def default_modal_config_path() -> Path:
    return Path.home() / ".modal.toml"


def load_modal_token_env(config_path: Path) -> dict[str, str]:
    """MODAL_TOKEN_ID/SECRET from the host's ~/.modal.toml (the active profile), for the box's own
    mngr to create workspaces on Modal from inside the sandbox."""
    if not config_path.is_file():
        raise BoxCommandError("missing {} (Modal auth) -- everything runs on Modal".format(config_path))
    profiles = tomllib.loads(config_path.read_text())
    token_profiles = [
        profile for profile in profiles.values() if isinstance(profile, dict) and profile.get("token_id")
    ]
    if not token_profiles:
        raise BoxCommandError("no token in ~/.modal.toml -- run `modal token new`")
    # A profile marked active wins (the last one, if several claim it); otherwise
    # the first profile that carries a token is used.
    active_profiles = [profile for profile in token_profiles if profile.get("active")]
    active = active_profiles[-1] if active_profiles else token_profiles[0]
    if not active.get("token_secret"):
        raise BoxCommandError("~/.modal.toml profile has a token_id but no token_secret -- run `modal token new`")
    return {"MODAL_TOKEN_ID": str(active["token_id"]), "MODAL_TOKEN_SECRET": str(active["token_secret"])}


@pure
def parse_activation_exports(activation_script: str) -> dict[str, str]:
    """Parse the `export KEY=VALUE` lines out of `minds-admin env activate` output (a shell snippet meant
    for `eval`); `unset` lines and comments are ignored -- we build the exec env from scratch, so
    an unset variable is simply never set."""
    exports: dict[str, str] = {}
    for raw_line in activation_script.splitlines():
        line = raw_line.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export ") :]
        key, separator, raw_value = assignment.partition("=")
        if not separator or not key.strip():
            continue
        tokens = shlex.split(raw_value)
        exports[key.strip()] = tokens[0] if tokens else ""
    return exports


async def fetch_minds_activation_env(environment: BaseEnvironment, minds_env: str) -> dict[str, str]:
    """The env vars `minds-admin env activate` exports (MNGR_PREFIX, MNGR_HOST_DIR, ...). Bridge execs do
    not go through the entrypoint, so without these mngr's modal provider computes the wrong
    environment name and silently sees no workspaces -- fail fast if the critical vars are absent."""
    result = await check_run_in_box(
        environment,
        "cd {} && uv run minds-admin env activate {}".format(BOX_MNGR_DIR, shlex.quote(minds_env)),
        {"MINDS_ENV": minds_env},
        _QUICK_EXEC_TIMEOUT_SECONDS,
    )
    activation_env = parse_activation_exports(result.stdout or "")
    for required_key in ("MNGR_HOST_DIR", "MNGR_PREFIX"):
        if not activation_env.get(required_key):
            raise BoxCommandError(
                "minds-admin env activate did not export {} (got: {}) -- bridge mngr commands would "
                "silently see no workspaces".format(required_key, sorted(activation_env))
            )
    return activation_env


@pure
def build_box_env(
    *,
    activation_env: dict[str, str],
    modal_token_env: dict[str, str],
    user_id: str,
    mngr_sha: str,
    minds_env: str,
) -> dict[str, str]:
    """The env for the backend-start exec and every bridge exec: the minds activation exports (so
    exec'd mngr commands resolve the same Modal environment the backend uses), Modal auth, the
    per-trial Modal user-id scope, and the provider disables the entrypoint would set."""
    env: dict[str, str] = dict(activation_env)
    env.update(
        {
            "MINDS_ENV": minds_env,
            "MNGR__PROVIDERS__MODAL__USER_ID": user_id,
            "MINDS_BOX_MNGR_REF": mngr_sha,
            # Every workspace this box creates is an eval workspace: stack the
            # modal_eval overlay (shorter 3h sandbox timeout) on the modal template.
            "MINDS_MODAL_EXTRA_TEMPLATE": "modal_eval",
            "SKIP_AUTH": "1",
        }
    )
    for provider in _DISABLED_PROVIDERS:
        env["MNGR__PROVIDERS__{}__IS_ENABLED".format(provider)] = "false"
    env.update(modal_token_env)
    # No AI credentials here on purpose. The workspace is authenticated after create, through the
    # same /api/claude-auth/submit-credentials endpoint the product's own sign-in modal posts to --
    # see authenticate_workspace. Injecting a key into the host env at create time instead would put
    # the workspace in a regime production never enters: dwt's auth module states credentials must
    # never go in the mngr host env file, because that file is frozen into supervisord and its
    # services at boot, and the host-env route was deliberately removed from the product.
    return env


async def run_in_box(
    environment: BaseEnvironment,
    command: str,
    env: dict[str, str],
    timeout_sec: int,
) -> ExecResult:
    return await environment.exec(command, env=env, timeout_sec=timeout_sec)


async def check_run_in_box(
    environment: BaseEnvironment,
    command: str,
    env: dict[str, str],
    timeout_sec: int,
) -> ExecResult:
    result = await run_in_box(environment, command, env, timeout_sec)
    if result.return_code != 0:
        detail = (result.stderr or result.stdout or "no output").strip()[:500]
        raise BoxCommandError("box command failed (rc={}): {} -- {}".format(result.return_code, command[:200], detail))
    return result


async def read_box_mngr_sha(environment: BaseEnvironment) -> str:
    """The exact mngr SHA the box image was built from (stamped into the image by the generator;
    the staged clone carries no .git because Modal's build-context upload drops it)."""
    result = await environment.exec("cat /work/mngr_sha", timeout_sec=_QUICK_EXEC_TIMEOUT_SECONDS)
    sha = (result.stdout or "").strip()
    if result.return_code != 0 or not sha:
        raise BoxCommandError("could not read the box mngr SHA: {}".format((result.stderr or "").strip()[:300]))
    return sha


async def start_backend(environment: BaseEnvironment, env: dict[str, str]) -> None:
    """Start the Minds desktop stack in the background with the per-trial env; everything the
    backend spawns (including each `mngr create`) inherits it."""
    await check_run_in_box(
        environment,
        "mkdir -p {logs} && setsid nohup /usr/local/bin/entrypoint.sh > {logs}/box.log 2>&1 < /dev/null &".format(
            logs=BOX_LOGS_DIR
        ),
        env,
        _QUICK_EXEC_TIMEOUT_SECONDS,
    )


async def discover_api_port(environment: BaseEnvironment, env: dict[str, str], timeout_seconds: float) -> str:
    """Find the Minds backend's API port from inside the box (the probe script polls in-box)."""
    result = await run_in_box(
        environment,
        "cd {} && uv run python /usr/local/bin/probe_minds_port.py {}".format(BOX_MNGR_DIR, timeout_seconds),
        env,
        int(timeout_seconds) + 60,
    )
    output_lines = (result.stdout or "").strip().splitlines()
    port = output_lines[-1].strip() if output_lines else ""
    if result.return_code != 0 or not port.isdigit():
        raise BoxCommandError(
            "could not find the Minds API port (is the app still booting?): {}".format(
                (result.stderr or result.stdout or "").strip()[:300]
            )
        )
    return port


async def _box_curl_json(
    environment: BaseEnvironment,
    env: dict[str, str],
    method: str,
    url: str,
    body_json: str | None,
) -> tuple[int, dict[str, Any]]:
    """Issue an HTTP call against the box-local Minds API via curl-in-box; returns (status, body).
    Connection failures return status 0 so callers can treat them as transient."""
    parts = ["curl", "-s", "-w", "\\n%{http_code}", "-X", method, url]
    if body_json is not None:
        parts += ["-H", "Content-Type: application/json", "-d", body_json]
    command = " ".join(shlex.quote(part) for part in parts)
    result = await run_in_box(environment, command, env, _QUICK_EXEC_TIMEOUT_SECONDS)
    output = (result.stdout or "").strip()
    if result.return_code != 0:
        return 0, {"error": (result.stderr or output or "curl failed").strip()[:400]}
    response = parse_curl_response(output)
    if isinstance(response.body, dict):
        return response.status, response.body
    # A body this caller cannot read is reported through the "error" key its callers already
    # inspect; a capture that never carried a status reads back as status 0, which they poll on.
    return response.status, {"error": response.text[:400]} if response.text else {}


@pure
def build_create_payload(*, dwt_repo: str, dwt_branch: str, host_name: str) -> dict[str, str]:
    """The Minds create-form payload (ported from the old harness's workspace.build_payload).
    Workspaces are always Modal; backup is configure_later (no restic provisioning). Empty branch:
    a local clone is already on its commit, and passing a branch trips mngr's
    checkout_branch(FETCH_HEAD) on the use-in-place path."""
    payload = {
        "git_url": dwt_repo,
        "branch": dwt_branch,
        "launch_mode": "MODAL",
        "backup_provider": "CONFIGURE_LATER",
    }
    if host_name:
        payload["host_name"] = host_name
    return payload


async def create_workspace_and_wait(
    environment: BaseEnvironment,
    env: dict[str, str],
    port: str,
    payload: dict[str, str],
    deadline: float,
    poll_seconds: float,
) -> str:
    """POST a create request to the box-local Minds API and poll the operation until done; returns
    the new workspace's agent id."""
    base_url = "http://127.0.0.1:{}".format(port)
    status, body = await _box_curl_json(
        environment, env, "POST", "{}/api/v1/workspaces".format(base_url), json.dumps(payload)
    )
    if status != 202:
        raise WorkspaceCreateError("create failed HTTP {}: {}".format(status, body))
    operation_id = body.get("operation_id")
    if not operation_id:
        raise WorkspaceCreateError("create returned no operation_id: {}".format(body))

    last_stage = ""
    while time.time() < deadline:
        status, info = await _box_curl_json(
            environment, env, "GET", "{}/api/v1/workspaces/operations/create/{}".format(base_url, operation_id), None
        )
        if status == 0:
            # Transient blip (backend busy, connection dropped) -- keep polling.
            await asyncio.sleep(poll_seconds)
            continue
        stage = str(info.get("status_text") or info.get("status") or "")
        if stage and stage != last_stage:
            logger.debug("Workspace create stage: {}", stage)
            last_stage = stage
        # minds only surfaces the agent_id once the whole create is done (its
        # readiness probe included), so we wait for is_done.
        if info.get("is_done"):
            agent_id = info.get("agent_id")
            if not isinstance(agent_id, str):
                raise WorkspaceCreateError("create finished without an agent_id: {}".format(info))
            return agent_id
        if info.get("error"):
            raise WorkspaceCreateError(str(info["error"]))
        await asyncio.sleep(poll_seconds)
    raise WorkspaceCreateError("timed out waiting for workspace create")


@pure
def _mngr_exec_command(workspace_agent_id: str, inner_command: str, timeout_seconds: int) -> str:
    return "cd {} && uv run mngr exec {} {} --format json --timeout {}".format(
        BOX_MNGR_DIR, shlex.quote(workspace_agent_id), shlex.quote(inner_command), timeout_seconds
    )


async def run_in_workspace(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    inner_command: str,
    timeout_sec: int,
) -> tuple[bool, str]:
    """Run a shell command inside the nested workspace sandbox via mngr's remote-exec path; returns
    (success, stdout)."""
    command = _mngr_exec_command(workspace_agent_id, inner_command, timeout_sec)
    result = await run_in_box(environment, command, env, timeout_sec + 120)
    output = (result.stdout or "").strip()
    # mngr --format json emits one JSON object on stdout; tolerate leading noise.
    json_start = output.find("{")
    if json_start == -1:
        return False, (result.stderr or output or "").strip()
    try:
        parsed = json.loads(output[json_start:])
    except ValueError:
        return False, output
    results = parsed.get("results") or []
    if not results:
        failed = parsed.get("failed_agents") or []
        return False, json.dumps(failed)[:400]
    first = results[0]
    return bool(first.get("success")), str(first.get("stdout") or "")


class WorkspaceResponse(FrozenModel):
    """One bridged HTTP call to the workspace's system_interface."""

    # Status 0 is the one callers may retry -- the bridged exec failed, or the system_interface is
    # not listening yet. Every other status is the endpoint's own answer, refusals included.
    status: int = Field(description="The response status; 0 when the call never reached the endpoint")
    body: Any = Field(default=None, description="The parsed JSON body; None when there was none or it did not parse")
    # A body that did not parse came from somewhere other than the endpoint, whose own error shapes
    # are all JSON -- an unhandled traceback page, or something in front of the system_interface.
    # That is when a failed trial most needs the text, so it is kept rather than dropped.
    text: str = Field(default="", description="The raw body as captured, whether or not it parsed")

    @property
    def is_ok(self) -> bool:
        """Whether the endpoint answered yes. The whole 2xx range counts -- a create answers 201
        where a send answers 200 -- and everything else is the endpoint refusing, or, at 0, not
        having answered at all."""
        return 200 <= self.status < 300


@pure
def parse_curl_response(output: str) -> WorkspaceResponse:
    """Split what `curl -w '\\n%{http_code}'` printed into the status and the parsed JSON body."""
    head, _, status_line = output.strip().rpartition("\n")
    if not status_line.strip().isdigit():
        return WorkspaceResponse(status=0, text=output.strip())
    # curl prints 000 when it never got a response, which reads back as the same 0 a failed bridge
    # exec gives: both mean the call was never answered.
    status = int(status_line.strip())
    if not head.strip():
        return WorkspaceResponse(status=status)
    try:
        return WorkspaceResponse(status=status, body=json.loads(head), text=head)
    except ValueError:
        return WorkspaceResponse(status=status, text=head)


async def workspace_curl(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    url_path: str,
    body_json: str | None,
) -> WorkspaceResponse:
    """HTTP against the workspace-local system_interface, bridged through mngr exec, keeping the
    status: an endpoint that answers 4xx has to be told apart from one that is not up yet."""
    parts = ["curl", "-s", "--max-time", "30", "-w", "\\n%{http_code}"]
    if body_json is not None:
        parts += ["-X", "POST", "-H", "Content-Type: application/json", "-d", body_json]
    parts.append("{}{}".format(WORKSPACE_SYSTEM_INTERFACE, url_path))
    inner_command = " ".join(shlex.quote(part) for part in parts)
    is_success, stdout = await run_in_workspace(environment, env, workspace_agent_id, inner_command, 60)
    if not is_success:
        # Whatever the failed exec left behind: mngr's own failure detail when the bridge could not
        # run the command at all, or -- when curl ran and could not reach the endpoint -- curl's
        # write-out on its own, which reduces to no text. Never a status, since a curl that exited
        # non-zero may have printed one (a `--max-time` cut off mid-response) without the body that
        # goes with it.
        return WorkspaceResponse(status=0, text=parse_curl_response(stdout).text)
    return parse_curl_response(stdout)


async def workspace_curl_json(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    url_path: str,
    body_json: str | None,
) -> Any | None:
    """The parsed JSON body of a bridged system_interface call, or None when there was none -- what
    the pollers read, which treat any answer alike."""
    response = await workspace_curl(environment, env, workspace_agent_id, url_path, body_json)
    return response.body


# A chat wears two names: the display name it was created under ("Chat 2") and the canonical true
# name the workspace lists it as ("Chat-2"). The agents listing carries the canonical one only --
# labels, which is where the display name lives, reach clients over the workspace's WebSocket and
# not over this endpoint -- so a chat is matched on the key below rather than on either name.
_CANONICAL_STRIP_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9 _-]+")
_CANONICAL_SPACES_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


@pure
def _chat_name_key(name: str) -> str:
    """The key a workspace compares two chat names on, and the rule it refuses a colliding create
    on: everything but a safe-name character or a space is dropped, each run of spaces becomes one
    dash, the ends are stripped of dashes and underscores, and case is ignored.

    This mirrors the template's own ``_canonical_name_key`` (``canonical_agent_name`` plus the
    casefold); the two have to be changed together. It is not the name a chat is listed under --
    the true name is the canonical form alone, which keeps the case it was created with.
    """
    stripped = _CANONICAL_STRIP_PATTERN.sub("", name.strip())
    return _CANONICAL_SPACES_PATTERN.sub("-", stripped).strip("-_").casefold()


@pure
def resolve_chat_agent_id(agents: list[Any], display_name: str) -> str | None:
    """The id of the chat created under ``display_name`` in an `/api/agents` listing, or None when
    the listing carries no such chat yet."""
    wanted_key = _chat_name_key(display_name)
    for agent in agents:
        if not isinstance(agent, dict) or not agent.get("id"):
            continue
        if _chat_name_key(str(agent.get("name") or "")) == wanted_key:
            return str(agent["id"])
    return None


async def fetch_chat_agent_id(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    display_name: str,
    deadline: float,
    poll_seconds: float,
) -> str | None:
    """Poll the workspace's agents listing until it carries the chat created under ``display_name``.

    Only a workspace that already has that chat has one to find, and a create it is still working on
    is not listed yet, which is why this polls rather than reading once. A workspace that boots with
    no chat at all gets its chat from ``create_chat_agent``.
    """
    while time.time() < deadline:
        body = await workspace_curl_json(environment, env, workspace_agent_id, AGENTS_PATH, None)
        agents = body.get("agents") if isinstance(body, dict) else None
        if isinstance(agents, list):
            chat_agent_id = resolve_chat_agent_id(agents, display_name)
            if chat_agent_id is not None:
                return chat_agent_id
        await asyncio.sleep(poll_seconds)
    return None


async def create_chat_agent(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    display_name: str,
    account_id: str,
    deadline: float,
    poll_seconds: float,
) -> str | None:
    """Create the workspace's chat through the endpoint the product's new-tab screen posts to;
    returns its agent id.

    The screen sends no name and lets the workspace mint the next free "Chat N"; this names the
    chat itself, because the name is the only handle a create whose answer was lost can be
    recovered by -- see the collision branch below.

    A chat runs on the provider account it binds to at creation, so the workspace must already be
    signed in: with no account to bind to, the workspace refuses the create rather than making a
    chat that could never take a turn. An empty ``account_id`` leaves the choice to the workspace,
    which takes the account it used most recently, or its oldest one when that is no longer usable.

    Only a call that never reached the endpoint is retried, since that is the system_interface still
    coming up. A refusal is final -- except a name collision, which says a chat under that name is
    already there and is answered by resolving it from the listing.
    """
    # The account is left out rather than sent empty: absent and empty mean the same thing to the
    # endpoint, and a request carries no field it has no value for.
    request_body = {"name": display_name}
    if account_id:
        request_body["account_id"] = account_id
    payload = json.dumps(request_body)
    unanswered_detail = ""
    while time.time() < deadline:
        response = await workspace_curl(environment, env, workspace_agent_id, CREATE_CHAT_PATH, payload)
        body = response.body if isinstance(response.body, dict) else {}
        if response.status == 0:
            # An attempt that says nothing at all (curl's own 000 carries no body) must not erase
            # what an earlier one said, since that is the only account of why the call keeps
            # failing.
            unanswered_detail = response.text or unanswered_detail
            await asyncio.sleep(poll_seconds)
            continue
        if response.is_ok:
            agent_id = body.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                logger.info("Created the workspace chat {!r} (agent {})", display_name, agent_id)
                return agent_id
            logger.error("The workspace created a chat but named no agent id: {}", response.text[:300])
            return None
        # A conflict is the name being held already -- by an agent, or by a create still in flight.
        if response.status == HTTPStatus.CONFLICT:
            logger.info(
                "The workspace already has a chat named {!r} ({}); resolving it from the agents listing",
                display_name,
                str(body.get("detail") or "")[:200],
            )
            resolved_agent_id = await fetch_chat_agent_id(
                environment, env, workspace_agent_id, display_name, deadline, poll_seconds
            )
            if resolved_agent_id is None:
                logger.error(
                    "The workspace refused a chat named {!r} as taken, then never listed one under that name",
                    display_name,
                )
            return resolved_agent_id
        logger.error(
            "The workspace refused to create a chat (HTTP {}): {}",
            response.status,
            str(body.get("detail") or response.text)[:300],
        )
        return None
    logger.error("The workspace's create-chat endpoint never answered ({})", unanswered_detail[:300])
    return None


@pure
def build_credential_lines(anthropic_api_key: str, anthropic_base_url: str) -> str:
    """The credential paste body the workspace's sign-in endpoint accepts: newline-separated
    ``NAME=value`` lines. A base URL routes the workspace through a proxy and requires an
    accompanying key, which is the shape the product's own Imbue-credential blob uses."""
    lines = ["{}={}".format(ANTHROPIC_API_KEY_ENV_VAR, anthropic_api_key)]
    if anthropic_base_url:
        lines.append("{}={}".format(ANTHROPIC_BASE_URL_ENV_VAR, anthropic_base_url))
    return "\n".join(lines) + "\n"


_REDACTION: Final[str] = "<redacted>"


@pure
def redact_secret(text: str, secret: str) -> str:
    """``text`` with every occurrence of ``secret`` masked.

    Whatever answers a sign-in can quote the request that carried the credential paste: the
    endpoint reports an unreadable body by rendering the validation error, which carries the input
    it could not read, and a bridged command that fails can be echoed back with its own ``-d``
    payload in it. Trial logs are kept and shared long after the run, so mask before logging --
    and mask before truncating, since a slice of an unmasked key is still a leak.
    """
    return text.replace(secret, _REDACTION) if secret else text


async def wait_for_auth_endpoint(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    deadline: float,
    poll_seconds: float,
) -> bool:
    """Block until the workspace's claude-auth endpoint answers, so credentials are not posted at a
    system_interface that is still coming up. This is a real readiness gate for auth, which the
    turn loop otherwise lacks -- without it a failure surfaces only as an agent that replies with
    'not logged in' text.

    Readiness is a 2xx, not merely a body. Being signed out is a 200 here, so the endpoint's own
    error shapes -- which are JSON like everything else -- all mean it cannot report the state at
    all, and posting credentials at a harness that just said so only moves the failure somewhere
    less legible.
    """
    while time.time() < deadline:
        response = await workspace_curl(environment, env, workspace_agent_id, CLAUDE_AUTH_STATUS_PATH, None)
        if response.is_ok and isinstance(response.body, dict):
            return True
        await asyncio.sleep(poll_seconds)
    return False


class WorkspaceSignIn(FrozenModel):
    """What the workspace reported when it was signed in through its own credential endpoint."""

    is_signed_in: bool = Field(description="Whether the workspace came back signed in, in the mode asked for")
    # A chat binds to an account when it is created, so this is what the driver's own chat is made
    # against. Empty when the workspace named no account, which leaves the choice to the workspace.
    account_id: str = Field(default="", description="The provider account the credentials minted")


# What every path that leaves the workspace unauthenticated reports, whether the workspace refused
# the credentials or was never asked for them at all.
NOT_SIGNED_IN: Final[WorkspaceSignIn] = WorkspaceSignIn(is_signed_in=False)


async def authenticate_workspace(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    anthropic_api_key: str,
    anthropic_base_url: str,
) -> WorkspaceSignIn:
    """Sign the workspace in the way a user does, via the product's own credential endpoint.

    The endpoint mints the provider account it answers with, writes the credentials into that
    account rather than over the workspace's shared login, and records the key's approval so claude
    never challenges it. That account is what a chat is then created against, so signing in has to
    come before the chat.
    """
    payload = json.dumps({"credentials": build_credential_lines(anthropic_api_key, anthropic_base_url)})
    response = await workspace_curl(environment, env, workspace_agent_id, CLAUDE_AUTH_SUBMIT_PATH, body_json=payload)
    body = response.body
    if not isinstance(body, dict):
        # The endpoint's own answers are all JSON, so anything else came from somewhere else -- an
        # unhandled traceback page, or something in front of the system_interface. It is the only
        # account of the failure a trial log would otherwise get, so it is reported verbatim.
        logger.error(
            "The workspace's sign-in endpoint answered nothing readable (HTTP {}): {}",
            response.status,
            redact_secret(response.text, anthropic_api_key)[:300],
        )
        return NOT_SIGNED_IN
    # Read on the status, not on the body alone: a signed-in answer is a 2xx, and anything else is a
    # refusal whose body may still parse and may still carry an auth status shaped like a success.
    # Every refusal names a detail, and which one it is decides whether the trial's credentials or
    # the workspace itself is at fault -- a paste the endpoint could not read against an account it
    # could not write -- so the status and the detail are reported together. A detail alongside a
    # 2xx is not a shape the endpoint has, but it is not a sign-in either.
    if not response.is_ok or body.get("detail"):
        logger.error(
            "The workspace refused the sign-in (HTTP {}): {}",
            response.status,
            redact_secret(str(body.get("detail") or response.text), anthropic_api_key)[:300],
        )
        return NOT_SIGNED_IN
    # The endpoint deliberately runs no credential probe, so a wrong key or base URL is accepted
    # here and surfaces only later, as the agent replying that it is not logged in -- which the
    # judge would then grade as if it were the agent's own behaviour. Check what the workspace
    # reports rather than treating a non-error response as success.
    expected_mode = AUTH_MODE_IMBUE if anthropic_base_url else AUTH_MODE_API_KEY
    actual_mode = str(body.get("auth_mode") or "none")
    if not body.get("logged_in") or actual_mode != expected_mode:
        logger.error(
            "The workspace did not come back signed in (logged_in={}, auth_mode={}, expected {})",
            body.get("logged_in"),
            actual_mode,
            expected_mode,
        )
        return NOT_SIGNED_IN
    account_id = body.get("account_id")
    if not isinstance(account_id, str) or not account_id:
        logger.warning(
            "The workspace signed in but named no account; its chat will be created against whichever "
            "account the workspace picks for itself"
        )
        return WorkspaceSignIn(is_signed_in=True)
    return WorkspaceSignIn(is_signed_in=True, account_id=account_id)


@pure
def parse_agent_ssh_info(listed_json: str, agent_id: str) -> dict[str, str] | None:
    """The workspace's SSH endpoint out of `mngr list --format json`, the same payload mngr's own
    forwarding parses. None when the agent is absent or carries no SSH block."""
    try:
        payload = json.loads(listed_json)
    except ValueError:
        return None
    agents = payload.get("agents") if isinstance(payload, dict) else payload
    for entry in agents or []:
        if not isinstance(entry, dict) or str(entry.get("id")) != agent_id:
            continue
        ssh = (entry.get("host") or {}).get("ssh")
        if not isinstance(ssh, dict) or not ssh.get("host"):
            return None
        return {
            "user": str(ssh.get("user") or "root"),
            "host": str(ssh["host"]),
            "port": str(ssh.get("port") or 22),
            "key_path": str(ssh.get("key_path") or ""),
        }
    return None


async def fetch_agent_ssh_info(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
) -> dict[str, str] | None:
    result = await run_in_box(
        environment, "cd {} && uv run mngr list --format json".format(BOX_MNGR_DIR), env, _QUICK_EXEC_TIMEOUT_SECONDS
    )
    return parse_agent_ssh_info(result.stdout or "", workspace_agent_id)


async def start_reverse_tunnel(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    ssh_info: dict[str, str],
    port: int,
    hold_seconds: float,
    is_probe_token_served: bool,
) -> None:
    """Upload the tunnel holder and start it in the background in the box.

    Uploaded at run time rather than baked into the box image: the image is layer-cached per mngr SHA
    and has to stay byte-identical across a dataset, so shipping this in it would cost a rebuild.
    """
    with resources.as_file(_RESOURCES / BOX_REVERSE_TUNNEL_FILENAME) as script_path:
        await environment.upload_file(script_path, BOX_REVERSE_TUNNEL_PATH)
    command = (
        "cd {mngr} && setsid nohup uv run python {script} --agent-id {agent} --ssh-user {user} "
        "--ssh-host {host} --ssh-port {ssh_port} --ssh-key {key} --port {port} "
        "--hold-seconds {hold}{probe} > {logs}/{log} 2>&1 < /dev/null &"
    ).format(
        mngr=BOX_MNGR_DIR,
        script=BOX_REVERSE_TUNNEL_PATH,
        agent=shlex.quote(workspace_agent_id),
        user=shlex.quote(ssh_info["user"]),
        host=shlex.quote(ssh_info["host"]),
        ssh_port=shlex.quote(ssh_info["port"]),
        key=shlex.quote(ssh_info["key_path"]),
        port=port,
        hold=hold_seconds,
        probe=" --serve-probe-token" if is_probe_token_served else "",
        logs=BOX_LOGS_DIR,
        log=TUNNEL_LOG_FILENAME,
    )
    await check_run_in_box(environment, command, env, _QUICK_EXEC_TIMEOUT_SECONDS)


async def upload_flow_step_script(environment: BaseEnvironment, target_path: str) -> None:
    """Put the UI-flow step script, and the protocol module it imports, in the box.

    Both land in the target's directory, because the script imports the protocol as a plain module
    beside it. Uploaded per trial rather than baked into the box image for the same reason the
    reverse-tunnel holder is: the image is layer-cached per mngr SHA and has to stay byte-identical
    across a dataset, so a change here would otherwise cost a full rebuild.
    """
    box_dir = target_path.rsplit("/", 1)[0]
    for filename, destination in (
        (BOX_FLOW_STEP_FILENAME, target_path),
        (BOX_FLOW_PROTOCOL_FILENAME, "{}/{}".format(box_dir, BOX_FLOW_PROTOCOL_FILENAME)),
    ):
        with resources.as_file(_RESOURCES / filename) as source_path:
            await environment.upload_file(source_path, destination)


async def read_box_file(environment: BaseEnvironment, env: dict[str, str], path: str) -> str:
    result = await run_in_box(
        environment, "cat {} 2>/dev/null || true".format(shlex.quote(path)), env, _QUICK_EXEC_TIMEOUT_SECONDS
    )
    return (result.stdout or "").strip()


async def start_proxy(
    environment: BaseEnvironment,
    env: dict[str, str],
    config_text: str,
    anthropic_api_key: str,
    proxy_key: str,
    port: int,
) -> None:
    """Upload the proxy's config and hooks and start it in the background in the box.

    The upstream credential and the trial's key reach the proxy through its environment, never
    through the uploaded config, so neither is written to a file the workspace could read even if it
    could reach the box's filesystem (it cannot).
    """
    await check_run_in_box(environment, "mkdir -p {}".format(BOX_PROXY_DIR), env, _QUICK_EXEC_TIMEOUT_SECONDS)
    with resources.as_file(_RESOURCES / BOX_PROXY_HOOKS_FILENAME) as hooks_path:
        await environment.upload_file(hooks_path, "{}/{}".format(BOX_PROXY_DIR, BOX_PROXY_HOOKS_FILENAME))
    await write_box_file(environment, env, "{}/{}".format(BOX_PROXY_DIR, PROXY_CONFIG_FILENAME), config_text)
    proxy_env = dict(env)
    proxy_env.update(
        {
            "ANTHROPIC_API_KEY": anthropic_api_key,
            PROXY_KEY_ENV_VAR: proxy_key,
            PROXY_USAGE_LOG_ENV_VAR: BOX_PROXY_USAGE_LOG_PATH,
            # litellm imports the hooks by module name, so the directory holding them must be on the
            # path; it is not the working directory, which stays the monorepo for `uv run`.
            "PYTHONPATH": BOX_PROXY_DIR,
        }
    )
    command = (
        "cd {mngr} && setsid nohup uv run --package modal-litellm litellm --config {config} "
        "--port {port} --host 127.0.0.1 > {logs}/{log} 2>&1 < /dev/null &"
    ).format(
        mngr=BOX_MNGR_DIR,
        config="{}/{}".format(BOX_PROXY_DIR, PROXY_CONFIG_FILENAME),
        port=port,
        logs=BOX_LOGS_DIR,
        log=PROXY_LOG_FILENAME,
    )
    await check_run_in_box(environment, command, proxy_env, _QUICK_EXEC_TIMEOUT_SECONDS)


async def wait_for_proxy(
    environment: BaseEnvironment,
    env: dict[str, str],
    port: int,
    deadline: float,
    poll_seconds: float,
) -> bool:
    """Block until the proxy answers its liveness endpoint inside the box."""
    command = "curl -s -o /dev/null -w '%{{http_code}}' --max-time 5 http://127.0.0.1:{}/health/liveliness".format(
        port
    )
    while time.time() < deadline:
        result = await run_in_box(environment, command, env, _QUICK_EXEC_TIMEOUT_SECONDS)
        if (result.stdout or "").strip().endswith("200"):
            return True
        await asyncio.sleep(poll_seconds)
    return False


async def write_box_file(environment: BaseEnvironment, env: dict[str, str], path: str, content: str) -> None:
    """Write text into the box via a heredoc, avoiding a temp file on the host for small payloads."""
    await check_run_in_box(
        environment,
        "cat > {} <<'MINDS_EVALS_BOX_FILE_EOF'\n{}\nMINDS_EVALS_BOX_FILE_EOF".format(shlex.quote(path), content),
        env,
        _QUICK_EXEC_TIMEOUT_SECONDS,
    )


async def fetch_from_workspace(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    url: str,
) -> str:
    """Fetch a URL from inside the workspace, over the bridge the driver already uses."""
    inner_command = " ".join(shlex.quote(part) for part in ["curl", "-s", "--max-time", "20", url])
    _is_success, stdout = await run_in_workspace(environment, env, workspace_agent_id, inner_command, 60)
    return stdout.strip()


async def fetch_chat_agent_state(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
) -> str | None:
    body = await workspace_curl_json(environment, env, workspace_agent_id, AGENTS_PATH, None)
    if not isinstance(body, dict):
        return None
    for agent in body.get("agents") or []:
        if isinstance(agent, dict) and agent.get("id") == chat_agent_id:
            return str(agent.get("state") or "").upper()
    return None


async def wait_for_chat_state(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
    *,
    is_waiting_desired: bool,
    deadline: float,
    poll_seconds: float,
) -> bool:
    """Block until the chat agent is WAITING (True) or has left WAITING (False), same gating the old
    in-workspace worker used."""
    while time.time() < deadline:
        state = await fetch_chat_agent_state(environment, env, workspace_agent_id, chat_agent_id)
        if state is not None and (state == "WAITING") == is_waiting_desired:
            return True
        await asyncio.sleep(poll_seconds)
    return False


async def send_chat_message(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
    message: str,
    deadline: float,
    poll_seconds: float,
) -> bool:
    """Send a chat message the way the UI chat box does, retrying anything short of a 2xx until the
    deadline.

    Only a 2xx means the harness took the message; the endpoint refuses in JSON, so a body on its
    own proves nothing. A chat listed as WAITING can still answer 404 here -- the listing is a live
    mngr discovery, while this endpoint resolves against the workspace's own agent map, which a
    create fills later -- and a harness whose daemon is still starting answers 503. Reading either
    as sent leaves the turn loop waiting out its budget for a reply to a message that never
    arrived, and blaming the agent for the silence.

    A refusal that will never clear is waited out along with them, unlike ``create_chat_agent``,
    where one is final. The endpoint does not separate the two: the same 500 covers a harness still
    settling and one that is wedged. Giving up on the first refusal would throw away a trial that a
    second attempt would have run, while waiting one out costs a trial that was already lost.
    """
    body_json = json.dumps({"message": message})
    url_path = "/api/agents/{}/message".format(chat_agent_id)
    refusal_detail = ""
    while time.time() < deadline:
        response = await workspace_curl(environment, env, workspace_agent_id, url_path, body_json)
        if response.is_ok:
            return True
        if response.status:
            body = response.body if isinstance(response.body, dict) else {}
            detail = "HTTP {}: {}".format(response.status, str(body.get("detail") or response.text)[:200])
        else:
            detail = response.text
        if detail and detail != refusal_detail:
            # Each refusal as it first appears, and then silence while it repeats: a send being
            # waited out can hold the whole remaining case budget, and an unannounced one is
            # indistinguishable in the log from an agent that is merely slow.
            logger.warning("The workspace has not taken the message yet ({})", detail)
        # An attempt that says nothing must not erase what an earlier one said.
        refusal_detail = detail or refusal_detail
        await asyncio.sleep(poll_seconds)
    logger.error("The workspace never took the message ({})", refusal_detail or "it never answered")
    return False


async def fetch_event_total(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
) -> int | None:
    """The current event count for the chat agent (cheap: a limit=1 head request), or None on a
    transient bridge failure. The driver polls this to decide whether new events exist before pulling
    the (potentially large) window of new events."""
    head = await workspace_curl_json(
        environment, env, workspace_agent_id, "/api/agents/{}/events?offset=0&limit=1".format(chat_agent_id), None
    )
    if not isinstance(head, dict):
        return None
    total = head.get("total")
    return int(total) if isinstance(total, int) else None


async def fetch_events_window(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
    offset: int,
    limit: int,
) -> list[dict[str, Any]] | None:
    """Only the events in [offset, offset+limit) -- an incremental window, so a long conversation is
    not re-transferred in full on every poll. None on a transient bridge failure."""
    if limit <= 0:
        return []
    body = await workspace_curl_json(
        environment,
        env,
        workspace_agent_id,
        "/api/agents/{}/events?offset={}&limit={}".format(chat_agent_id, offset, limit),
        None,
    )
    if not isinstance(body, dict):
        return None
    events = body.get("events")
    return events if isinstance(events, list) else None


# Left out of a snapshot because they are all reinstallable from the lockfiles
# and sources that are in it, and they dominate the tree's size -- which is what
# keeps a per-turn tarball small enough to ship as a trial artifact.
SNAPSHOT_EXCLUDES: Final[tuple[str, ...]] = (
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "target",
    "dist",
    "build",
    ".next",
    ".cache",
)
# The workspace home tree, which contains the mngr host dir -- code, agent
# state, and data -- so snapshotting it captures everything a trial produced.
WORKSPACE_BACKUP_ROOT: Final[str] = "/home/user"


async def snapshot_workspace(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    tag: str,
) -> bool:
    """Tar the workspace home tree (minus SNAPSHOT_EXCLUDES) and pull it into the box's
    /logs/agent/snapshots/<tag>.tar.gz, which harbor syncs into the trial artifacts."""
    exclude_flags = " ".join("--exclude={}".format(shlex.quote(pattern)) for pattern in SNAPSHOT_EXCLUDES)
    # Name the workspace-side tarball after the tag and pull it INTO the
    # snapshots directory (trailing slash): rsyncing a named file into a
    # directory keeps its basename, whereas rsync to an explicit file path was
    # observed to create a directory of that name and nest the tarball inside.
    workspace_tar = "/tmp/{}.tar.gz".format(tag)
    tar_command = "tar czf {} {} -C {} . 2>/dev/null || true".format(
        workspace_tar, exclude_flags, WORKSPACE_BACKUP_ROOT
    )
    is_success, _ = await run_in_workspace(environment, env, workspace_agent_id, tar_command, 300)
    if not is_success:
        logger.warning("Skipped snapshot {}: tar failed in the workspace", tag)
        return False
    pull_command = (
        "mkdir -p {logs}/snapshots && cd {mngr} && uv run mngr rsync {agent}:{src} {logs}/snapshots/".format(
            logs=BOX_LOGS_DIR,
            mngr=BOX_MNGR_DIR,
            agent=shlex.quote(workspace_agent_id),
            src=workspace_tar,
        )
    )
    result = await run_in_box(environment, pull_command, env, _SLOW_EXEC_TIMEOUT_SECONDS)
    if result.return_code != 0:
        logger.warning("Skipped snapshot {}: rsync pull failed: {}", tag, (result.stderr or "").strip()[:200])
        return False
    return True


async def _destroy_pass(environment: BaseEnvironment, env: dict[str, str]) -> list[str]:
    """One destroy sweep; returns the agent ids still listed afterwards. An empty listing piped
    into destroy exits 0 without destroying anything, so the leftover listing (not the exit code)
    is the source of truth."""
    result = await run_in_box(
        environment,
        "cd {mngr} && uv run mngr list --ids | uv run mngr destroy - --force".format(mngr=BOX_MNGR_DIR),
        env,
        _SLOW_EXEC_TIMEOUT_SECONDS,
    )
    if result.return_code != 0:
        logger.warning(
            "Workspace destroy sweep exited {}: {}", result.return_code, (result.stderr or "").strip()[:300]
        )
    remaining = await run_in_box(
        environment,
        "cd {mngr} && uv run mngr list --ids".format(mngr=BOX_MNGR_DIR),
        env,
        _QUICK_EXEC_TIMEOUT_SECONDS,
    )
    remaining_stdout: str = remaining.stdout or ""
    return [agent_id for agent_id in remaining_stdout.split()]


async def destroy_workspaces(environment: BaseEnvironment, env: dict[str, str]) -> None:
    """Tear down every workspace sandbox this trial created (`mngr destroy` has no --all flag; the
    pipe-from-`list` form is the documented destroy-everything idiom, and the box only ever sees its
    own per-trial USER_ID scope). A destroy sweep occasionally no-ops (observed once on a trial
    whose workspace was mid-boot), so retry once before falling back to the sandbox-timeout
    backstop."""
    leftover_ids = await _destroy_pass(environment, env)
    if leftover_ids:
        logger.warning("Workspace cleanup left {} agent(s); retrying the sweep once", len(leftover_ids))
        leftover_ids = await _destroy_pass(environment, env)
    if leftover_ids:
        logger.warning(
            "Workspace cleanup left {} agent(s) behind (their sandbox timeout is the backstop): {}",
            len(leftover_ids),
            " ".join(leftover_ids)[:200],
        )
