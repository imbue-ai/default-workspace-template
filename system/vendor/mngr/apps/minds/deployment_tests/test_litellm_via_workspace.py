"""``minds_services`` test: real-LLM-call-through-litellm via a local Docker DEFAULT_WORKSPACE_TEMPLATE workspace.

The "but does this actually work" test for imbue_cloud LLM key minting
+ litellm proxy routing + spend tracking, exercised through the same
product path a user takes since AI credentials moved out of the create
flow: the workspace boots unauthenticated, a LiteLLM key is minted for
the signed-in user (the same connector mint the desktop app's
``/settings/ai-keys`` page drives, with the same workspace-keyed alias),
and the resulting env-var credential blob is submitted through the
workspace's own ``/api/claude-auth/submit-credentials`` endpoint -- the
strict endpoint behind the sign-in modal's paste textarea, which writes
the shared Claude settings env block and restarts the workspace's
claude agents. A real chat message then proves the agent serves traffic
through the minted key, and the litellm token row proves the spend was
tracked.

The mint-page and modal *browser UI* legs are covered elsewhere (the
ai_keys unit tests and the Electron modal sign-in test in
test_snapshot_resume.py); this test owns the cross-service integration:
connector mint -> workspace credential write -> litellm-proxied claude
traffic -> spend row.

Runs locally against the operator's Docker daemon (skips when Docker or
the orchestrator-prepared template worktree is unavailable). When this
moves to offload, the future ``offload-modal-minds-services.toml`` will
enable Docker-in-Docker (mirroring ``offload-modal-acceptance.toml``).
"""

import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from collections.abc import Iterator
from pathlib import Path

import httpx
import psycopg2
import pytest
import tomlkit
from loguru import logger
from pydantic import SecretStr

from imbue.imbue_common.primitives import NonEmptyStr
from imbue.minds.deployment_tests.data_types import DefaultWorkspaceTemplateRef
from imbue.minds.deployment_tests.data_types import SharedEnvHandle
from imbue.minds.deployment_tests.helpers import signin_and_mint_litellm_key
from imbue.minds.deployment_tests.helpers import wait_for_env_ready
from imbue.minds.desktop_client.ai_keys import build_credential_blob
from imbue.mngr.utils.testing import get_short_random_string

pytestmark = [pytest.mark.release, pytest.mark.minds_services, pytest.mark.docker, pytest.mark.rsync]

