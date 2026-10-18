"""The Screen through the real desktop (docs/system/specs/screen/spec.md section 20, "Acceptance"): Imbue Studio's
Electron app creates a local Docker workspace from this tree, and Playwright drives it over CDP to open the Screen,
see a frame, take control, type into a terminal on the Screen, open the browser launch path with a URL and see the
tab, close the window with the stack still running, and quit with everything gone.

The desktop app lives in the mngr monorepo, so the test runs on a developer's machine with ``MNGR_WORKTREE`` naming
a synced checkout of it (Electron installed under ``apps/minds``), Docker on the host, a display, and
``MNGR_INTERNAL_GIT_TOKEN`` for the workspace image's build; ``system/apps/screen/README.md`` has the recipe. It is
skipped at collection where the variable is unset, which includes the CI job inside the workspace image, where
nothing of this can run (no Docker on the host, no desktop app).
"""

import io
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final
from uuid import uuid4

import httpx
import pytest
import tomlkit
from PIL import Image
from loguru import logger
from playwright._impl._api_structures import SetCookieParam
from playwright.sync_api import Browser
from playwright.sync_api import Cookie
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Frame
from playwright.sync_api import Page
from playwright.sync_api import Playwright
from playwright.sync_api import sync_playwright
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

MNGR_WORKTREE_ENV_VAR: Final[str] = "MNGR_WORKTREE"
_MNGR_INTERNAL_GIT_TOKEN_ENV_VAR: Final[str] = "MNGR_INTERNAL_GIT_TOKEN"
_VIEWER_CHROMIUM_ENV_VAR: Final[str] = "SCREEN_ACCEPTANCE_CHROMIUM"

if os.environ.get(MNGR_WORKTREE_ENV_VAR, "") == "":
    pytest.skip(
        f"{MNGR_WORKTREE_ENV_VAR} is not set: the Screen's acceptance test drives Imbue Studio's desktop app from the"
        " mngr monorepo on a developer's machine (system/apps/screen/README.md)",
        allow_module_level=True,
    )

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(3600)]

_REPO_ROOT: Final[Path] = Path(__file__).resolve().parents[3]
_IN_WORKSPACE_ROOT: Final[str] = "/home/user/workspace"
# The tier the Electron app runs as: a committed, inert client config whose connector URLs never resolve, the one
# the minds e2e tests boot under; its root name is minted per run so no legacy dotfolder is ever migrated.
_INERT_CLIENT_CONFIG: Final[Path] = Path("apps/minds/imbue/minds/config/envs/ci-snapshot/client.toml")
_ELECTRON_BINARY: Final[Path] = Path("apps/minds/node_modules/.bin/electron")
_ELECTRON_MAIN_JS: Final[Path] = Path("apps/minds/electron/main.js")
# The variables this process carries that the desktop app must not inherit: an mngr agent's identity (the app
# derives its own host dir and prefix from the minted root, and a create run by an agent is gated differently),
# and this repo's venv, which `uv run` in the mngr checkout would otherwise be pointed at.
_ENVIRONMENT_NOT_INHERITED: Final[tuple[str, ...]] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "VIRTUAL_ENV",
    "MNGR_AGENT_ID",
    "MNGR_AGENT_NAME",
    "MNGR_AGENT_STATE_DIR",
    "MNGR_AGENT_WORK_DIR",
    "MNGR_HOST_DIR",
    "MNGR_PREFIX",
    "MNGR_PROJECT_CONFIG_DIR",
    "MINDS_CHAT_ID",
)

_BACKEND_ORIGIN: Final[re.Pattern[str]] = re.compile(r"^(http://localhost:\d+)(?:/|$)")
# The workspace's desktop page: the shell app's own origin label before the workspace coordinate (the bare
# coordinate origin serves nothing; the forwarder answers its loading page there).
_WORKSPACE_ORIGIN: Final[re.Pattern[str]] = re.compile(
    r"^https?://system-interface(?:-[a-z0-9]+)?\.(?:host|agent)-[a-f0-9]+\.localhost:\d+(?:/|$)"
)
# The Screen's page, served at its own origin label under the workspace (service-per-origin).
_SCREEN_ORIGIN: Final[re.Pattern[str]] = re.compile(r"^https?://screen(?:-[a-z0-9]+)?\.")

