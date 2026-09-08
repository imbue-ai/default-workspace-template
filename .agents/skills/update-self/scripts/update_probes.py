"""Liveness and frontend probes: the pre-flight boots of the merged shell and chat app,
the shell's health poll, the instances-API poll of every critical app that serves one,
the served-bundle check, and the view refresh that follows a change.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Callable, NamedTuple, Sequence

from update_banding import ExpendWrapper, as_expendable
from update_layout import (
    APPS_DIR,
    APPS_REGISTRY_PATH,
    CHAT_DIR,
    CHAT_TOOL_NAME,
    MANIFEST_FILENAME,
    SYSTEM_INTERFACE_DIR,
    TOOL_NAME,
)
from update_runtime import (
    FetchedPage,
    FrontendProbe,
    HttpClient,
    Runner,
    Spawner,
    find_free_port,
    tail,
)

# The shared post-change refresh motion, repo-relative. It owns *how* a changed
# interface is pushed to whoever is looking; this script only decides *when*.
_REFRESH_SCRIPT = "system/scripts/refresh_workspace_view.py"

_REFRESH_TIMEOUT_SECONDS = 120.0

# Header the backend stamps on the app shell: ``false`` on the "not built"
# placeholder, ``true`` on the real app.
FRONTEND_BUILT_HEADER = "x-frontend-built"

# The hashed module script the built index.html loads -- what distinguishes the
# real app shell from the placeholder even on a backend too old for the header.
_ASSET_REFERENCE_PATTERN = re.compile(r"/assets/([A-Za-z0-9._-]+\.js)")

# The shell's probe route (the workspace app model, contracts section 5): alive,
# and whether the built frontend is being served.
HEALTH_PATH = "/api/health"

# The chat app's own probe route, polled by its pre-flight boot: ``--preflight`` runs no
# agent manager, so the instances API is not there to ask, and health is the boot
# having imported mngr and the harness plugins and bound its socket.
CHAT_HEALTH_PATH = "/api/health"
# The chat program's entry point; a tree without it has no chat program to pre-flight.
CHAT_PROGRAM_ENTRY = f"{CHAT_DIR}/imbue/chat/main.py"

# The instances API (the workspace app model, contracts section 4), polled after the
# restart on every critical app that serves instances: the route the shell reads, so
# it is the one that says the app is usable rather than merely bound. It answers 503
# while the app is initialising (the chat, until its first agent list arrives) and
# hangs when the app's own machinery is stuck -- both of which a plain health route
# would pass over.
INSTANCES_PATH = "/_instances"

SERVE_PATH = "/"

# Poll budgets. The health and pre-flight budgets are deliberately generous: a
# loaded workspace boots a healthy backend well past the 30s the old reveal
# allowed, and a budget that is too short reads as "your change was bad" over a
# change that was fine -- with the whole release as blast radius and a retry
# that is correctly refused. A budget that is too long costs seconds only on a
# genuinely broken change (the pre-flight also stops early when the boot
# process dies). Tune these down against the per-phase timings the apply
# marker records, not by guesswork.
HEALTH_ATTEMPTS = 240

HEALTH_INTERVAL_SECONDS = 1.0

_PREFLIGHT_ATTEMPTS = 240

_PREFLIGHT_INTERVAL_SECONDS = 1.0

_FRONTEND_PROBE_ATTEMPTS = 5

_FRONTEND_PROBE_INTERVAL_SECONDS = 1.0

_PREFLIGHT_OUTPUT_TAIL_LINES = 40


def wait_healthy(
    http: HttpClient,
    url: str,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
    should_stop: Callable[[], bool] | None = None,
) -> bool:
    """Poll ``url`` until it returns HTTP 200, up to ``attempts`` times."""
    for index in range(attempts):
        if http.get_status(url, timeout=5.0) == 200:
            return True
        if should_stop is not None and should_stop():
            return False
        if index < attempts - 1:
            sleeper(interval)
    return False


def has_chat_program(repo_root: Path) -> bool:
    """Whether the tree runs the chat as its own program, so it can be pre-flighted."""
    return (repo_root / CHAT_PROGRAM_ENTRY).is_file()


class CriticalInstanceApp(NamedTuple):
    """An app the apply holds to its instances API after the restart: one whose
    manifest says ``critical = true`` and ``instances = true``.

    ``instances_url`` is the manifest's own declaration when it makes one (the
    terminal's sidecar port); ``None`` means the API lives at the app URL, which
    only the registry knows. ``program`` is the supervisord program that runs
    it, whose pid the settled verdict holds steady.
    """

    name: str
    instances_url: str | None
    program: str = ""


def read_critical_instance_apps(repo_root: Path) -> tuple[CriticalInstanceApp, ...]:
    """Every critical app with an instances API in the tree at ``repo_root``, in
    directory order.

    Read off the tree being applied (the merged tree, or the restored one on
    rollback) rather than the registry: right after the restart the registry
    still holds whatever rows the programs wrote before it, so the manifests are
    what say which apps the tree runs. A tree with no manifests probes nothing
    but the shell. A manifest that will not parse or
    names no app is skipped with a note: this runs on the rollback path too, where
    an exception would escape the apply's last line of defense.
    """
    apps_dir = repo_root / APPS_DIR
    if not apps_dir.is_dir():
        return ()
    apps: list[CriticalInstanceApp] = []
    for directory in sorted(apps_dir.iterdir()):
        manifest_path = directory / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        try:
            manifest = tomllib.loads(manifest_path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as exc:
            sys.stderr.write(
                f"note: skipping the app at {directory} for the post-restart probes: "
                f"its {MANIFEST_FILENAME} could not be read ({exc}).\n"
            )
            continue
        name = manifest.get("name")
        if not isinstance(name, str) or not name:
            sys.stderr.write(
                f"note: skipping the app at {directory} for the post-restart probes: "
                f"its {MANIFEST_FILENAME} names no app.\n"
            )
            continue
        if (
            manifest.get("critical") is not True
            or manifest.get("instances") is not True
        ):
            continue
        declared_url = manifest.get("instances_url")
        program = manifest.get("program")
        apps.append(
            CriticalInstanceApp(
                name=name,
                instances_url=(
                    declared_url
                    if isinstance(declared_url, str) and declared_url
                    else None
                ),
                program=program if isinstance(program, str) and program else name,
            )
        )
    return tuple(apps)


# The supervisord program that runs the live shell, and the client used to ask
# whether a program has settled.
SHELL_PROGRAM = "system_interface"
_SUPERVISORCTL = "supervisorctl"
# How many consecutive healthy answers (one poll interval apart) make a verdict.
# One 200 is a point-in-time probe, not settled state: it reads green in a gap
# between two restarts and red on a change that was never broken. Since this
# verdict is what arms the automatic rollback, both directions are expensive --
# green ships a stack that is still turning over, red reverts a good change.
SETTLED_HEALTHY_PROBES = 3


def parse_supervisor_pid(status_line: str) -> str | None:
    """The pid one ``supervisorctl status`` line reports, or None if it is not RUNNING.

    The line reads ``chat   RUNNING   pid 1234, uptime 0:00:05``. Every other
    state (STARTING, BACKOFF, FATAL, STOPPED) names no settled process, so it
    answers None -- the same answer a supervisorctl that could not be reached
    gets, because neither is evidence the program has settled.
    """
    if "RUNNING" not in status_line:
        return None
    marker = "pid "
    index = status_line.find(marker)
    if index == -1:
        return None
    return status_line[index + len(marker) :].split(",")[0].strip() or None


def read_supervisor_pids(
    repo_root: Path, runner: Runner, programs: Sequence[str]
) -> dict[str, str | None]:
    """Ask supervisord for each program's pid in one call (None where not RUNNING)."""
    result = runner.run(
        [_SUPERVISORCTL, "status", *programs],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    pids: dict[str, str | None] = {program: None for program in programs}
    for line in (getattr(result, "stdout", "") or "").splitlines():
        fields = line.split()
        if fields and fields[0] in pids:
            pids[fields[0]] = parse_supervisor_pid(line)
    return pids


def wait_settled(
    http: HttpClient,
    repo_root: Path,
    runner: Runner,
    sleeper: Callable[[float], None],
    *,
    shell_url: str,
    programs: Sequence[str],
    instance_apps: Sequence[CriticalInstanceApp],
    require_stable_pid: bool,
    attempts: int = HEALTH_ATTEMPTS,
) -> str | None:
    """Whether the live workspace reaches -- and holds -- a healthy state; None when it does.

    Healthy is the shell's health answering together with every ``instance_apps``
    entry's instances API answering (re-reading the registry each time, as the
    apps re-register). It takes ``SETTLED_HEALTHY_PROBES`` consecutive healthy
    answers rather than one, and, when the caller has just restarted programs,
    supervisord reporting every one of ``programs`` RUNNING on the same pids
    throughout. A pid that turns over mid-run restarts the confirmation instead
    of failing it: the stack is still settling (the services agent is
    supervisord's parent, so a restart turns every program over), which is the
    situation this exists to wait out rather than to judge.

    Returns what the last attempt found when the budget runs out, so the caller
    can say which app or program never settled.
    """
    healthy_streak = 0
    streak_pids: dict[str, str | None] | None = None
    last_finding = "the budget ran out before the first probe"
    for index in range(attempts):
        finding: str | None = None
        if http.get_status(shell_url, timeout=5.0) != 200:
            finding = f"the shell's health at {shell_url} did not answer 200"
        for app in instance_apps:
            if finding is not None:
                break
            url = instances_probe_url(repo_root, app)
            if url is None:
                finding = _describe_missing_registry_url(repo_root, app.name)
            else:
                page = http.get_page(url, timeout=5.0)
                if not is_instances_answer(page):
                    finding = (
                        f"the {app.name} app's instances API at "
                        f"{_describe_instances_non_answer(url, page)}"
                    )
        pids: dict[str, str | None] | None = None
        if finding is None and require_stable_pid:
            pids = read_supervisor_pids(repo_root, runner, programs)
            not_running = sorted(
                program for program, pid in pids.items() if pid is None
            )
            if not_running:
                finding = (
                    f"supervisord does not report {', '.join(not_running)} RUNNING"
                )
        if finding is None:
            if healthy_streak == 0 or pids != streak_pids:
                healthy_streak = 1
                streak_pids = pids
            else:
                healthy_streak += 1
            if healthy_streak >= SETTLED_HEALTHY_PROBES:
                return None
            last_finding = "the workspace answered healthy but had not yet held it"
        else:
            healthy_streak = 0
            streak_pids = None
            last_finding = finding
        if index < attempts - 1:
            sleeper(HEALTH_INTERVAL_SECONDS)
    return last_finding


def _read_registry_rows(repo_root: Path) -> list:
    """The registry's ``apps`` rows; raises whatever reading or parsing it raised."""
    return tomllib.loads((repo_root / APPS_REGISTRY_PATH).read_text()).get("apps", [])


def registry_app_url(repo_root: Path, app_name: str) -> str | None:
    """The ``url`` of the registry row named ``app_name``, or ``None`` when the
    registry is missing, unreadable, or has no such row -- all of which read as
    "the app has not registered yet" to a poll, never as a failure (the registry
    is rewritten under the poll as apps register). What a poll that gave up saw
    is :func:`_describe_missing_registry_url`'s to tell."""
    try:
        rows = _read_registry_rows(repo_root)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for row in rows:
        if (
            isinstance(row, dict)
            and row.get("name") == app_name
            and isinstance(row.get("url"), str)
            and row["url"]
        ):
            return row["url"]
    return None


def instances_probe_url(repo_root: Path, app: CriticalInstanceApp) -> str | None:
    """Where ``app``'s instances API is reached right now: the manifest's own
    ``instances_url``, else the registry row's ``url``; ``None`` while the app has
    no row yet."""
    base = app.instances_url or registry_app_url(repo_root, app.name)
    if base is None:
        return None
    return f"{base.rstrip('/')}{INSTANCES_PATH}"


def is_instances_answer(page: FetchedPage | None) -> bool:
    """Whether a response is the instances API answering: 200 with a JSON body.

    The body's type is what tells the app from a stale registry row: until the
    restarted app re-registers at the end of its boot, its row can still name
    another server, and the shell's SPA catch-all, for one, answers 200 on any
    path -- as HTML.
    """
    return (
        page is not None and page.status == 200 and "json" in page.content_type.lower()
    )


def wait_instances_healthy(
    http: HttpClient,
    repo_root: Path,
    app: CriticalInstanceApp,
    attempts: int,
    interval: float,
    sleeper: Callable[[float], None],
) -> str | None:
    """Poll ``app``'s instances API until it answers, re-reading the registry on
    every attempt so the poll follows the app's own re-registration. Returns
    ``None`` once it answered, else what the last attempt found."""
    last_finding = ""
    for index in range(attempts):
        url = instances_probe_url(repo_root, app)
        if url is None:
            last_finding = _describe_missing_registry_url(repo_root, app.name)
        else:
            page = http.get_page(url, timeout=5.0)
            if is_instances_answer(page):
                return None
            last_finding = _describe_instances_non_answer(url, page)
        if index < attempts - 1:
            sleeper(interval)
    return last_finding


def _describe_missing_registry_url(repo_root: Path, app_name: str) -> str:
    """Why the registry names no URL for ``app_name``: a registry that does not
    exist or has no row for it means the app never registered, while one that is
    there but will not read or parse is named as such, so the failure points at
    the broken file rather than at a registration that was never the problem."""
    try:
        _read_registry_rows(repo_root)
    except FileNotFoundError:
        pass
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return (
            f"the app registry at {APPS_REGISTRY_PATH} could not be read "
            f"({type(exc).__name__}: {exc})"
        )
    return f"the app registry at {APPS_REGISTRY_PATH} never listed '{app_name}'"


def _describe_instances_non_answer(url: str, page: FetchedPage | None) -> str:
    if page is None:
        return f"{url} did not answer"
    if page.status != 200:
        return f"{url} answered HTTP {page.status}"
    return (
        f"{url} answered 200 but as '{page.content_type}' rather than JSON, so it is "
        "not the app's instances API (a registry row that still names another server)"
    )


def preflight(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    expend: ExpendWrapper = as_expendable,
) -> str | None:
    """Boot the merged shell backend on a throwaway port and probe it, without
    touching the live service. Returns ``None`` iff it serves a healthy
    response; otherwise what went wrong -- the tail of what the throwaway boot
    wrote, or, for a boot that could not be spawned at all, a line saying so."""
    port = find_free_port()
    return _preflight_boot(
        argv=[TOOL_NAME],
        cwd=repo_root / SYSTEM_INTERFACE_DIR,
        env_overrides={
            "SYSTEM_INTERFACE_HOST": "127.0.0.1",
            "SYSTEM_INTERFACE_PORT": str(port),
        },
        health_url=f"http://127.0.0.1:{port}{HEALTH_PATH}",
        what="the merged backend",
        http=http,
        spawner=spawner,
        sleeper=sleeper,
        expend=expend,
    )


def preflight_chat(
    repo_root: Path,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    expend: ExpendWrapper = as_expendable,
) -> str | None:
    """Boot the merged chat app on a throwaway port in its side-effect-free mode and
    probe it, the way :func:`preflight` boots the shell. The chat is the process that
    imports mngr and the harness plugins, so a broken plugin table or a missing
    dependency in its tool environment shows up here, before the live restart, rather
    than in the post-restart probe and its rollback. ``--preflight`` keeps the boot
    from reconciling the live account store, starting ``mngr observe``, or registering."""
    port = find_free_port()
    return _preflight_boot(
        argv=[CHAT_TOOL_NAME, "--preflight"],
        # The repo root, where supervisord runs the chat from (its paths are relative to it).
        cwd=repo_root,
        env_overrides={"CHAT_HOST": "127.0.0.1", "CHAT_PORT": str(port)},
        health_url=f"http://127.0.0.1:{port}{CHAT_HEALTH_PATH}",
        what="the merged chat app",
        http=http,
        spawner=spawner,
        sleeper=sleeper,
        expend=expend,
    )


def _preflight_boot(
    argv: list[str],
    cwd: Path,
    env_overrides: dict[str, str],
    health_url: str,
    what: str,
    http: HttpClient,
    spawner: Spawner,
    sleeper: Callable[[float], None],
    expend: ExpendWrapper,
) -> str | None:
    """Spawn a throwaway boot, wait for its health route, and always terminate it."""
    env = dict(os.environ)
    env.update(env_overrides)
    # The caller is an agent, so its environment carries MNGR_AGENT_ID, which
    # the chat app reads as its own primary agent's id; a throwaway boot must
    # never act as the calling agent. The preview flow (preview_app.py)
    # drops it for the same reason.
    env.pop("MNGR_AGENT_ID", None)
    with tempfile.TemporaryDirectory() as scratch:
        output_path = Path(scratch) / "preflight-boot.log"
        try:
            spawned = spawner.spawn(
                expend(argv),
                cwd=str(cwd),
                env=env,
                output_path=output_path,
            )
        except OSError as exc:
            # Not booting and failing is the same verdict as failing to boot,
            # and reaching this with the console script missing is exactly what
            # a tool reinstall that half-succeeded leaves behind.
            return f"{what} could not be launched ({type(exc).__name__}: {exc})"
        try:
            if wait_healthy(
                http,
                health_url,
                _PREFLIGHT_ATTEMPTS,
                _PREFLIGHT_INTERVAL_SECONDS,
                sleeper,
                should_stop=spawned.has_exited,
            ):
                return None
        finally:
            spawned.terminate()
        return tail(spawned.read_output(), _PREFLIGHT_OUTPUT_TAIL_LINES)


def probe_frontend(http: HttpClient, base_url: str) -> FrontendProbe:
    """Ask the live UI whether it is serving a working frontend.

    Asks the two questions a browser would -- is this the real app shell, and
    does its module script actually load as JavaScript -- which together cover
    both the missing-bundle state and the blank screen an unserved ``/assets``
    path produces.
    """
    shell = http.get_page(f"{base_url}{SERVE_PATH}", timeout=10.0)
    if shell is None:
        return FrontendProbe(
            "the live service did not answer a request for the app shell",
            is_answered=False,
        )
    if shell.status != 200:
        return FrontendProbe(
            f"the app shell returned HTTP {shell.status}", is_answered=True
        )
    if shell.headers.get(FRONTEND_BUILT_HEADER) == "false":
        return FrontendProbe(
            "the live service is serving the 'frontend not built' placeholder -- the compiled bundle is missing",
            is_answered=True,
        )
    match = _ASSET_REFERENCE_PATTERN.search(shell.body)
    if match is None:
        return FrontendProbe(
            "the app shell loads no bundled script, so it is not the built app",
            is_answered=True,
        )
    asset_url = f"{base_url}/assets/{match.group(1)}"
    asset = http.get_page(asset_url, timeout=10.0)
    if asset is None:
        return FrontendProbe(
            f"the live service did not answer a request for the bundled script {asset_url}",
            is_answered=False,
        )
    if asset.status != 200:
        return FrontendProbe(
            f"the bundled script {asset_url} returned HTTP {asset.status}",
            is_answered=True,
        )
    if "javascript" not in asset.content_type:
        return FrontendProbe(
            f"the bundled script {asset_url} came back as '{asset.content_type}' rather than JavaScript, "
            "so the browser will refuse it and render a blank page",
            is_answered=True,
        )
    return FrontendProbe(None, is_answered=True)


def _probe_frontend_until_answered(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> FrontendProbe:
    """:func:`probe_frontend`, retrying until the service actually answers.

    Only a *non-answer* is retried: a verdict -- the placeholder, a bad status,
    a script served as HTML -- is the service telling us the frontend really is
    broken, and asking again reaches the same conclusion more slowly.
    """
    probe = probe_frontend(http, base_url)
    for _ in range(_FRONTEND_PROBE_ATTEMPTS - 1):
        if probe.is_answered:
            return probe
        sleeper(_FRONTEND_PROBE_INTERVAL_SECONDS)
        probe = probe_frontend(http, base_url)
    return probe


def describe_frontend_failure(
    http: HttpClient, base_url: str, sleeper: Callable[[float], None]
) -> str | None:
    """Return why the live UI is not serving a working frontend, or ``None``."""
    return _probe_frontend_until_answered(http, base_url, sleeper).failure


def refresh_workspace_view(repo_root: Path, runner: Runner) -> None:
    """Ask every open view of this workspace to reload the changed interface.

    Best-effort and never fatal: the change is already on disk and will load on
    the next visit regardless.
    """
    try:
        completed = runner.run(
            [sys.executable, str(repo_root / _REFRESH_SCRIPT)],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=_REFRESH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"refresh: could not run {_REFRESH_SCRIPT} ({type(exc).__name__}: {exc}); "
            "an open view may still be showing the previous build until reloaded.\n"
        )
        return
    if completed.stderr:
        sys.stderr.write(completed.stderr)