_CREATE_TIMEOUT_SECONDS = 1200
_IN_CONTAINER_TIMEOUT_SECONDS = 120
_SYSTEM_INTERFACE_READY_ATTEMPTS = 60
_CHAT_REPLY_ATTEMPTS = 60
_SPEND_POLL_ATTEMPTS = 30
_SPEND_POLL_INTERVAL_SECONDS = 10


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout: int,
    logged_command: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``command``; ``logged_command`` (when given) is logged in its place so secrets stay out of the logs."""
    logger.info("Running: {}", " ".join(command) if logged_command is None else logged_command)
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False, env=env)


def _exec_in_container(
    container_name: str, command: str, *, timeout: int, logged_command: str | None = None
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["docker", "exec", container_name, "bash", "-lc", command],
        timeout=timeout,
        logged_command=None if logged_command is None else f"docker exec {container_name} bash -lc {logged_command}",
    )


def _prepare_template_clone(source_worktree: Path) -> Path:
    """Clone the orchestrator-prepared template checkout into a scratch dir.

    The clone gets ``is_allowed_in_pytest = true`` appended to its mngr
    settings (mngr's config guard refuses to run under PYTEST_CURRENT_TEST
    otherwise); the orchestrator's own worktree is never mutated.
    """
    clone_target = Path(tempfile.mkdtemp(prefix="litellm-e2e-dwt-")) / "default-workspace-template"
    clone = _run(
        ["git", "clone", "--local", f"file://{source_worktree}", str(clone_target)],
        timeout=600,
    )
    assert clone.returncode == 0, f"template clone failed: {clone.stderr}"
    settings_path = clone_target / ".mngr" / "settings.toml"
    doc = tomlkit.parse(settings_path.read_text())
    doc["is_allowed_in_pytest"] = True
    settings_path.write_text(tomlkit.dumps(doc))
    return clone_target


def _create_docker_workspace(template_path: Path, host_name: str) -> tuple[str, str]:
    """Run the real ``mngr create`` the desktop client runs; return (agent_id, host_id)."""
    # The pytest isolation fixture sets MNGR_ROOT_NAME to a per-test value, which
    # makes mngr resolve project config at .<root_name>/ instead of the template's
    # .mngr/ -- so the create would see no templates at all. Point the project
    # config dir at the clone's .mngr explicitly.
    create_env = dict(os.environ)
    create_env["MNGR_PROJECT_CONFIG_DIR"] = str(template_path / ".mngr")
    # The template passes the host's ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL
    # into the workspace (pass_host_env). With a real key in the invoking env
    # (the CI job exports one for other steps), the workspace's claude would
    # talk straight to Anthropic and the minted litellm key would never record
    # spend -- the very thing this test must prove. Strip them so the
    # submitted credentials are the workspace's only LLM path.
    create_env.pop("ANTHROPIC_API_KEY", None)
    create_env.pop("ANTHROPIC_BASE_URL", None)
    create = _run(
        [
            "mngr",
            "create",
            f"system-services@{host_name}.docker",
            "--new-host",
            "--no-connect",
            "--label",
            "is_primary=true",
            "--template",
            "main",
            "--template",
            "docker",
            "--format",
            "jsonl",
        ],
        cwd=template_path,
        timeout=_CREATE_TIMEOUT_SECONDS,
        env=create_env,
    )
    assert create.returncode == 0, f"mngr create failed: {create.stderr[-2000:]}"
    agent_id, host_id = "", ""
    for line in create.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("event") == "created":
            agent_id = str(event.get("agent_id", ""))
            host_id = str(event.get("host_id", ""))
    assert agent_id and host_id, f"mngr create emitted no created event: {create.stdout[-2000:]}"
    return agent_id, host_id


def _find_container_name(host_id: str) -> str:
    listing = _run(
        ["docker", "ps", "--filter", f"label=com.imbue.mngr.host-id={host_id}", "--format", "{{.Names}}"],
        timeout=60,
    )
    names = [name for name in listing.stdout.splitlines() if name.strip()]
    assert names, f"No docker container carries mngr host id {host_id}"
    return names[0]


def _wait_for_system_interface(container_name: str) -> None:
    poll = (
        f"for i in $(seq 1 {_SYSTEM_INTERFACE_READY_ATTEMPTS}); do "
        "code=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8000/api/claude-auth/status); "
        '[ "$code" = "200" ] && exit 0; sleep 5; done; exit 1'
    )
    result = _exec_in_container(container_name, poll, timeout=_SYSTEM_INTERFACE_READY_ATTEMPTS * 5 + 120)
    assert result.returncode == 0, "The workspace's system_interface never answered its claude-auth status endpoint"


def _submit_credentials_via_workspace_endpoint(container_name: str, credential_blob: str) -> dict[str, object]:
    """POST the blob to the workspace's own modal backend (the strict endpoint)."""
    payload = json.dumps({"credentials": credential_blob})
    # The blob carries the minted LiteLLM key, so the real command must never be
    # logged; a redacted stand-in is logged instead.
    submit = _exec_in_container(
        container_name,
        "curl -s -X POST http://localhost:8000/api/claude-auth/submit-credentials "
        f"-H 'Content-Type: application/json' -d {shlex.quote(payload)}",
        timeout=600,
        logged_command=(
            "curl -s -X POST http://localhost:8000/api/claude-auth/submit-credentials "
            "-H 'Content-Type: application/json' -d '<credential blob redacted>'"
        ),
    )
    assert submit.returncode == 0, f"submit-credentials curl failed: {submit.stderr}"
    body = json.loads(submit.stdout)
    assert isinstance(body, dict), f"submit-credentials returned non-object JSON: {submit.stdout[:500]}"
    return body