_CDP_READY_SECONDS: Final[float] = 120.0
_BACKEND_PAGE_SECONDS: Final[float] = 120.0
_PICK_ROUND_SECONDS: Final[float] = 20.0
# The create includes a docker build of the workspace image, which the layer cache makes a few minutes and a cold
# cache many more.
_CREATE_SECONDS: Final[float] = 1800.0
_DESKTOP_SECONDS: Final[float] = 180.0
# The first boot installs the stack (Xorg, XFCE, neko, Fortress) through env-converge before the Screen can start.
_SCREEN_RUNNING_SECONDS: Final[float] = 900.0
_FRAME_SECONDS: Final[float] = 120.0
_CONTROL_SECONDS: Final[float] = 20.0
_WINDOW_SECONDS: Final[float] = 60.0
# The first-boot env.d unit downloads Fortress and neko into a fresh workspace before the browser can start.
_INSTALL_SECONDS: Final[float] = 900.0
_BROWSER_SECONDS: Final[float] = 180.0
_STACK_STATE_SECONDS: Final[float] = 60.0
_DESTROY_SECONDS: Final[float] = 600.0
_POLL_SECONDS: Final[float] = 0.5

_DESKTOP_BACKDROP: Final[str] = "[data-desktop-id]"
_LAUNCHER_FIELD: Final[str] = "[data-launcher-field]"
_LAUNCHER_INPUT: Final[str] = "[data-launcher-field] textarea"
_LAUNCHER_OVERLAY: Final[str] = "[data-launcher-overlay]"
_SCREEN_ROW: Final[str] = '[data-launch="screen:screen"]'
_SCREEN_IFRAME: Final[str] = 'iframe[data-live-page][src*="://screen"]'
_STAGE: Final[str] = "[data-screen-stage]"


class _Harness(FrozenModel):
    """Where this run keeps everything: the desktop app's checkout, the template clone it builds, and its roots."""

    mngr_worktree: Path = Field(description="The mngr monorepo checkout the Electron app runs from")
    template_clone: Path = Field(description="A plain clone of this tree at its branch, the create's build context")
    template_branch: str = Field(description="The branch the clone is at")
    root_name: str = Field(description="MINDS_ROOT_NAME for this run: a `ci` env minted so no real env is touched")
    data_home: Path = Field(description="MINDS_DATA_HOME: every minds root of this run lives under it")
    host_config_root: Path = Field(description="The Electron app's cwd: a pytest-opted-in copy of mngr's config")
    viewer_chromium: Path = Field(description="The Chromium executable the viewer is shown in")
    user_data_dir: Path = Field(description="Electron's profile for this run")
    workspace_name: str = Field(description="The workspace the create form names")
    shots_dir: Path = Field(description="Where the run's screenshots land")

    @property
    def tier(self) -> str:
        return self.root_name.removeprefix("minds-")


class _Workspace(FrozenModel):
    """The created workspace's container, for the checks that look inside it."""

    container: str = Field(description="The docker container's name")

    def run(self, command: str, timeout_seconds: float = 60.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["docker", "exec", self.container, "bash", "-lc", f"cd {_IN_WORKSPACE_ROOT} && {command}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )

    def succeeds(self, command: str) -> bool:
        return self.run(command).returncode == 0

    def supervisor_state(self, program: str) -> str:
        """The program's supervisord state word (``RUNNING``, ``STOPPED``, ...), or the failure's text."""
        completed = self.run(f"supervisorctl status {program}")
        fields = completed.stdout.split()
        return fields[1] if len(fields) >= 2 else completed.stdout.strip() or completed.stderr.strip()


def _wait_until(description: str, timeout_seconds: float, probe: Callable[[], Any]) -> Any:
    """Poll ``probe`` until it answers something truthy, failing the test with ``description`` at the deadline."""
    deadline = time.monotonic() + timeout_seconds
    pause = threading.Event()
    while time.monotonic() < deadline:
        found = probe()
        if found:
            return found
        pause.wait(_POLL_SECONDS)
    pytest.fail(f"{description} did not happen within {timeout_seconds:.0f}s")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True, timeout=300).stdout


def _opt_into_pytest_config_guard(settings_path: Path) -> None:
    """mngr refuses to run under PYTEST_CURRENT_TEST, which the app inherits, unless every config it loads opts in."""
    document = tomlkit.parse(settings_path.read_text()) if settings_path.exists() else tomlkit.document()
    document["is_allowed_in_pytest"] = True
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(tomlkit.dumps(document))


def _clone_template(destination: Path) -> str:
    """Clone this tree at its branch into ``destination`` and return the branch. A worktree's ``.git`` pointer does
    not resolve inside the image, so the create builds from a plain clone; its committed state is what is built."""
    branch = _git(_REPO_ROOT, "rev-parse", "--abbrev-ref", "HEAD").strip()
    if branch == "HEAD":
        pytest.fail(f"{_REPO_ROOT} is at a detached HEAD; check out the branch to build the workspace from")
    _git(_REPO_ROOT, "clone", "--quiet", "--branch", branch, "--single-branch", str(_REPO_ROOT), str(destination))
    _opt_into_pytest_config_guard(destination / ".mngr" / "settings.toml")
    _git_commit_all(destination, "Opt the clone's mngr config into pytest for the acceptance run")
    return branch


def _git_commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=screen-acceptance",
        "-c",
        "user.email=screen-acceptance@localhost",
        "commit",
        "-qm",
        message,
    )


