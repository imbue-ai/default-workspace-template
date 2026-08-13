"""Helpers that reach the box's Minds HTTP API and the workspace's system_interface through
``environment.exec`` (ported from the old harness's minds_client).

Everything here runs commands inside the harbor environment (the box) or, bridged one level deeper
via ``mngr exec``, inside the trial's nested workspace sandbox. The functions are async because
harbor's environment API is async; this module and driver.py are the only async code in the app.
"""

import asyncio
import json
import shlex
import time
import tomllib
from pathlib import Path
from typing import Any
from typing import Final

from harbor.environments.base import BaseEnvironment
from harbor.environments.base import ExecResult
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds_evals.errors import BoxCommandError
from imbue.minds_evals.errors import WorkspaceCreateError

BOX_MNGR_DIR: Final[str] = "/work/mngr"
BOX_LOGS_DIR: Final[str] = "/logs/agent"
# The workspace-local system_interface the old in-workspace eval worker polled
# (unauthenticated loopback inside the workspace sandbox).
WORKSPACE_SYSTEM_INTERFACE: Final[str] = "http://127.0.0.1:8000"

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
    """Parse the `export KEY=VALUE` lines out of `minds env activate` output (a shell snippet meant
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
    """The env vars `minds env activate` exports (MNGR_PREFIX, MNGR_HOST_DIR, ...). Bridge execs do
    not go through the entrypoint, so without these mngr's modal provider computes the wrong
    environment name and silently sees no workspaces -- fail fast if the critical vars are absent."""
    result = await check_run_in_box(
        environment,
        "cd {} && uv run minds env activate {}".format(BOX_MNGR_DIR, shlex.quote(minds_env)),
        {"MINDS_ENV": minds_env},
        _QUICK_EXEC_TIMEOUT_SECONDS,
    )
    activation_env = parse_activation_exports(result.stdout or "")
    for required_key in ("MNGR_HOST_DIR", "MNGR_PREFIX"):
        if not activation_env.get(required_key):
            raise BoxCommandError(
                "minds env activate did not export {} (got: {}) -- bridge mngr commands would "
                "silently see no workspaces".format(required_key, sorted(activation_env))
            )
    return activation_env


@pure
def build_box_env(
    *,
    activation_env: dict[str, str],
    modal_token_env: dict[str, str],
    anthropic_api_key: str,
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
    if anthropic_api_key:
        env["ANTHROPIC_API_KEY"] = anthropic_api_key
        # dwt pins claude agents to shared config-dir mode
        # (agent_types.claude.isolate_local_config_dir = false), and in shared
        # mode mngr_claude skips the create-time path that records the key's
        # approval in .claude.json -- so the workspace's claude chat agent would
        # otherwise deadlock on the interactive "use this API key?" TUI dialog.
        # Flipping it to isolated mode via the MNGR__* config-override layer
        # (which outranks the dwt settings file) restores the approval; the chat
        # agent type inherits it through parent_type = "claude".
        env["MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR"] = "true"
        # The dwt modal template carries no pass_host_env of its own (verified
        # against current dwt main), so name what the workspace needs in the
        # box-level manifest: the in-box minds backend turns each name into
        # `--pass-host-env` on every create, forwarding the value from its own
        # env into the workspace host env, which the in-workspace chat-agent
        # create then inherits (the key to approve, and the override that makes
        # the approval happen).
        env["MINDS_EXTRA_PASS_HOST_ENV"] = "ANTHROPIC_API_KEY MNGR__AGENT_TYPES__CLAUDE__ISOLATE_LOCAL_CONFIG_DIR"
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
    head, _, status_line = output.rpartition("\n")
    if not status_line.strip().isdigit():
        return 0, {"error": output[:400]}
    status = int(status_line.strip())
    if not head.strip():
        return status, {}
    try:
        parsed = json.loads(head)
    except ValueError:
        return status, {"error": head[:400]}
    return status, parsed if isinstance(parsed, dict) else {"error": head[:400]}


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


async def workspace_curl_json(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    url_path: str,
    body_json: str | None,
) -> Any | None:
    """HTTP against the workspace-local system_interface, bridged through mngr exec; returns the
    parsed JSON body, or None on any failure (callers poll)."""
    parts = ["curl", "-s", "--max-time", "30"]
    if body_json is not None:
        parts += ["-X", "POST", "-H", "Content-Type: application/json", "-d", body_json]
    parts.append("{}{}".format(WORKSPACE_SYSTEM_INTERFACE, url_path))
    inner_command = " ".join(shlex.quote(part) for part in parts)
    is_success, stdout = await run_in_workspace(environment, env, workspace_agent_id, inner_command, 60)
    if not is_success or not stdout.strip():
        return None
    try:
        return json.loads(stdout)
    except ValueError:
        return None


_SYSTEM_SERVICES_AGENT_NAME: Final[str] = "system-services"


@pure
def _resolve_chat_agent_id(agents: list[Any], workspace_host_name: str) -> str | None:
    named_matches = [
        agent
        for agent in agents
        if isinstance(agent, dict)
        and agent.get("id")
        and str(agent.get("name") or "").lower() == workspace_host_name.lower()
    ]
    if named_matches:
        return str(named_matches[0]["id"])
    non_system_agents = [
        agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("id") and agent.get("name") != _SYSTEM_SERVICES_AGENT_NAME
    ]
    if len(non_system_agents) == 1:
        return str(non_system_agents[0]["id"])
    return None


async def fetch_chat_agent_id(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    workspace_host_name: str,
    deadline: float,
    poll_seconds: float,
) -> str | None:
    """The workspace's chat (primary) agent id, resolved from the workspace system_interface's
    agents listing: the agent named after the workspace host, else the single non-system-services
    agent. (The dwt worker read the manager's initial_chat_agent_id file instead, but that file is
    written lazily and proved unreliable over the bridge.)"""
    while time.time() < deadline:
        body = await workspace_curl_json(environment, env, workspace_agent_id, "/api/agents", None)
        agents = body.get("agents") if isinstance(body, dict) else None
        if isinstance(agents, list):
            chat_agent_id = _resolve_chat_agent_id(agents, workspace_host_name)
            if chat_agent_id is not None:
                return chat_agent_id
        await asyncio.sleep(poll_seconds)
    return None


async def fetch_chat_agent_state(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    chat_agent_id: str,
) -> str | None:
    body = await workspace_curl_json(environment, env, workspace_agent_id, "/api/agents", None)
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
    """Send a chat message the way the UI chat box does, retrying transient failures until the
    deadline."""
    body_json = json.dumps({"message": message})
    url_path = "/api/agents/{}/message".format(chat_agent_id)
    while time.time() < deadline:
        body = await workspace_curl_json(environment, env, workspace_agent_id, url_path, body_json)
        if body is not None:
            return True
        await asyncio.sleep(poll_seconds)
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


# Match the dwt eval worker's restic exclude set (deps are reinstallable from
# lockfiles), so snapshots stay lean and comparable to the old harness's.
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
# The tree the dwt eval worker snapshotted: the workspace home tree that
# contains the mngr host dir (code, agent state, and data).
WORKSPACE_BACKUP_ROOT: Final[str] = "/home/user"


async def snapshot_workspace(
    environment: BaseEnvironment,
    env: dict[str, str],
    workspace_agent_id: str,
    tag: str,
) -> bool:
    """Tar the workspace home tree (with the dwt worker's exclude set) and pull it into the box's
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