def _create_chat_on_account(container_name: str, account_id: str) -> str:
    """Start a chat bound to the account the credential was just adopted into.

    The workspace's boot chat cannot be reused: an agent binds to an account when it is
    CREATED (the credential rides `mngr create`'s own flags), so a chat that already
    existed when the blob arrived is still running on whatever it was created with. Only
    a chat created afterwards runs on the minted key, which is what this test measures
    spend against.
    """
    payload = json.dumps({"account_id": account_id})
    created = _exec_in_container(
        container_name,
        "curl -s -X POST http://localhost:8000/api/agents/create-chat "
        f"-H 'Content-Type: application/json' -d {shlex.quote(payload)}",
        timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
    )
    assert created.returncode == 0, f"create-chat curl failed: {created.stderr}"
    body = json.loads(created.stdout)
    agent_id = str(body.get("agent_id", ""))
    assert agent_id, f"create-chat returned no agent id: {created.stdout[:500]}"

    # The endpoint answers as soon as the background `mngr create` starts, so the agent is
    # a proto for a few seconds; messaging it before mngr registers it would fail.
    poll = (
        f"for i in $(seq 1 {_CHAT_CREATE_ATTEMPTS}); do "
        "cd /home/user/workspace && mngr list --format json --on-error continue "
        f"| grep -q {shlex.quote(agent_id)} && exit 0; sleep 5; done; exit 1"
    )
    listed = _exec_in_container(container_name, poll, timeout=_CHAT_CREATE_ATTEMPTS * 5 + 60)
    assert listed.returncode == 0, f"chat agent {agent_id} never appeared in mngr list"
    return agent_id


def _assert_chat_is_bound_to_account(container_name: str, agent_id: str, account_id: str) -> None:
    """Prove the agent's harness will actually READ the account it was created on.

    Writing the credential and creating the chat are separately covered; this is the join
    between them, and it is the one that fails silently. A chat bound to nothing looks
    identical until its first turn -- and for codex, a credential that landed in an OS
    keyring rather than the account leaves a dangling symlink while `codex login status`
    still reports success.

    Both mechanisms are checked the way the harness resolves them: claude reads
    CLAUDE_CONFIG_DIR out of the agent's env file, the others follow a credential symlink
    placed in the agent's own state directory.
    """
    script = f"""
import json, os, pathlib, subprocess, sys
rows = json.loads(subprocess.run(
    ["mngr", "list", "--format", "json", "--on-error", "continue"],
    cwd="/home/user/workspace", capture_output=True, text=True, check=True).stdout)
row = next((r for r in rows if r.get("id") == {agent_id!r}), None)
if row is None:
    sys.exit("agent {agent_id} is not in mngr list")
state = pathlib.Path(row["state_dir"])
account = pathlib.Path(os.path.expanduser("~/.minds/accounts")) / {account_id!r}
env_file = state / "env"
if env_file.exists() and str(account) in env_file.read_text():
    print("BOUND_BY_ENV")
    raise SystemExit(0)
for link in state.rglob("*"):
    if link.is_symlink() and str(account) in str(link.resolve()):
        print("BOUND_BY_SYMLINK", link)
        raise SystemExit(0)
print("NOT_BOUND")
print("env:", env_file.read_text() if env_file.exists() else "<none>")
print("links:", [str(p) for p in state.rglob("*") if p.is_symlink()])
raise SystemExit(1)
"""
    bound = _exec_in_container(
        container_name,
        f"cd /home/user/workspace && python3 -c {shlex.quote(script)}",
        timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
    )
    assert bound.returncode == 0, f"chat {agent_id} is not bound to account {account_id}:\n{bound.stdout}"
    logger.info("chat {} bound to account {}: {}", agent_id, account_id, bound.stdout.strip())


def _sign_in_paste_lane(container_name: str, lane_id: str, api_key: str, key_provider: str | None = None) -> str:
    """Drive a key-paste lane through the real HTTP routes; returns the account id.

    A deliberately fake key: what this proves is the chain from paste to a bound chat --
    account minted, credential file written where the harness looks, chat created against
    it. Whether the provider would honour the key is not a thing a deployment test can
    know, and the promote probe reads the file rather than calling out.
    """
    start = json.dumps({"lane_id": lane_id, "method_id": "api_key"})
    started = _exec_in_container(
        container_name,
        "curl -s -X POST http://localhost:8000/api/accounts "
        f"-H 'Content-Type: application/json' -d {shlex.quote(start)}",
        timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
    )
    assert started.returncode == 0, f"start-flow curl failed for {lane_id}: {started.stderr}"
    flow = json.loads(started.stdout)
    assert flow.get("shape") == "paste", f"{lane_id} is not a paste lane: {flow!r}"

    body = {"api_key": api_key} | ({"key_provider": key_provider} if key_provider else {})
    submitted = _exec_in_container(
        container_name,
        f"curl -s -X POST http://localhost:8000/api/accounts/flow/{flow['flow_id']} "
        f"-H 'Content-Type: application/json' -d {shlex.quote(json.dumps(body))}",
        timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
    )
    assert submitted.returncode == 0, f"submit-key curl failed for {lane_id}: {submitted.stderr}"
    status = json.loads(submitted.stdout)
    assert status.get("state") == "ok", f"{lane_id} sign-in did not complete: {status!r}"
    account_id = str(status.get("account_id", ""))
    assert account_id, f"{lane_id} sign-in minted no account: {status!r}"
    return account_id