def _build_harness(tmp_path: Path) -> _Harness:
    mngr_worktree = Path(os.environ[MNGR_WORKTREE_ENV_VAR]).resolve()
    for required in (_ELECTRON_BINARY, _ELECTRON_MAIN_JS, _INERT_CLIENT_CONFIG, Path(".mngr/settings.toml")):
        if not (mngr_worktree / required).exists():
            pytest.fail(f"{mngr_worktree / required} is missing; is {MNGR_WORKTREE_ENV_VAR} a synced mngr checkout?")
    if os.environ.get(_MNGR_INTERNAL_GIT_TOKEN_ENV_VAR, "") == "":
        pytest.fail(f"{_MNGR_INTERNAL_GIT_TOKEN_ENV_VAR} is not set; the workspace image's build needs it")
    if subprocess.run(["docker", "info"], capture_output=True, check=False, timeout=60).returncode != 0:
        pytest.fail("docker is not answering on this host; the workspace is a local Docker container")
    viewer_chromium = _viewer_chromium_executable()
    if viewer_chromium is None:
        pytest.fail(
            "No Chromium to show the viewer in: install Playwright's (`uv run --no-sync playwright install chromium`), "
            f"or name one with {_VIEWER_CHROMIUM_ENV_VAR} (any Playwright build under ~/.cache/ms-playwright is found)"
        )
    run_id = uuid4().hex[:8]
    template_clone = tmp_path / "template"
    branch = _clone_template(template_clone)
    host_config_root = tmp_path / "mngr_host_config"
    host_config_root.mkdir()
    _git(host_config_root, "init", "--quiet")
    settings_path = host_config_root / ".mngr" / "settings.toml"
    settings_path.parent.mkdir()
    # The clone's settings, so the create templates the desktop stacks are the template's, opted into the
    # pytest config guard; MNGR_PROJECT_CONFIG_DIR points every mngr the app runs here, including the backend's
    # own in-process load, whose cwd is the mngr checkout (whose .mngr is never opted in).
    settings_path.write_text((template_clone / ".mngr" / "settings.toml").read_text())
    _opt_into_pytest_config_guard(settings_path)
    shots_dir = tmp_path / "shots"
    shots_dir.mkdir()
    return _Harness(
        mngr_worktree=mngr_worktree,
        template_clone=template_clone,
        template_branch=branch,
        root_name=f"minds-ci-screen-{run_id}",
        data_home=tmp_path / "minds-data-home",
        host_config_root=host_config_root,
        viewer_chromium=viewer_chromium,
        user_data_dir=tmp_path / "electron-profile",
        workspace_name=f"screen-e2e-{run_id}",
        shots_dir=shots_dir,
    )


