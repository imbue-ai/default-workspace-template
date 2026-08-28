"""Bootstrap: first-boot setup, then launch supervisord.

`uv run bootstrap` runs once per container boot (from the `bootstrap`
extra_window). It performs first-boot setup -- global git config and creating
the initial chat agent -- and then `exec`s the system supervisord in the
foreground. supervisord (configured by system/supervisord.conf) owns every
background service from then on.

Running supervisord via exec keeps the bootstrap tmux window alive as
supervisord and lets the supervised services inherit this shell's already-
sourced agent environment (MNGR_AGENT_STATE_DIR, MNGR_HOST_DIR, etc.).
CLAUDE_CONFIG_DIR is deliberately absent from that environment: every claude
in the workspace uses claude's own default ~/.claude, so there is nothing to
resolve or export.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_fixed

from bootstrap.claude_state_migration import (
    LEGACY_ROOT_HOME,
    migrate_legacy_claude_state,
)

# Path (relative to the repo root, which is bootstrap's cwd) of the supervisord
# config that defines every background service.
SUPERVISORD_CONF = Path("system/supervisord.conf")
# Container-local directory for supervisord's own log + the per-service logs. Not
# under data/, so these are never backed up.
SUPERVISOR_LOG_DIR = Path("/var/log/supervisor")

STATE_DIR = Path("data/.state")

# Durable home for user-editable cron entries. /etc/cron.d lives on the
# container rootfs and is lost when the container is recreated; files under
# data/.state/cron.d persist with the container volume, and the bootstrap
# installs them into /etc/cron.d at each boot. The entry file is still the
# on/off switch for its job -- it just lives where it survives.
RUNTIME_CRON_DIR = STATE_DIR / "cron.d"
# cron silently ignores drop-ins with dots or other odd characters in their
# names; install only names it will accept and warn about the rest.
_CRON_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")

# Its own signal, separate from the chat's. `git add -A` + commit is a once-per-workspace
# operation: running it on a later boot would commit whatever the user had in flight.
MAIN_BRANCH_SIGNAL = STATE_DIR / "workspace_main_branch_initialized"
# The view the first chat is filed in: the starter project the workspace seeds.
# Duplicated from system_interface's ``projects.DEFAULT_PROJECT_ID`` rather than
# imported, to keep this one-shot first-boot program's dependencies minimal (the
# same trade ``FAST_MODE_DECISION_FILE`` makes above).
_STARTER_PROJECT_ID = "project-1"

# Env var names used by the bootstrap's responsibilities.
_AGENT_ID_ENV_VAR = "MNGR_AGENT_ID"
_HOST_DIR_ENV_VAR = "MNGR_HOST_DIR"

# Global git config applied on every boot: rewrite git@ / ssh:// GitHub
# remotes to https (there are no SSH credentials in the container). Note that
# git applies at most one insteadOf rewrite per URL, so this rewrite's output
# is NOT further rewritten by github-sync's latchkey gateway wiring: only
# remotes stored as https://github.com/ URLs (the shape the github-sync skill
# always configures) route through the gateway.
# core.hooksPath is deliberately NOT set here -- the post-commit auto-push
# hook only becomes active when the github-sync skill wires it up.
_GIT_GLOBAL_CONFIG_ARGVS = (
    (
        "config",
        "--global",
        "--replace-all",
        "url.https://github.com/.insteadOf",
        "git@github.com:",
    ),
    (
        "config",
        "--global",
        "--add",
        "url.https://github.com/.insteadOf",
        "ssh://git@github.com/",
    ),
)


def _read_host_name() -> str | None:
    """Read host_name from $MNGR_HOST_DIR/data.json.

    Same source as system_interface._read_host_name. Returns None if any
    step fails so callers can decide whether to fall back.
    """
    host_dir = os.environ.get(_HOST_DIR_ENV_VAR, "")
    if not host_dir:
        return None
    data_path = Path(host_dir) / "data.json"
    if not data_path.exists():
        return None
    try:
        data = json.loads(data_path.read_text())
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read {}: {}", data_path, e)
        return None
    name = data.get("host_name")
    if not isinstance(name, str) or not name:
        return None
    return name












def _touch(signal: Path) -> None:
    """Create a signal file (and its parent), marking a once-per-workspace step done."""
    signal.parent.mkdir(parents=True, exist_ok=True)
    signal.touch()




def _ensure_git_identity() -> None:
    """Give the workspace repo a committer identity if it has none. Every boot.

    Only-if-unset, so it costs two `git config` reads and never overwrites the user's own.

    Unconditional rather than gated by a signal, because it is the workspace's ONLY committer
    identity (nothing else in the repo sets `user.email` outside tests and vendored code) and
    `pool_bake` deliberately unsets it on finalize, expecting the adopted workspace's bootstrap
    to supply it again. Without an identity every non-agent commit fails -- the user's own
    terminal, github-sync, any script. Agent tool calls survive only because the bash wrapper
    exports GIT_AUTHOR_*/GIT_COMMITTER_*, which covers claude and codex and nothing else.
    """
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    if not work_dir:
        return

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args], cwd=work_dir, capture_output=True, text=True, check=False
        )

    if _git("config", "user.email").returncode != 0:
        _git("config", "user.email", "bootstrap@minds.local")
    if _git("config", "user.name").returncode != 0:
        _git("config", "user.name", "minds-bootstrap")


def _initialize_workspace_main_branch() -> None:
    """Commit any rsync-staged content and rename the work_dir branch to `main`.

    On first boot the work_dir (the services agent's $MNGR_AGENT_WORK_DIR,
    which the chat agent will share via `--transfer none`) is on whatever
    branch the desktop client's create flow assigned (typically
    `mngr/<host_name>` from agent_creator's `--branch :mngr/{host_name}`),
    with the desktop client's `_rsync_worktree_over_clone` content sitting
    as uncommitted changes on top of the shallow clone's tip.

    We want every new minds workspace to start out on a single clean
    `main` branch the user can git-log / push from without having to
    reason about the per-host mngr/* branch. So before the chat agent
    is created, we:
      1. `git add -A` + `git commit` everything currently uncommitted
      2. `git branch -D main` (drop the stale shallow-clone main, if any)
      3. `git checkout -b main` (rename the working tree's branch to main)

    The committer identity is NOT set here -- see `_ensure_git_identity`, which runs on every
    boot. It used to live in this function, which meant it was gated behind the same one-shot
    signal, and `pool_bake` unsets identity on finalize expecting the adopted workspace's
    bootstrap to put it back. Anything that made this function run less often would have left
    an adopted workspace unable to commit at all.

    Each step is best-effort: a failure here should not prevent the
    chat-agent create from running. We log a warning and continue. Hooks
    are skipped with `--no-verify` because the user hasn't seen the
    workspace yet and a misbehaving pre-commit hook on the rsynced
    template shouldn't gate boot.
    """
    if MAIN_BRANCH_SIGNAL.exists():
        logger.debug("Signal file {} present; work_dir is already on main", MAIN_BRANCH_SIGNAL)
        return
    work_dir = os.environ.get("MNGR_AGENT_WORK_DIR", "")
    if not work_dir:
        logger.warning(
            "MNGR_AGENT_WORK_DIR is unset; skipping initial commit / main rename"
        )
        return

    def _git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=work_dir,
            capture_output=True,
            text=True,
            check=False,
        )

    _git("add", "-A")
    # --allow-empty so we end up with a commit even when the work_dir is
    # already clean (e.g. on second boot after a re-Create-from-snapshot,
    # though that path isn't wired up today). --no-verify skips any
    # pre-commit hooks the template repo may have configured.
    commit = _git(
        "commit", "--allow-empty", "--no-verify", "-m", "Initial workspace commit"
    )
    if commit.returncode != 0:
        logger.warning(
            "Initial workspace commit failed (rc={}): {}",
            commit.returncode,
            commit.stderr.strip() or commit.stdout.strip(),
        )

    # Drop any local `main` (the shallow clone's tip) so the rename
    # below has somewhere to land. `-D` is force-delete; harmless when
    # `main` doesn't exist.
    _git("branch", "-D", "main")
    # Rename / move the current branch to `main`. -M is force-rename
    # (move-over). On the very first boot the current branch is
    # `mngr/<host_name>`; on subsequent boots we may already be on `main`,
    # in which case `-M main` is a no-op.
    rename = _git("branch", "-M", "main")
    if rename.returncode != 0:
        logger.warning(
            "git branch -M main failed (rc={}): {}",
            rename.returncode,
            rename.stderr.strip() or rename.stdout.strip(),
        )
    else:
        logger.info("work_dir {} is now on branch main", work_dir)
    # Written whichever way the rename went: a failed rename is not worth re-committing the
    # user's working tree over on every subsequent boot.
    _touch(MAIN_BRANCH_SIGNAL)






def _configure_git_global() -> None:
    """Apply the boot-time global git config.

    Rewrites git@ / ssh:// GitHub remotes to https (see
    _GIT_GLOBAL_CONFIG_ARGVS). Best-effort: a failure here should not block
    the supervisord launch.
    """
    for argv in _GIT_GLOBAL_CONFIG_ARGVS:
        result = subprocess.run(
            ["git", *argv], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            logger.warning(
                "git {} failed (rc={}): {}",
                " ".join(argv),
                result.returncode,
                result.stderr.strip(),
            )


class TimezoneFetchError(Exception):
    """The timezone endpoint answered with an unusable payload."""


def _parse_timezone_response(body: bytes) -> str:
    """Parse the timezone endpoint's body into an IANA name, or "" for unknown.

    A well-formed ``{"timezone": ""}`` is the desktop client's documented
    answer when the user's timezone cannot be determined -- a valid response,
    returned as "" so callers fall back to UTC without treating it as a
    failure. Raises ValueError for a non-JSON or non-UTF-8 body and
    TimezoneFetchError for a well-formed body of the wrong shape.
    """
    payload = json.loads(body.decode("utf-8"))
    timezone_name = payload.get("timezone") if isinstance(payload, dict) else None
    if not isinstance(timezone_name, str):
        raise TimezoneFetchError(f"unexpected timezone payload: {payload!r}")
    return timezone_name


# The gateway's reverse tunnel may not be up yet this early in boot, and there
# is no readiness event to wait on -- hence the small bounded retry.
@retry(
    retry=retry_if_exception_type((OSError, ValueError, TimezoneFetchError)),
    stop=stop_after_attempt(3),
    wait=wait_fixed(3),
    reraise=True,
)
def _request_timezone(request: urllib.request.Request) -> str:
    """One GET of the timezone endpoint; raises so the retry decorator can act.

    OSError covers URLError/HTTPError (refused, 403/503, timeout); ValueError
    covers a non-JSON or non-UTF-8 body; TimezoneFetchError an unexpected
    payload shape. A well-formed "unknown" answer is returned as "" without
    retrying -- the server's answer will not change within the retry window.
    """
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read()
    return _parse_timezone_response(body)


def _fetch_user_timezone() -> str:
    """Fetch the user's IANA timezone name from the minds desktop client.

    GETs /api/v1/timezone through the latchkey gateway's minds-api-proxy using
    the gateway env vars mngr injects into the agent environment. Timezone-at-
    boot: the caller points /etc/localtime + /etc/timezone at the result so
    cron schedules run in the user's local time. Returns "" on any failure
    (missing env, refused connection, non-200, malformed body) -- and when the
    desktop client itself does not know the timezone -- so the caller can fall
    back to UTC.
    """
    gateway = os.environ.get("LATCHKEY_GATEWAY", "")
    password = os.environ.get("LATCHKEY_GATEWAY_PASSWORD", "")
    if not gateway or not password:
        logger.debug("Latchkey gateway env not fully set; skipping timezone fetch")
        return ""
    headers = {"X-Latchkey-Gateway-Password": password}
    # Desktop-hosted gateways authorize via this per-agent JWT and deny
    # requests without it; VPS gateways omit the env var.
    permissions_override = os.environ.get("LATCHKEY_GATEWAY_PERMISSIONS_OVERRIDE", "")
    if permissions_override:
        headers["X-Latchkey-Gateway-Permissions-Override"] = permissions_override
    request = urllib.request.Request(
        f"{gateway.rstrip('/')}/minds-api-proxy/api/v1/timezone",
        headers=headers,
    )
    try:
        timezone_name = _request_timezone(request)
    except (OSError, ValueError, TimezoneFetchError) as e:
        logger.warning(
            "Could not fetch the user timezone from the gateway ({}); "
            "container stays on UTC",
            e,
        )
        return ""
    if not timezone_name:
        logger.debug(
            "Desktop client does not know the user timezone; container stays on UTC"
        )
    return timezone_name


def _apply_container_timezone(
    tz_name: str,
    zoneinfo_dir: Path = Path("/usr/share/zoneinfo"),
    localtime_path: Path = Path("/etc/localtime"),
    timezone_path: Path = Path("/etc/timezone"),
) -> bool:
    """Point /etc/localtime and /etc/timezone at the named IANA zone.

    The name is validated by loading it with ``ZoneInfo`` -- the same check the
    minds desktop client applies before serving the value -- which by spec
    rejects absolute paths and ``..`` components (so a malicious response
    cannot traverse out of the zoneinfo dir) and proves the zone is real. The
    ``is_file`` check below still matters: ZoneInfo may resolve a zone from
    elsewhere on TZPATH, but the symlink must point into ``zoneinfo_dir``
    specifically. The localtime swap is a temp symlink + os.replace so a
    concurrent reader never sees the file missing. Must run before supervisord
    starts cron: cron reads the timezone once at daemon start. Best-effort:
    returns False with a warning on any failure.
    """
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        logger.warning("Ignoring invalid timezone name {!r}", tz_name)
        return False
    zone_file = zoneinfo_dir / tz_name
    if not zone_file.is_file():
        logger.warning("Timezone {!r} has no zoneinfo file at {}", tz_name, zone_file)
        return False
    try:
        tmp_link = localtime_path.with_name(localtime_path.name + ".minds-tmp")
        tmp_link.unlink(missing_ok=True)
        tmp_link.symlink_to(zone_file)
        os.replace(tmp_link, localtime_path)
        timezone_path.write_text(tz_name + "\n")
    except OSError as e:
        logger.warning("Failed to apply timezone {!r}: {}", tz_name, e)
        return False
    logger.info("Container timezone set to {}", tz_name)
    return True


def _install_runtime_cron_entries(target_dir: Path = Path("/etc/cron.d")) -> None:
    """Install data/.state/cron.d/* into /etc/cron.d (mode 0644).

    Best-effort per file: a bad name or an OSError is logged and skipped so
    one broken entry cannot block the rest (or the boot). Runs before
    supervisord starts cron, though cron would also pick the files up on its
    minute-level rescan.
    """
    if not RUNTIME_CRON_DIR.is_dir():
        return
    for entry in sorted(RUNTIME_CRON_DIR.iterdir()):
        if not entry.is_file():
            continue
        if not _CRON_NAME_PATTERN.fullmatch(entry.name):
            logger.warning(
                "Skipping cron entry with a name cron would ignore: {}", entry.name
            )
            continue
        try:
            target = target_dir / entry.name
            target.write_text(entry.read_text())
            target.chmod(0o644)
        except OSError as e:
            logger.warning("Failed to install cron entry {}: {}", entry.name, e)
            continue
        logger.info("Installed cron entry {} into {}", entry.name, target_dir)


def _ensure_supervisor_log_dir() -> None:
    """Create supervisord's log directory if missing.

    supervisord and its child programs write into SUPERVISOR_LOG_DIR but do not
    create it, so it must exist before we exec supervisord. Best-effort.
    """
    try:
        SUPERVISOR_LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning(
            "Failed to create supervisor log dir {}: {}", SUPERVISOR_LOG_DIR, e
        )


# Bound on the boot-time venv converge. A venv that already matches the
# lockfile no-ops in well under a second; a genuinely drifted one (image bake
# older than the landed branch tip) reinstalls from uv's baked warm cache,
# which stays comfortably inside this bound.
_UV_SYNC_TIMEOUT_SECONDS = 600.0


def _sync_workspace_venv() -> None:
    """Converge the workspace .venv to the landed lockfile before any agent runs.

    The venv is a bake-time artifact (build_workspace.sh at image build / host
    provisioning) while the working tree is a landing-time artifact (the
    create's git-mirror checkout) -- and on docker and pool-lease hosts nothing
    re-runs the sync at create, so the two can disagree whenever the baked
    image lags the landed branch. Left alone, the FIRST implicit ``uv run``
    sync reconciles them lazily: mid-boot, concurrent with the services and
    the initial chat agent, and with root-closure scope rather than
    --all-packages. Whatever imports from the venv during that rewrite window
    fails intermittently (ModuleNotFoundError for imbue_common and friends).

    Converging here -- once, up front, before the chat agent exists and before
    supervisord starts anything -- removes both the race window and the scope
    gap; every later implicit sync then no-ops. ``--frozen`` asserts the
    committed lockfile is canonical, matching build_workspace.sh. Best-effort:
    a failure is logged loudly but never blocks boot (the per-``uv run``
    implicit syncs remain the fallback).
    """
    try:
        result = subprocess.run(
            ["uv", "sync", "--all-packages", "--frozen"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_UV_SYNC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "uv sync --all-packages timed out after {}s; continuing boot",
            _UV_SYNC_TIMEOUT_SECONDS,
        )
        return
    if result.returncode != 0:
        logger.error(
            "uv sync --all-packages failed (rc={}): {}",
            result.returncode,
            (result.stderr or result.stdout).strip()[-500:],
        )
        return
    logger.info("Workspace venv converged (uv sync --all-packages --frozen)")


def _exec_supervisord() -> None:
    """Replace this process with supervisord running in the foreground.

    Uses the system supervisord (installed via system/scripts/setup_system.sh) and the
    repo-root system/supervisord.conf. `-n` keeps it in the foreground (so the
    bootstrap tmux window stays alive as supervisord) while still creating the
    [unix_http_server] socket that `supervisorctl` talks to.
    """
    logger.info("Launching supervisord with config {}", SUPERVISORD_CONF)
    os.execvp("supervisord", ["supervisord", "-n", "-c", str(SUPERVISORD_CONF)])


# Bound on the boot-time venv converge. A venv that already matches the
# lockfile no-ops in well under a second; a genuinely drifted one (image bake
# older than the landed branch tip) reinstalls from uv's baked warm cache,
# which stays comfortably inside this bound.
_UV_SYNC_TIMEOUT_SECONDS = 600.0


def _sync_workspace_venv() -> None:
    """Converge the workspace .venv to the landed lockfile before any agent runs.

    The venv is a bake-time artifact (build_workspace.sh at image build / host
    provisioning) while the working tree is a landing-time artifact (the
    create's git-mirror checkout) -- and on docker and pool-lease hosts nothing
    re-runs the sync at create, so the two can disagree whenever the baked
    image lags the landed branch. Left alone, the FIRST implicit ``uv run``
    sync reconciles them lazily: mid-boot, concurrent with the services and
    the initial chat agent, and with root-closure scope rather than
    --all-packages. Whatever imports from the venv during that rewrite window
    fails intermittently (ModuleNotFoundError for imbue_common and friends).

    Converging here -- once, up front, before the chat agent exists and before
    supervisord starts anything -- removes both the race window and the scope
    gap; every later implicit sync then no-ops. ``--frozen`` asserts the
    committed lockfile is canonical, matching build_workspace.sh. Best-effort:
    a failure is logged loudly but never blocks boot (the per-``uv run``
    implicit syncs remain the fallback).
    """
    try:
        result = subprocess.run(
            ["uv", "sync", "--all-packages", "--frozen"],
            capture_output=True,
            text=True,
            check=False,
            timeout=_UV_SYNC_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error(
            "uv sync --all-packages timed out after {}s; continuing boot",
            _UV_SYNC_TIMEOUT_SECONDS,
        )
        return
    if result.returncode != 0:
        logger.error(
            "uv sync --all-packages failed (rc={}): {}",
            result.returncode,
            (result.stderr or result.stdout).strip()[-500:],
        )
        return
    logger.info("Workspace venv converged (uv sync --all-packages --frozen)")


def _run_env_converge_fast_phase() -> None:
    """Apply the overlay symlinks BEFORE any service starts.

    A service that writes to a rootfs path declared in overlay-paths.json must
    find the symlink already in place, or its data would be orphaned on the
    rootfs -- so the fast phase (instant, no network) runs synchronously here,
    pre-supervisord. Best-effort: a failure must not block boot (the slow-phase
    one-shot logs the environment's real problems).
    """
    result = subprocess.run(
        ["uv", "run", "env-converge", "run", "--phase", "fast"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        logger.warning(
            "env-converge fast phase failed (rc={}): {}",
            result.returncode,
            result.stderr.strip()[-500:],
        )


def _migrate_legacy_claude_state_best_effort() -> None:
    """Heal pre-/home/user-layout workspaces whose claude state is root-homed.

    Must run before supervisord starts (the services would otherwise create
    fresh state at the new location) and before the initial chat agent could
    exist. Best-effort: a failure is logged loudly but never blocks boot --
    the state stays where it was, and the next boot retries.
    """
    try:
        migrate_legacy_claude_state(LEGACY_ROOT_HOME, Path.home())
    except (OSError, shutil.Error) as e:
        logger.opt(exception=e).error(
            "Failed to migrate legacy claude state; continuing boot"
        )


def main() -> None:
    logger.info("Bootstrap starting: first-boot setup, then supervisord")

    # Move any root-homed claude state (pre-/home/user-layout workspaces) into
    # the current home BEFORE anything claude-related starts, so an updated
    # workspace keeps its chat history and sign-in.
    _migrate_legacy_claude_state_best_effort()

    # Apply the global git config (https rewrites) before any service or
    # agent runs git.
    _configure_git_global()

    # Every boot, not once: `pool_bake` unsets the repo identity on finalize and expects the
    # adopted workspace to supply it again. Only-if-unset, so it never overwrites the user's.
    _ensure_git_identity()

    # Commit the rsynced template and put the work_dir on `main`. Its OWN one-shot signal:
    # "does this workspace have a main branch" and "does it have a chat" are different
    # questions with different answers, so they cannot share one.
    _initialize_workspace_main_branch()

    # Converge the workspace venv BEFORE the initial chat agent is created
    # (below) and before supervisord's `uv run` services start, so nothing
    # races the reconcile or runs against a bake-stale venv.
    _sync_workspace_venv()

    # Set the container clock to the user's timezone so cron schedules run in
    # their local time. Must precede _exec_supervisord: cron reads the
    # timezone once at daemon start.
    tz_name = _fetch_user_timezone()
    if tz_name:
        _apply_container_timezone(tz_name)


    # Overlay symlinks must exist before services start writing.
    _run_env_converge_fast_phase()

    # Reinstall any cron entries persisted under data/.state/cron.d (e.g.
    # the Caretaker's schedule) so they survive container recreation. Must
    # precede _exec_supervisord so entries exist before cron starts.
    _install_runtime_cron_entries()

    # Make sure supervisord's log directory exists, then hand off: replace this
    # process with supervisord in the foreground. supervisord owns every
    # background service from here on (see system/supervisord.conf).
    _ensure_supervisor_log_dir()
    _exec_supervisord()


if __name__ == "__main__":
    main()