def _chat_and_await_echo(container_name: str, chat_agent_id: str, token: str) -> None:
    messaged = _exec_in_container(
        container_name,
        f'cd /home/user/workspace && mngr message {chat_agent_id} -m "Reply with exactly this token and nothing else: {token}"',
        timeout=300,
    )
    assert messaged.returncode == 0, f"mngr message failed: {messaged.stderr}"
    # Filter to assistant events: the sent prompt itself contains the token, so
    # an unfiltered transcript would match the user's own message before (and
    # regardless of) any reply.
    poll = (
        f"for i in $(seq 1 {_CHAT_REPLY_ATTEMPTS}); do "
        f"cd /home/user/workspace && mngr transcript {chat_agent_id} --role assistant 2>/dev/null | grep -q {token} && exit 0; "
        "sleep 5; done; exit 1"
    )
    replied = _exec_in_container(container_name, poll, timeout=_CHAT_REPLY_ATTEMPTS * 5 + 120)
    assert replied.returncode == 0, f"The chat agent never echoed the token {token}"


# How long to wait for a freshly created chat to be registered by mngr.
_CHAT_CREATE_ATTEMPTS = 24

_KEEPALIVE_PING_INTERVAL_SECONDS = 2.0


@contextlib.contextmanager
def _litellm_proxy_keepalive(litellm_base_url: str) -> Iterator[None]:
    """Keep the env's litellm Modal container awake so its spend writer can run.

    LiteLLM persists spend asynchronously: each request's cost is queued in
    memory and a scheduled job (``proxy_batch_write_at``, ~10s) batch-writes it
    to the DB. The CI env's litellm proxy is a scale-to-zero Modal ASGI
    function, and Modal CPU-throttles a container whenever no request is in
    flight -- so after the last LLM call of a burst the flush timer never
    fires. The one wake-up the queue gets is container scaledown (~60s idle),
    where the flush races the teardown and loses ("Spend tracking - DB
    connection error writing spend logs ... All connection attempts failed",
    then the event loop is destroyed mid-retry), silently dropping the spend.
    Verified against a live CI env: an identical proxied call recorded no
    spend when left idle and full spend when followed by traffic.

    Cheap liveness pings during the chat + spend-poll window keep the
    container unthrottled (and alive), so the batch writer actually runs and
    the spend this test asserts on reaches the ledger. Ping failures are
    swallowed: the pings are a scheduling aid, not an assertion.
    """
    stop = threading.Event()

    def ping_loop() -> None:
        with httpx.Client(timeout=10.0) as client:
            while not stop.is_set():
                try:
                    client.get(f"{litellm_base_url}/health/liveness")
                except httpx.HTTPError:
                    pass
                stop.wait(_KEEPALIVE_PING_INTERVAL_SECONDS)

    thread = threading.Thread(target=ping_loop, name="litellm-proxy-keepalive", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=15)


def _await_key_spend(neon_litellm_dsn: str, key_alias: str) -> float:
    """Poll the litellm token table until the minted key shows non-zero spend."""
    for _attempt in range(_SPEND_POLL_ATTEMPTS):
        connection = psycopg2.connect(neon_litellm_dsn)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    'SELECT COALESCE(spend, 0) FROM "LiteLLM_VerificationToken" WHERE key_alias = %s',
                    (key_alias,),
                )
                rows = cursor.fetchall()
        finally:
            connection.close()
        spends = [float(row[0]) for row in rows]
        if any(spend > 0 for spend in spends):
            return max(spends)
        time.sleep(_SPEND_POLL_INTERVAL_SECONDS)
    raise AssertionError(f"litellm never recorded spend for key alias {key_alias!r}")