def _electron_environment(harness: _Harness) -> dict[str, str]:
    """What the Electron app inherits: the minted tier, the clone as the create form's repo, and none of this
    process's own mngr identity (the ``just minds-start`` variables, as the minds e2e harness sets them)."""
    environment = {name: value for name, value in os.environ.items() if name not in _ENVIRONMENT_NOT_INHERITED}
    environment.update(
        {
            "MINDS_ROOT_NAME": harness.root_name,
            "MINDS_CLIENT_CONFIG_PATH": str(harness.mngr_worktree / _INERT_CLIENT_CONFIG),
            "MINDS_DATA_HOME": str(harness.data_home),
            "MINDS_WORKSPACE_GIT_URL": str(harness.template_clone),
            "MINDS_WORKSPACE_BRANCH": harness.template_branch,
            "MINDS_USE_LOCAL_WORKSPACE_DEFAULTS": "1",
            "MNGR_ROOT_NAME": "mngr",
            "MNGR_PROJECT_CONFIG_DIR": str(harness.host_config_root / ".mngr"),
            # The providers that need credentials this machine may not hold are silenced for every mngr the app
            # spawns and for the destroy; the workspace is local Docker.
            # The clone's settings enable Docker only inside its `docker` create template; the app's other mngr
            # runs (discovery, the forwarder, the destroy) must see the workspace too.
            "MNGR__PROVIDERS__DOCKER__IS_ENABLED": "true",
            "MNGR__PROVIDERS__MODAL__IS_ENABLED": "false",
            "MNGR__PROVIDERS__AWS__IS_ENABLED": "false",
            "MNGR__PROVIDERS__VULTR__IS_ENABLED": "false",
            "MNGR__PROVIDERS__OVH__IS_ENABLED": "false",
            "LATCHKEY_DISABLE_COUNTING": "1",
        }
    )
    return environment


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    group = os.getpgid(process.pid)
    os.killpg(group, signal.SIGTERM)
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(group, signal.SIGKILL)
        process.wait(timeout=10)


@contextmanager
def _electron(harness: _Harness, debug_port: int, log_path: Path) -> Generator[subprocess.Popen[bytes]]:
    """The Electron app on a throwaway profile with CDP on ``debug_port``, torn down with its whole process group
    (the backend it spawns included)."""
    harness.user_data_dir.mkdir(exist_ok=True)
    command = [
        str(harness.mngr_worktree / _ELECTRON_BINARY),
        str(harness.mngr_worktree / _ELECTRON_MAIN_JS),
        f"--remote-debugging-port={debug_port}",
        f"--user-data-dir={harness.user_data_dir}",
        "--no-sandbox",
    ]
    logger.info("Launching Electron: {}", " ".join(command))
    with log_path.open("ab") as log:
        process = subprocess.Popen(
            command,
            cwd=harness.host_config_root,
            env=_electron_environment(harness),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        yield process
    finally:
        _terminate_process_group(process)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _cdp_answers(debug_port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{debug_port}/json/version", timeout=2.0).status_code == 200
    except httpx.HTTPError:
        return False


def _find_backend_page(browser: Browser) -> Page | None:
    for context in browser.contexts:
        for page in context.pages:
            if _BACKEND_ORIGIN.match(page.url):
                return page
    return None


def _connect_to_backend_page(playwright: Playwright, debug_port: int) -> tuple[Browser, Page]:
    """Attach to Electron and find the chrome page on the backend origin, reconnecting between short rounds: a
    page object attached across the app's file:// to http:// boot handoff can stay frozen on the old URL."""
    deadline = time.monotonic() + _BACKEND_PAGE_SECONDS
    while time.monotonic() < deadline:
        browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{debug_port}", timeout=60_000)
        round_deadline = time.monotonic() + _PICK_ROUND_SECONDS
        pause = threading.Event()
        while time.monotonic() < min(round_deadline, deadline):
            page = _find_backend_page(browser)
            if page is not None:
                logger.info("Electron's chrome page is at {}", page.url)
                return browser, page
            pause.wait(_POLL_SECONDS)
        browser.close()
    pytest.fail(f"No Electron page settled on the backend origin within {_BACKEND_PAGE_SECONDS:.0f}s")


def _shot(target: Page | Frame, shots_dir: Path, name: str) -> None:
    page = target.page if isinstance(target, Frame) else target
    path = shots_dir / f"{name}.png"
    try:
        page.screenshot(path=str(path))
    except PlaywrightError as e:
        logger.warning("Could not screenshot {}: {}", name, e)
        return
    logger.info("Screenshot {}", path)


def _find_workspace_frame(browser: Browser) -> Frame | None:
    for context in browser.contexts:
        for page in context.pages:
            for frame in page.frames:
                if _WORKSPACE_ORIGIN.match(frame.url):
                    return frame
    return None


def _create_workspace(browser: Browser, page: Page, harness: _Harness) -> Frame:
    """Drive the create form to a ready local Docker workspace and return its desktop's frame."""
    origin_match = _BACKEND_ORIGIN.match(page.url)
    assert origin_match is not None, page.url
    origin = origin_match.group(1)
    page.goto(f"{origin}/create", wait_until="domcontentloaded")
    page.wait_for_selector("#create-form", state="attached", timeout=10_000)
    # With no account the cloud preset would open the sign-in modal; the local preset keeps everything on this box.
    page.click('[data-preset="local"]')
    page.wait_for_selector("#toggle-advanced:visible", timeout=5_000)
    page.click("#toggle-advanced")
    page.wait_for_selector("#git_url:visible", timeout=5_000)
    page.fill("#host_name", harness.workspace_name)
    page.fill("#git_url", str(harness.template_clone))
    page.select_option("#launch_mode", "DOCKER")
    _shot(page, harness.shots_dir, "01-create-form")
    page.click("#create-submit")
    page.wait_for_selector("#creating", state="attached", timeout=10_000)
    logger.info(
        "Creating workspace {} from {} at {}", harness.workspace_name, harness.template_clone, harness.template_branch
    )

    def ready_or_failed() -> Frame | None:
        found = _find_workspace_frame(browser)
        if found is not None:
            return found
        try:
            is_failed = page.is_visible("#failure-view")
        except PlaywrightError:
            return None
        if is_failed:
            message = page.query_selector("#error-message")
            pytest.fail(f"The create failed: {message.inner_text().strip() if message is not None else 'no message'}")
        return None

    workspace: Frame = _wait_until("the workspace coming up", _CREATE_SECONDS, ready_or_failed)
    workspace.wait_for_selector(_DESKTOP_BACKDROP, state="visible", timeout=int(_DESKTOP_SECONDS * 1000))
    logger.info("The desktop rendered at {}", workspace.url)
    _shot(workspace, harness.shots_dir, "02-desktop")
    return workspace


def _find_container(harness: _Harness) -> _Workspace:
    def workspace_container() -> str | None:
        listing = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=True, timeout=30
        )
        for name in listing.stdout.split():
            if harness.workspace_name in name and "docker-state" not in name:
                return name
        return None

    container = _wait_until("the workspace's container appearing", _WINDOW_SECONDS, workspace_container)
    logger.info("The workspace's container is {}", container)
    return _Workspace(container=container)


def _viewer_chromium_executable() -> Path | None:
    """The Chromium that shows the viewer: the named one, else this venv's Playwright build, else the newest
    Playwright build in the cache (another venv's), since Playwright may have no build for this OS release."""
    named = os.environ.get(_VIEWER_CHROMIUM_ENV_VAR, "")
    if named:
        return Path(named)
    with sync_playwright() as playwright:
        own = Path(playwright.chromium.executable_path)
    if own.exists():
        return own
    cached = sorted(Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"))
    return cached[-1] if cached else None


def _cookie_to_set(cookie: Cookie) -> SetCookieParam:
    """The desktop session's cookie as a cookie to set: Playwright reads and writes two different shapes."""
    return SetCookieParam(
        name=cookie["name"],
        value=cookie["value"],
        domain=cookie["domain"],
        path=cookie["path"],
        expires=cookie["expires"],
        httpOnly=cookie["httpOnly"],
        secure=cookie["secure"],
        sameSite=cookie["sameSite"],
    )


def _open_screen_from_launcher(
    workspace: Frame, playwright: Playwright, harness: _Harness
) -> tuple[Frame, str, Browser]:
    """Press the Screen's launcher row; return the viewer's frame, the window id, and the browser showing it.

    Electron exposes the shell's frame to Playwright but neither the Screen's frame nested inside it nor a new
    page, so the viewer is shown in a Chromium of its own at the same address, through the same desktop proxy,
    carrying the desktop session's cookies; the caller closes that browser.
    """
    if workspace.query_selector(f"{_LAUNCHER_OVERLAY}:visible") is None:
        opener = _LAUNCHER_INPUT if workspace.query_selector(_LAUNCHER_INPUT) is not None else _LAUNCHER_FIELD
        workspace.click(opener)
    row = f"{_LAUNCHER_OVERLAY}:visible {_SCREEN_ROW}"
    workspace.wait_for_selector(row, state="visible", timeout=int(_WINDOW_SECONDS * 1000))
    workspace.click(row)
    workspace.wait_for_selector(_SCREEN_IFRAME, state="attached", timeout=int(_WINDOW_SECONDS * 1000))
    window_id = workspace.eval_on_selector(_SCREEN_IFRAME, "(el) => el.getAttribute('data-live-page')")
    viewer_url: str = _wait_until(
        "the Screen's page getting its address",
        _WINDOW_SECONDS,
        lambda: workspace.eval_on_selector(_SCREEN_IFRAME, "(el) => el.src") or None,
    )
    assert _SCREEN_ORIGIN.match(viewer_url), viewer_url
    viewer_browser = playwright.chromium.launch(executable_path=harness.viewer_chromium)
    viewer_context = viewer_browser.new_context(ignore_https_errors=True, viewport={"width": 1280, "height": 800})
    viewer_context.add_cookies([_cookie_to_set(cookie) for cookie in workspace.page.context.cookies()])
    viewer_page = viewer_context.new_page()
    viewer_page.goto(viewer_url, wait_until="domcontentloaded")
    screen = viewer_page.main_frame
    logger.info("The Screen's window {} shows {}, driven in a browser of its own", window_id, viewer_url)
    return screen, window_id, viewer_browser


def _service_get(screen: Frame, path: str) -> dict[str, Any]:
    """A GET on the Screen service from its own page, through the desktop's proxy with the viewer's identity."""
    answer = screen.evaluate(
        "async (path) => { const r = await fetch(path); return {status: r.status, body: await r.text()}; }", path
    )
    assert answer["status"] == 200, (path, answer)
    return json.loads(answer["body"])


def _video_ready_state(screen: Frame) -> int:
    return screen.evaluate("() => { const v = document.querySelector('video'); return v ? v.readyState : -1; }")


def _grey_level_range(png: bytes) -> int:
    """The spread between the darkest and the brightest grey level in the picture: a black or frozen frame is flat,
    a desktop (the image's XFCE wallpaper is black, its panel light) is not."""
    with Image.open(io.BytesIO(png)) as image:
        histogram = image.convert("L").histogram()
    present = [level for level, count in enumerate(histogram) if count > 0]
    return (present[-1] - present[0]) if present else 0


_DESKTOP_GREY_LEVEL_SPREAD: Final[int] = 64


def _desktop_frame(screen: Frame) -> bytes | None:
    """The video element's current picture when it shows a desktop (its grey levels spread at least
    ``_DESKTOP_GREY_LEVEL_SPREAD``), or None while it is still black or frozen."""
    png = screen.locator("video").screenshot()
    return png if _grey_level_range(png) >= _DESKTOP_GREY_LEVEL_SPREAD else None


def _window_matching(screen: Frame, predicate: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    return next((window for window in _service_get(screen, "/api/windows")["windows"] if predicate(window)), None)


def _is_frontmost(screen: Frame, window_id: str) -> bool:
    """Whether the Screen's title follows the window: ``/api/state`` carries the frontmost window's title."""
    window = _window_matching(screen, lambda w: w["id"] == window_id)
    return window is not None and _service_get(screen, "/api/state")["title"] == window["title"]


def _kill_processes_referencing(tokens: tuple[str, ...]) -> None:
    """End every process whose command line carries one of this run's names (the root name, or the data home the
    app's detached latchkey forwarder is pointed at), each by its exact pid."""
    own_pid = os.getpid()
    listing = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=False, timeout=30)
    pids = [
        int(line.split(None, 1)[0])
        for line in listing.stdout.splitlines()
        if any(token in line for token in tokens) and int(line.split(None, 1)[0]) != own_pid
    ]
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    if pids:
        logger.info("Ended {} process(es) referencing {}", len(pids), tokens)


def _destroy_workspace(harness: _Harness) -> None:
    """Destroy the workspace through the desktop app's own mngr state, then remove any container or volume of this
    run that outlived it."""
    host_dirs = list(harness.data_home.glob(f"{harness.tier}/*/mngr"))
    if host_dirs:
        environment = {
            **_electron_environment(harness),
            "MNGR_HOST_DIR": str(host_dirs[0]),
            "MNGR_PREFIX": f"{harness.root_name}-",
            "MNGR_PROJECT_CONFIG_DIR": str(harness.host_config_root / ".mngr"),
        }
        command = ["uv", "run", "--no-sync", "--project", str(harness.mngr_worktree), "mngr", "destroy"]
        command += [harness.workspace_name, "--force"]
        completed = subprocess.run(
            command,
            cwd=harness.host_config_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=_DESTROY_SECONDS,
        )
        logger.info("mngr destroy exited {}: {}", completed.returncode, (completed.stderr or completed.stdout)[-800:])
    else:
        logger.warning("No mngr host dir under {}; removing this run's containers directly", harness.data_home)
    for kind, listing_command, remove_command in (
        ("container", ["docker", "ps", "-aq", "--filter", f"name={harness.root_name}-"], ["docker", "rm", "-f"]),
        (
            "volume",
            ["docker", "volume", "ls", "-q", "--filter", f"name={harness.root_name}-"],
            ["docker", "volume", "rm"],
        ),
    ):
        leftovers = subprocess.run(
            listing_command, capture_output=True, text=True, check=False, timeout=60
        ).stdout.split()
        if leftovers:
            subprocess.run([*remove_command, *leftovers], capture_output=True, check=False, timeout=120)
            logger.warning("Removed {} leftover {}(s) of {}", len(leftovers), kind, harness.root_name)


def _running_state(screen: Frame) -> dict[str, Any] | None:
    state = _service_get(screen, "/api/state")
    return state if state["lifecycle"] == "running" else None


def _drive_the_screen(workspace: Frame, harness: _Harness, inside: _Workspace, playwright: Playwright) -> None:
    # Open the Screen and see a frame.
    screen, window_id, viewer = _open_screen_from_launcher(workspace, playwright, harness)
    state = _wait_until("the Screen's stack running", _SCREEN_RUNNING_SECONDS, lambda: _running_state(screen))
    assert state["screen_size"] == {"width": 1920, "height": 1080, "rate": 60}, state
    _wait_until("the video decoding (readyState 4)", _FRAME_SECONDS, lambda: _video_ready_state(screen) == 4)
    frame_png: bytes = _wait_until(
        "a frame with a desktop's spread of grey levels", _FRAME_SECONDS, lambda: _desktop_frame(screen)
    )
    (harness.shots_dir / "03-first-frame.png").write_bytes(frame_png)
    logger.info("A frame is showing (grey levels span {})", _grey_level_range(frame_png))

    # Take control: with nobody controlling, the first input is neko's control/request and the service records it.
    screen.click(_STAGE, position={"x": 400, "y": 300})
    _wait_until(
        "this viewer becoming the controller",
        _CONTROL_SECONDS,
        lambda: screen.query_selector(f'{_STAGE}[data-screen-controller="you"]') is not None,
    )
    assert "Control: You" in screen.inner_text("[data-screen-controller]")
    assert _service_get(screen, "/api/state")["control"]["controller"] == "human"

    # Type into a terminal on the Screen and see the text land: the command it runs writes a marker file.
    started = inside.run("uv run --no-sync screen run xfce4-terminal")
    assert started.returncode == 0, started.stderr
    terminal = _wait_until(
        "the terminal's window appearing",
        _WINDOW_SECONDS,
        lambda: _window_matching(screen, lambda w: "xfce4-terminal" in w["window_class"].lower()),
    )
    assert inside.succeeds(f"uv run --no-sync screen activate {terminal['id']}")
    _wait_until("the terminal coming to the front", _WINDOW_SECONDS, lambda: _is_frontmost(screen, terminal["id"]))
    marker = f"{_IN_WORKSPACE_ROOT}/data/screen-acceptance-{uuid4().hex[:8]}.txt"
    screen.click(_STAGE, position={"x": 400, "y": 300})
    screen.page.keyboard.type(f"touch {marker}", delay=40)
    screen.page.keyboard.press("Enter")
    _wait_until(
        "the typed command creating the marker file", _WINDOW_SECONDS, lambda: inside.succeeds(f"test -f {marker}")
    )
    _shot(screen, harness.shots_dir, "04-terminal-typed")
    logger.info("The typed command ran on the Screen ({})", marker)

    # The browser launch path with a URL: Chromium comes up on the Screen with the page as its tab. Fortress is
    # installed by the first-boot env.d unit, which may still be running in a workspace this young.
    _wait_until(
        "the first-boot install finishing (Fortress)",
        _INSTALL_SECONDS,
        lambda: inside.supervisor_state("env-converge") == "EXITED"
        and inside.succeeds("test -x /opt/fortress/tilion-fortress/tilion"),
    )
    page_url = "https://example.com/"
    opened = inside.run(f"uv run --no-sync workspace-layout open {page_url}", timeout_seconds=120.0)
    assert opened.returncode == 0, (opened.stdout, opened.stderr)
    _wait_until(
        "Chromium's window showing the page",
        _BROWSER_SECONDS,
        lambda: _window_matching(
            screen,
            lambda w: any(m in w["window_class"].lower() for m in ("tilion", "chrome", "chromium"))
            and "Example Domain" in w["title"],
        ),
    )
    state = _service_get(screen, "/api/state")
    assert state["chromium"]["state"] == "running", state["chromium"]
    assert state["title"] == "Browser", state["title"]
    _wait_until(
        "the browser's tab listed by the fleet",
        _WINDOW_SECONDS,
        lambda: any(tab["url"] == page_url for tab in _service_get(screen, "/browsers")["browsers"][0]["tabs"]),
    )
    _shot(screen, harness.shots_dir, "05-browser-tab")

    # Close the window: the stack runs on (D3).
    workspace.click(f'[data-window-id="{window_id}"] [data-window-control="close"]')
    workspace.wait_for_selector(_SCREEN_IFRAME, state="detached", timeout=int(_WINDOW_SECONDS * 1000))
    workspace.wait_for_timeout(5000)
    assert inside.supervisor_state("screen-stack:screen-neko") == "RUNNING"
    assert inside.supervisor_state("screen") == "RUNNING"
    assert inside.succeeds('curl -fsS http://127.0.0.1:8090/api/state | grep -q \'"lifecycle": *"running"\'')
    logger.info("The window is closed and the stack still runs")

    viewer.close()

    # Open it again (one more viewer of the same Screen), then Quit from the window menu: everything is gone.
    screen, window_id, viewer = _open_screen_from_launcher(workspace, playwright, harness)
    _wait_until("the reopened Screen's video", _FRAME_SECONDS, lambda: _video_ready_state(screen) == 4)
    workspace.click(f'[data-window-id="{window_id}"] [data-window-control="menu"]')
    workspace.wait_for_selector('[data-menu-row="quit"]', state="visible", timeout=10_000)
    _shot(workspace, harness.shots_dir, "06-window-menu")
    workspace.click('[data-menu-row="quit"]')
    workspace.wait_for_selector(_SCREEN_IFRAME, state="detached", timeout=int(_WINDOW_SECONDS * 1000))
    _wait_until(
        "the service stopping",
        _STACK_STATE_SECONDS,
        lambda: inside.supervisor_state("screen") in ("STOPPED", "EXITED"),
    )
    _wait_until(
        "the stack's members stopping",
        _STACK_STATE_SECONDS,
        lambda: all(
            inside.supervisor_state(f"screen-stack:{member}") in ("STOPPED", "EXITED")
            for member in ("screen-neko", "screen-session", "screen-pulse", "screen-xorg")
        ),
    )
    _wait_until(
        "the display socket going away", _STACK_STATE_SECONDS, lambda: inside.succeeds("test ! -e /tmp/.X11-unix/X99")
    )
    assert not inside.succeeds("pgrep -x Xorg") and not inside.succeeds("pgrep -x neko")
    logger.info("Quit ended the service and every member of the stack")
    _shot(workspace, harness.shots_dir, "07-after-quit")
    viewer.close()


def test_the_screen_works_through_the_desktop_app_on_a_local_docker_workspace(tmp_path: Path) -> None:
    """The whole journey of spec section 20's acceptance row, in one run against one workspace."""
    harness = _build_harness(tmp_path)
    debug_port = _free_port()
    logger.info("Run {}: workspace {} on CDP port {}", harness.root_name, harness.workspace_name, debug_port)
    try:
        with _electron(harness, debug_port, tmp_path / "electron.log"):
            _wait_until("Electron's CDP endpoint answering", _CDP_READY_SECONDS, lambda: _cdp_answers(debug_port))
            with sync_playwright() as playwright:
                browser, page = _connect_to_backend_page(playwright, debug_port)
                try:
                    workspace = _create_workspace(browser, page, harness)
                    inside = _find_container(harness)
                    _drive_the_screen(workspace, harness, inside, playwright)
                finally:
                    browser.close()
    finally:
        _destroy_workspace(harness)
        _kill_processes_referencing((harness.root_name, str(harness.data_home)))
        shutil.rmtree(harness.user_data_dir, ignore_errors=True)