@pytest.mark.timeout(2700)
def test_litellm_spend_tracking_via_local_workspace(
    shared_env: Callable[[str], SharedEnvHandle],
    ci_test_user: tuple[NonEmptyStr, SecretStr],
    default_workspace_template_ref: DefaultWorkspaceTemplateRef,
) -> None:
    """Drive a real local DEFAULT_WORKSPACE_TEMPLATE workspace + assert spend lands in litellm's ledger.

    Flow (matching the product's post-create sign-in path):

    0. Wait for the env to be reachable (defensive preamble for every test
       in this suite).
    1. Sign in as the fixed CI test user (paid-listed, so it can switch to
       the ally plan key minting needs) and mint a LiteLLM key through the
       connector, with the workspace-keyed alias the desktop app's
       ``/settings/ai-keys`` mint page uses.
    2. Create a real local Docker workspace from the template checkout (the
       same ``mngr create`` invocation the desktop client runs). It boots
       with NO AI credentials.
    3. Submit the env-var credential blob through the workspace's own
       ``/api/claude-auth/submit-credentials`` endpoint (the sign-in
       modal's backend), which writes the shared Claude settings env block
       and restarts the workspace's claude agents.
    4. Send a real chat message via ``mngr message`` and assert the agent
       echoes a unique token -- traffic flows through the minted key + the
       env's litellm proxy.
    5. Poll the env's litellm database for non-zero spend on the minted
       key's alias.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    if shutil.which("docker") is None:
        pytest.skip("Docker is required to create the local workspace")
    if default_workspace_template_ref.worktree_path is None:
        pytest.skip("No local template worktree available (offload sandboxes lack the Docker daemon anyway)")

    # 1. Mint a key for the signed-in user, aliased to the workspace like the
    #    desktop app's mint page does, and render the same paste-ready blob.
    ci_user_email, ci_user_password = ci_test_user
    host_name = f"litellm-e2e-{get_short_random_string()}"
    key_alias = f"workspace-{host_name}"
    minted = signin_and_mint_litellm_key(
        connector_url=str(env.urls.connector_url),
        email=str(ci_user_email),
        password=ci_user_password.get_secret_value(),
        key_alias=key_alias,
        max_budget=100.0,
        budget_duration="1d",
    )
    credential_blob = build_credential_blob(api_key=minted.key.get_secret_value(), base_url=str(minted.base_url))

    # 2-5. Create the workspace, sign it in, chat, and assert spend.
    template_path = _prepare_template_clone(default_workspace_template_ref.worktree_path)
    token = get_short_random_string()
    try:
        _agent_id, host_id = _create_docker_workspace(template_path, host_name)
        container_name = _find_container_name(host_id)
        _wait_for_system_interface(container_name)

        submit_body = _submit_credentials_via_workspace_endpoint(container_name, credential_blob)
        assert submit_body.get("logged_in") is True, f"credential submit did not authenticate: {submit_body!r}"
        assert submit_body.get("auth_mode") == "imbue", f"expected imbue mode after blob submit: {submit_body!r}"
        assert submit_body.get("account_id"), f"credential submit minted no account: {submit_body!r}"

        account_id = str(submit_body["account_id"])
        chat_agent_id = _create_chat_on_account(container_name, account_id)
        # The spend assertion below proves the key was USED; this proves the chat was
        # pointed at the account holding it, which is the join that fails silently.
        _assert_chat_is_bound_to_account(container_name, chat_agent_id, account_id)
        # The keepalive must span the whole chat-to-assertion window: the
        # proxy's last chat call otherwise strands its spend in the throttled
        # container's memory (see _litellm_proxy_keepalive), and the echo
        # poll alone can outlast the ~60s idle scaledown that drops it.
        with _litellm_proxy_keepalive(str(minted.base_url)):
            _chat_and_await_echo(container_name, chat_agent_id, token)
            spend = _await_key_spend(env.neon_litellm_dsn.get_secret_value(), key_alias)
        logger.info("litellm recorded spend {} for key alias {}", spend, key_alias)
    finally:
        # Destroy unconditionally: even a create that failed partway (timeout,
        # nonzero exit, missing created event) may have brought a container up.
        # Destroying a host that never came up fails harmlessly and is only
        # logged.
        destroy = _run(
            ["mngr", "destroy", f"system-services@{host_name}", "--force"],
            cwd=template_path,
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        if destroy.returncode != 0:
            logger.warning("Workspace teardown failed (leaving for manual cleanup): {}", destroy.stderr[-500:])
        shutil.rmtree(template_path.parent, ignore_errors=True)


# Every lane whose sign-in is a file write, so it needs no browser and no real credential.
# The OAuth lanes (Anthropic subscription, OpenAI device, Google) cannot be reached without a
# human in a browser; their PTY driving is covered by unit tests against recorded output.
_PASTE_LANES: tuple[tuple[str, str | None, str], ...] = (
    ("opencode-go", None, "Opencode Go (Pi)"),
    ("openrouter", None, "OpenRouter (Pi)"),
    ("api-key", "groq", "Groq (Pi)"),
)


@pytest.mark.timeout(2700)
def test_every_paste_lane_binds_a_chat_to_its_own_account(
    shared_env: Callable[[str], SharedEnvHandle],
    default_workspace_template_ref: DefaultWorkspaceTemplateRef,
) -> None:
    """Sign in to every key-paste lane in one real workspace and prove each binds a chat.

    The other half of the sign-in artifact. `test_litellm_spend_tracking_via_local_workspace`
    proves one lane end to end including a real turn; this proves the part that is common to
    all of them -- account minted, credential written where the harness looks, chat created
    against it, agent pointed at that folder -- for every lane that can be driven without a
    human in a browser.

    Deliberately fake keys. What fails silently here is the BINDING, not the key: a chat
    bound to nothing is indistinguishable from a working one until its first turn, and the
    label a picker shows comes from the index rather than from anything on disk. A real key
    would add a turn assertion and a credential to leak, and would still not test the join.

    No LiteLLM leg, so no connector sign-in and no key minting -- just the workspace.
    """
    env = shared_env("default")
    wait_for_env_ready(env)
    if shutil.which("docker") is None:
        pytest.skip("Docker is required to create the local workspace")
    if default_workspace_template_ref.worktree_path is None:
        pytest.skip("No local template worktree available (offload sandboxes lack the Docker daemon anyway)")

    template_path = _prepare_template_clone(default_workspace_template_ref.worktree_path)
    host_name = f"lanes-e2e-{get_short_random_string()}"
    try:
        _agent_id, host_id = _create_docker_workspace(template_path, host_name)
        container_name = _find_container_name(host_id)
        _wait_for_system_interface(container_name)

        bound: list[tuple[str, str, str]] = []
        for lane_id, key_provider, expected_label in _PASTE_LANES:
            account_id = _sign_in_paste_lane(
                container_name, lane_id, f"sk-not-a-real-key-{get_short_random_string()}", key_provider
            )
            chat_agent_id = _create_chat_on_account(container_name, account_id)
            _assert_chat_is_bound_to_account(container_name, chat_agent_id, account_id)
            bound.append((lane_id, account_id, expected_label))

        # Each lane is its own account, and the picker names it by its provider rather than
        # by the lane -- two of these run on pi and would otherwise be indistinguishable.
        listed = _exec_in_container(
            container_name,
            "curl -s http://localhost:8000/api/accounts",
            timeout=_IN_CONTAINER_TIMEOUT_SECONDS,
        )
        assert listed.returncode == 0, f"accounts curl failed: {listed.stderr}"
        rows = json.loads(listed.stdout)["accounts"]
        assert len(rows) == len(_PASTE_LANES), f"expected one account per lane, got {rows!r}"
        assert {row["label"] for row in rows} == {label for _lane, _account, label in bound}
        assert {row["id"] for row in rows} == {account for _lane, account, _label in bound}
    finally:
        destroy = _run(
            ["mngr", "destroy", f"system-services@{host_name}", "--force"],
            cwd=template_path,
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        if destroy.returncode != 0:
            logger.warning("Workspace teardown failed (leaving for manual cleanup): {}", destroy.stderr[-500:])
        shutil.rmtree(template_path.parent, ignore_errors=True)
