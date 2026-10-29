"""The browser on the Screen (docs/system/specs/screen/spec.md section 16): one Chromium with one profile, started
by the ``browser`` launch path, ``screen run`` or an agent's ``new``, its windows ordinary X windows the window
manager places, its tabs checkpointed to the manifest and restored when it is next started.

The fleet keeps the browser app's shape (browsers by name, a cap of one, the fleet API's verbs) and owns Chromium's
lifecycle, the fleet's own CDP channel (the tab list, foregrounding, crash detection) and the gated CDP endpoint
agents drive through. It does not own the lease: that is the Screen's ``ScreenControl``, which the proxy's
per-frame gate and the fleet verbs act on.
"""

import secrets as secrets_module
import shutil
import threading
from collections.abc import Callable
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel
from imbue.imbue_common.pure import pure
from screen.browser_manifest import BrowserManifest
from screen.browser_manifest import ManifestEntry
from screen.browser_manifest import ManifestStore
from screen.browser_names import first_free_numbered_browser_name
from screen.browser_names import is_valid_browser_name
from screen.cdp_client import CdpClientInterface
from screen.cdp_client import CdpConnectorInterface
from screen.cdp_client import CdpTarget
from screen.cdp_proxy import BrowserProxy
from screen.cdp_proxy import ProxyServer
from screen.chrome_launcher import ChromiumHandleInterface
from screen.chrome_launcher import ChromiumLaunchRequest
from screen.chrome_launcher import ChromiumLauncherInterface
from screen.chrome_launcher import extension_paths
from screen.control import ScreenControl
from screen.data_types import AcquireStatus
from screen.data_types import BrowserLifecycle
from screen.data_types import ChromiumView
from screen.data_types import ControlView
from screen.data_types import Controller
from screen.errors import BrowserNotDrivableError
from screen.errors import CdpError
from screen.errors import ChromiumNotInstalledError
from screen.errors import ChromiumStartError
from screen.errors import DuplicateBrowserNameError
from screen.errors import FleetFullError
from screen.errors import InvalidBrowserNameError
from screen.errors import UnknownBrowserError
from screen.primitives import AgentId
from screen.primitives import BROWSER_HOME_URL
from screen.primitives import BROWSER_PROFILE_DIR_PREFIX
from screen.primitives import BrowserName
from screen.primitives import FORTRESS_EXTENSIONS_DIR
from screen.primitives import MAX_BROWSERS

# The Screen's own unpacked extensions, shipped in this package.
BUNDLED_EXTENSIONS_DIR: Final[Path] = Path(__file__).parent / "extensions"
# How often the fleet polls Chromium for a crash and re-checkpoints the tabs (a no-op when nothing changed).
KEEPALIVE_SECONDS: Final[int] = 10
# Two consecutive misses debounce a transient drop the CDP client is still retrying.
_DEAD_POLLS_BEFORE_CRASH: Final[int] = 2
_CAPABILITY_TOKEN_BYTES: Final[int] = 18

_URL_PREFIXES_NOT_RESTORED: Final[tuple[str, ...]] = ("about:", "chrome:", "chrome-error:", "devtools:")


@pure
def is_restorable_url(url: str | None) -> bool:
    """Whether a tab URL is worth persisting and reopening (blank and internal pages are not)."""
    return bool(url) and not url.startswith(_URL_PREFIXES_NOT_RESTORED)


@pure
def profile_dir_for(profile_root: Path, name: BrowserName) -> Path:
    """The persistent Chromium user data directory of a browser. The prefix is historical and must not change: it
    is what an upgraded workspace's profile (its logins) is found under."""
    return profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}{name}"


@pure
def stopped_hint(name: BrowserName) -> str:
    return f"start it with `agentic-browser-fleet new`, or with `POST /browsers/{name}/start` on the Screen service"


class Browser(MutableModel):
    """One browser: its Chromium, the fleet's CDP connection to it, the agent's capability token, and what it is
    known to have open."""

    model_config = {"arbitrary_types_allowed": True}

    name: BrowserName = Field(frozen=True, description="The addressing key: the fleet CLI's argument, the paths")
    lifecycle: BrowserLifecycle = Field(default=BrowserLifecycle.INIT, description="Where its Chromium is")
    last_known_tabs: list[str] = Field(
        default_factory=list,
        description="The tabs it is known to have: what a start opens, until Chromium can be asked",
    )
    last_known_active_tab: int = Field(default=0, description="The index of the tab in front among those")
    pending_url: str | None = Field(
        default=None,
        description="A page the human asked to open while an agent drove; opened once the agent lets go",
    )
    launch_tabs: list[str] = Field(
        default_factory=list, description="Pages asked for while Chromium launched; opened in front once it is up"
    )
    is_stopped_by_user: bool = Field(
        default=False, description="Stopped on purpose (the stop verb), so a stack start leaves it stopped"
    )
    is_launch_wanted: bool = Field(
        default=False, description="Registered while the stack was down: launched when the stack next runs"
    )

    _chromium: ChromiumHandleInterface | None = PrivateAttr(default=None)
    _cdp: CdpClientInterface | None = PrivateAttr(default=None)
    _token: str = PrivateAttr(default="")
    _token_owner: AgentId | None = PrivateAttr(default=None)
    _token_owner_name: str | None = PrivateAttr(default=None)
    _active_target_id: str = PrivateAttr(default="")
    _dead_polls: int = PrivateAttr(default=0)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)

    @property
    def is_running(self) -> bool:
        return self.lifecycle == BrowserLifecycle.RUNNING

    @property
    def chromium_pid(self) -> int | None:
        return self._chromium.pid if self._chromium is not None else None

    @property
    def token(self) -> str:
        return self._token

    @property
    def token_owner(self) -> AgentId | None:
        return self._token_owner

    @property
    def token_owner_name(self) -> str | None:
        return self._token_owner_name

    def remember_tabs(self, urls: Sequence[str], active_tab: int) -> None:
        """What a start restores: the tab list and the index in front among it (the first when out of range)."""
        with self._lock:
            self.last_known_tabs = list(urls)
            self.last_known_active_tab = active_tab if 0 <= active_tab < len(urls) else 0

    def remember_tab_in_front(self, url: str) -> None:
        """A page added to a stopped browser's list, to open in front when it starts."""
        with self._lock:
            self.last_known_tabs.append(url)
            self.last_known_active_tab = len(self.last_known_tabs) - 1

    def open_tab_or_queue_for_launch(self, url: str) -> bool:
        """Open ``url`` in front when Chromium is up, else queue it for the launch still coming up; answers whether it
        was opened now. Decided under the lock: a launch that finishes meanwhile either drains the queue after the
        append or is seen as running, so the page is never queued behind a drain that will not come again."""
        with self._lock:
            if self.is_running:
                self.open_tab(url)
                return True
            self.launch_tabs.append(url)
            return False

    def open_queued_tabs(self) -> None:
        """Open what was asked for during the launch, the last in front; a tab Chromium refuses is logged."""
        with self._lock:
            urls, self.launch_tabs = list(self.launch_tabs), []
        for url in urls:
            try:
                self.open_tab(url)
            except BrowserNotDrivableError as e:
                logger.debug("A page asked for while {} launched was not opened: {}", self.name, e)

    def mark_stopped_by_user(self) -> None:
        """The stop verb: stopped on purpose, so a stack start leaves it stopped; a held page and the pages queued
        for the launch are dropped."""
        with self._lock:
            self.lifecycle = BrowserLifecycle.STOPPED
            self.is_stopped_by_user = True
            self.pending_url = None
            self.launch_tabs = []

    def mark_stopped_with_stack(self) -> None:
        """The stack ended under it: stopped for this stack life and, unless the user had stopped it, launched again
        when the stack next runs; a held page and the pages queued for the launch are dropped."""
        with self._lock:
            if self.lifecycle != BrowserLifecycle.STOPPED:
                self.is_launch_wanted = True
            self.lifecycle = BrowserLifecycle.STOPPED
            self.pending_url = None
            self.launch_tabs = []

    def mark_stopped_after_failed_launch(self) -> None:
        """A relaunch that failed: kept stopped with its tabs, the pages queued for the launch dropped."""
        with self._lock:
            self.lifecycle = BrowserLifecycle.STOPPED
            self.launch_tabs = []

    def mark_crashed(self) -> bool:
        """Chromium died under a running browser; answers whether it was running (a stopped one has not crashed)."""
        with self._lock:
            if self.lifecycle != BrowserLifecycle.RUNNING:
                return False
            self.lifecycle = BrowserLifecycle.CRASHED
            self.pending_url = None
            return True

    def mint_token(self, agent_id: AgentId | None, agent_name: str | None) -> str:
        """A fresh capability token issued to one agent: minted on every launch (a CLI session reconnecting across
        a restart is refused rather than silently driving a different Chromium) and whenever the browser passes to
        a different agent, which makes revocation race-free."""
        self._token = secrets_module.token_urlsafe(_CAPABILITY_TOKEN_BYTES)
        self._token_owner = agent_id
        self._token_owner_name = agent_name
        return self._token

    def attach(self, chromium: ChromiumHandleInterface, cdp: CdpClientInterface) -> None:
        with self._lock:
            self._chromium = chromium
            self._cdp = cdp
            self._dead_polls = 0

    def teardown_chromium(self) -> None:
        """Drop the CDP connection and end Chromium; a no-op when nothing is attached."""
        with self._lock:
            cdp, self._cdp = self._cdp, None
            chromium, self._chromium = self._chromium, None
            self._active_target_id = ""
        if cdp is not None:
            cdp.close()
        if chromium is not None:
            chromium.kill()

    def is_chromium_alive(self) -> bool:
        """Two signals: the process handle catches a kill even while nothing is attached, and the CDP ping catches
        a browser that is alive but no longer serving (with one reconnect before it counts)."""
        with self._lock:
            chromium, cdp = self._chromium, self._cdp
        if chromium is None or not chromium.is_alive() or cdp is None:
            return False
        if cdp.is_alive():
            return True
        return cdp.ping()

    def note_poll(self, is_alive: bool) -> bool:
        """Record one keepalive poll; answers whether Chromium is now to be declared crashed."""
        with self._lock:
            self._dead_polls = 0 if is_alive else self._dead_polls + 1
            return self._dead_polls >= _DEAD_POLLS_BEFORE_CRASH

    def page_targets(self) -> list[CdpTarget]:
        with self._lock:
            cdp = self._cdp
        if cdp is None:
            raise BrowserNotDrivableError(f"browser {self.name} has no connection to Chromium")
        return cdp.page_targets()

    def page_count(self) -> int:
        """For the proxy's last-page guard: zero when Chromium cannot be asked, which the guard reads as the last page
        and refuses the close, rather than wedging the socket on the error."""
        try:
            return len(self.page_targets())
        except (BrowserNotDrivableError, CdpError) as e:
            logger.debug("The page count of {} failed ({})", self.name, e)
            return 0

    def focus_and_foreground(self, target_id: str) -> None:
        """Bring a tab to the front of its window and record it as the active tab; best-effort."""
        with self._lock:
            cdp = self._cdp
            if cdp is None:
                return
            try:
                cdp.activate(target_id)
                self._active_target_id = target_id
            except CdpError as e:
                logger.debug("Foregrounding {} on {} was ignored ({})", target_id, self.name, e)

    def shown_target(self, targets: list[CdpTarget]) -> str | None:
        """The tab in front of the window, asked of the pages themselves (a human switching tabs inside Chromium
        never reaches the fleet's connection, so its own record is stale as soon as they browse); the record stands
        in when no page answers."""
        with self._lock:
            cdp = self._cdp
            cached = self._active_target_id or None
        if cdp is None:
            return None
        open_ids = [target.target_id for target in targets]
        cached_first = ([cached] if cached in open_ids else []) + [tid for tid in open_ids if tid != cached]
        for target_id in cached_first:
            try:
                is_shown = cdp.is_shown(target_id)
            except CdpError as e:
                logger.debug("The visibility of {} on {} was ignored ({})", target_id, self.name, e)
                continue
            if is_shown:
                with self._lock:
                    self._active_target_id = target_id
                return target_id
        return cached if cached in open_ids else None

    def open_tab(self, url: str) -> None:
        """Open ``url`` as a new tab in front; raises ``BrowserNotDrivableError`` when Chromium cannot."""
        with self._lock:
            cdp = self._cdp
            if cdp is None or not self.is_running:
                raise BrowserNotDrivableError(f"browser {self.name} is not running")
            try:
                target_id = cdp.create_target(url)
            except CdpError as e:
                raise BrowserNotDrivableError(f"browser {self.name} could not open a tab: {e}") from e
            self.focus_and_foreground(target_id)

    def close_active_tab(self) -> None:
        """Close the tab in front, keeping Chromium up: the last tab is replaced with a fresh home page first,
        since Chromium closes its window with it."""
        with self._lock:
            cdp = self._cdp
            if cdp is None or not self.is_running:
                raise BrowserNotDrivableError(f"browser {self.name} is not running")
            try:
                targets = cdp.page_targets()
            except CdpError as e:
                raise BrowserNotDrivableError(f"browser {self.name} cannot list its tabs: {e}") from e
            if not targets:
                return
            closing = self.shown_target(targets) or targets[-1].target_id
            remaining = [target.target_id for target in targets if target.target_id != closing]
            if not remaining:
                try:
                    remaining = [cdp.create_target(BROWSER_HOME_URL)]
                except CdpError as e:
                    raise BrowserNotDrivableError(f"browser {self.name} could not open a fresh tab: {e}") from e
            try:
                cdp.close_target(closing)
            except CdpError as e:
                raise BrowserNotDrivableError(f"browser {self.name} could not close its tab: {e}") from e
            self.focus_and_foreground(remaining[-1])

    def tab_list(self) -> list[dict[str, Any]]:
        """The tabs as ``GET /browsers`` lists them, in the order the agent's ``tab-select`` indexes, the one in
        front marked active."""
        try:
            targets = self.page_targets()
        except (BrowserNotDrivableError, CdpError) as e:
            logger.debug("The tab list of {} was skipped ({})", self.name, e)
            return []
        active = self.shown_target(targets)
        return [
            {"index": index, "title": target.title, "url": target.url, "active": target.target_id == active}
            for index, target in enumerate(targets)
        ]

    def refresh_tabs(self) -> tuple[list[str], int]:
        """The restorable tab URLs and the index of the one in front, for the manifest; when Chromium cannot answer
        the last known list stands, since a failed query is no evidence the browser has no tabs."""
        try:
            targets = self.page_targets()
        except (BrowserNotDrivableError, CdpError) as e:
            logger.debug("The tabs of {} kept their last known list ({})", self.name, e)
            return list(self.last_known_tabs), self.last_known_active_tab
        active_target = self.shown_target(targets)
        with self._lock:
            urls: list[str] = []
            active = 0
            for target in targets:
                if is_restorable_url(target.url):
                    if target.target_id == active_target:
                        active = len(urls)
                    urls.append(target.url)
            self.last_known_tabs = urls
            self.last_known_active_tab = active
            return list(urls), active


class BrowserFleet(MutableModel):
    """Owns the browser: creation, launch, stop and start, the tab manifest, the proxy registration and the
    per-frame gate over the Screen's lease."""

    model_config = {"arbitrary_types_allowed": True}

    launcher: ChromiumLauncherInterface = Field(frozen=True, description="How a Chromium is spawned")
    cdp_connector: CdpConnectorInterface = Field(frozen=True, description="How the fleet's CDP channel opens")
    proxy_server: ProxyServer = Field(frozen=True, description="The agents' gated endpoint")
    manifest_store: ManifestStore = Field(frozen=True, description="The tab manifest")
    profile_root: Path = Field(frozen=True, description="Where the profiles live")
    executable: Path = Field(frozen=True, description="The Fortress launcher")
    install_check: Callable[[], str | None] = Field(
        frozen=True, description="Why Chromium cannot launch yet (not installed), or None"
    )
    environment: Callable[[], dict[str, str]] = Field(frozen=True, description="The child's environment")
    is_sandbox_disabled: bool = Field(frozen=True, description="Whether Chromium runs with --no-sandbox")
    control: ScreenControl = Field(frozen=True, description="The Screen's lease, which the gate acts on")
    on_changed: Callable[[], None] = Field(frozen=True, description="Something the viewers show changed")
    on_agent_activity: Callable[[AgentId], None] = Field(
        frozen=True, description="A forwarded frame: the lease's idle clock and the profile's input clock"
    )
    on_chromium_processes_expected: Callable[[], None] = Field(
        frozen=True, description="Chromium is about to spawn processes: the band sweep's cue"
    )
    on_browser_running: Callable[[BrowserName], None] = Field(
        frozen=True, description="A browser came up: the service raises its window"
    )
    bundled_extensions_dir: Path = Field(default=BUNDLED_EXTENSIONS_DIR, frozen=True)
    installed_extensions_dir: Path = Field(default=FORTRESS_EXTENSIONS_DIR, frozen=True)
    max_browsers: int = Field(default=MAX_BROWSERS, frozen=True, description="The cap on launched browsers")

    _browsers: dict[str, Browser] = PrivateAttr(default_factory=dict)
    _lock: threading.RLock = PrivateAttr(default_factory=threading.RLock)
    _startup_lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)
    _is_stack_running: bool = PrivateAttr(default=False)
    _last_manifest_json: str | None = PrivateAttr(default=None)
    _ticks: int = PrivateAttr(default=0)
    _threads: list[threading.Thread] = PrivateAttr(default_factory=list)

    # reading

    def install_reason(self) -> str | None:
        return self.install_check()

    def get(self, name: BrowserName) -> Browser:
        with self._lock:
            browser = self._browsers.get(str(name))
        if browser is None:
            raise UnknownBrowserError(f"No browser {name}")
        return browser

    def the_browser(self) -> Browser | None:
        """The fleet's one browser: the one running or launching, else the first by name; None when empty."""
        with self._lock:
            by_name = sorted(self._browsers.items())
        live = next(
            (
                browser
                for _, browser in by_name
                if browser.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING)
            ),
            None,
        )
        if live is not None:
            return live
        return by_name[0][1] if by_name else None

    def browsers(self) -> list[Browser]:
        with self._lock:
            return [browser for _, browser in sorted(self._browsers.items())]

    def launched_count(self) -> int:
        with self._lock:
            return sum(
                1
                for browser in self._browsers.values()
                if browser.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING)
            )

    def chromium_pids(self) -> list[int]:
        return [pid for browser in self.browsers() if (pid := browser.chromium_pid) is not None]

    def chromium_view(self) -> ChromiumView:
        browser = self.the_browser()
        if browser is None:
            return ChromiumView(state="absent")
        return ChromiumView(state=browser.lifecycle.value.lower(), name=browser.name, pending_url=browser.pending_url)

    def describe(self, browser: Browser, view: ControlView) -> dict[str, Any]:
        """One browser as ``GET /browsers`` lists it: the lifecycle, the lease as the fleet CLI reads it, the tabs."""
        return {
            "id": str(browser.name),
            "lifecycle": browser.lifecycle.value.lower(),
            "controller": view.controller.value.lower(),
            "owner_agent_id": view.agent_id,
            "owner_name": view.agent_name,
            "human_pinned": view.human_pinned,
            "human_name": view.human_name,
            "waiting": list(view.waiting),
            "crashed": browser.lifecycle == BrowserLifecycle.CRASHED,
            "pending_url": browser.pending_url,
            "tabs": browser.tab_list() if browser.is_running else [],
        }

    # creating and the one browser

    def _persisted_names(self) -> set[str]:
        saved = self.manifest_store.read()
        names = {str(entry.id) for entry in saved.browsers} if saved is not None else set()
        return names | set(self.scan_profile_names())

    def scan_profile_names(self) -> list[str]:
        """The browsers with a profile on disk, by the directory suffix; a suffix that is not a valid name (an old
        numeric profile) is left to the orphan sweep."""
        if not self.profile_root.exists():
            return []
        names = [
            child.name[len(BROWSER_PROFILE_DIR_PREFIX) :]
            for child in self.profile_root.iterdir()
            if child.is_dir() and child.name.startswith(BROWSER_PROFILE_DIR_PREFIX)
        ]
        return sorted(name for name in names if is_valid_browser_name(name))

    def _register_locked(self, name: BrowserName) -> Browser:
        browser = Browser(name=name)
        self._browsers[str(name)] = browser
        return browser

    def create(self, name: str | None, start_url: str | None) -> Browser:
        """Register a browser (minted ``browser-<N>`` without a name) and launch it in the background; the cap
        counts launched browsers, a chosen name is validated and refused on a collision with a live, saved or
        on-disk browser."""
        with self._lock:
            if name is not None and not is_valid_browser_name(name):
                raise InvalidBrowserNameError(
                    f"'{name}' is not a valid browser name -- use lowercase letters, digits, and single dashes "
                    "(e.g. 'browser-2'), 1-40 characters, no leading/trailing dash."
                )
            live = self.launched_count()
            if live >= self.max_browsers:
                raise FleetFullError(f"{live}/{self.max_browsers} browsers open -- close one first.")
            persisted = self._persisted_names()
            if name is None:
                chosen = BrowserName(first_free_numbered_browser_name(set(self._browsers) | persisted))
            else:
                if name in self._browsers:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' is already in use -- pick another, or close that browser first."
                    )
                if name in persisted:
                    raise DuplicateBrowserNameError(
                        f"the name '{name}' still belongs to a saved browser or its on-disk profile -- pick another."
                    )
                chosen = BrowserName(name)
            browser = self._register_locked(chosen)
            if start_url is not None:
                browser.last_known_tabs = [start_url]
        self.checkpoint()
        self.on_changed()
        self._spawn_launch(browser, [start_url] if start_url is not None else [], 0, is_kept_stopped_on_failure=False)
        return browser

    def ensure_browser(self, start_url: str | None) -> Browser:
        """What a nameless create answers (an agent's ``new``): the one browser, created when there is none,
        started again when it was stopped, with ``start_url`` opened as a new tab when given (queued for the launch
        when Chromium is still coming up)."""
        browser = self.the_browser()
        if browser is None:
            return self.create(None, start_url)
        if browser.is_running or browser.lifecycle == BrowserLifecycle.INIT:
            if start_url is not None and browser.open_tab_or_queue_for_launch(start_url):
                self.on_chromium_processes_expected()
            return browser
        if start_url is not None:
            browser.remember_tab_in_front(start_url)
        self.start_browser(browser.name)
        return browser

    def open_url_for_human(self, url: str) -> Browser:
        """A page the human asked for (``open:url``, or the launch path's start page): a tab in front, except that
        while an agent drives the browser it is held rather than opened over the agent's work (the viewer offers
        Take control, which opens it, or Cancel)."""
        browser = self.the_browser()
        if browser is not None and browser.is_running and self.control.view().controller == Controller.AGENT:
            logger.info("Held {} for browser {} while an agent drives it", url, browser.name)
            self.hold_pending_url(browser, url)
            return browser
        return self.ensure_browser(url)

    def hold_pending_url(self, browser: Browser, url: str) -> None:
        browser.pending_url = url
        self.on_changed()

    def cancel_pending_url(self, name: BrowserName) -> bool:
        browser = self.get(name)
        if browser.pending_url is None:
            return False
        browser.pending_url = None
        self.on_changed()
        return True

    def open_pending_url(self) -> None:
        """Open the held page as a tab in front, once the browser runs and no agent drives (the human took control,
        or the agent let go)."""
        for browser in self.browsers():
            url = browser.pending_url
            if url is None or not browser.is_running or self.control.view().controller == Controller.AGENT:
                continue
            browser.pending_url = None
            self.on_changed()
            try:
                browser.open_tab(url)
                self.on_chromium_processes_expected()
            except BrowserNotDrivableError as e:
                logger.warning("The held page {} could not be opened in {} ({})", url, browser.name, e)

    # launching

    def _spawn_launch(
        self, browser: Browser, restore_tabs: list[str], active_tab: int, is_kept_stopped_on_failure: bool
    ) -> None:
        """Launch in the background, serialized so at most one Chromium starts at a time; a browser registered
        while the stack is down waits for the stack (its launch runs from the restore the stack's start brings)."""
        with self._lock:
            if not self._is_stack_running:
                browser.is_launch_wanted = True
                return
            browser.is_launch_wanted = False
        thread = threading.Thread(
            target=self._launch,
            args=(browser, restore_tabs, active_tab, is_kept_stopped_on_failure),
            name=f"screen-browser-launch-{browser.name}",
            daemon=True,
        )
        self._threads.append(thread)
        thread.start()

    def _launch(
        self, browser: Browser, restore_tabs: list[str], active_tab: int, is_kept_stopped_on_failure: bool
    ) -> None:
        with self._startup_lock:
            with self._lock:
                if self._browsers.get(str(browser.name)) is not browser or not self._is_stack_running:
                    return
            try:
                self._start(browser, restore_tabs, active_tab)
            except (ChromiumStartError, ChromiumNotInstalledError, CdpError, OSError) as e:
                browser.teardown_chromium()
                with self._lock:
                    is_stack_running = self._is_stack_running
                if not is_stack_running:
                    # Not the browser's failure: it launches again with the next stack, as it would have had the
                    # stack ended a moment later.
                    logger.info("Browser {} did not launch because the stack ended ({})", browser.name, e)
                    browser.mark_stopped_with_stack()
                elif is_kept_stopped_on_failure:
                    logger.warning("Browser {} failed to relaunch ({}); leaving it stopped", browser.name, e)
                    browser.mark_stopped_after_failed_launch()
                else:
                    logger.warning("Browser {} failed to launch ({}); removing it", browser.name, e)
                    with self._lock:
                        self._browsers.pop(str(browser.name), None)
                self.checkpoint()
                self.on_changed()
                return
        self.checkpoint()

    def _start(self, browser: Browser, restore_tabs: list[str], active_tab: int) -> None:
        missing = self.install_check()
        if missing is not None:
            raise ChromiumNotInstalledError(missing)
        restorable = [url for url in restore_tabs if is_restorable_url(url)]
        if restorable:
            browser.remember_tabs(restorable, active_tab)
        urls = restorable or [BROWSER_HOME_URL]
        request = ChromiumLaunchRequest(
            executable=self.executable,
            profile_dir=profile_dir_for(self.profile_root, browser.name),
            start_url=urls[0],
            extensions=extension_paths(self.bundled_extensions_dir, self.installed_extensions_dir),
            environment=self.environment(),
            is_sandbox_disabled=self.is_sandbox_disabled,
        )
        chromium = self.launcher.launch(request)
        # The stack may have ended while Chromium came up; its teardown saw nothing attached yet.
        with self._lock:
            is_stack_running = self._is_stack_running
        if not is_stack_running:
            chromium.kill()
            raise ChromiumStartError("the stack ended while Chromium launched")
        self.on_chromium_processes_expected()
        try:
            cdp = self.cdp_connector.connect(chromium.http_endpoint)
        except CdpError:
            chromium.kill()
            raise
        browser.attach(chromium, cdp)
        browser.mint_token(None, None)
        self.proxy_server.register(
            BrowserProxy(
                name=str(browser.name),
                upstream_http=chromium.http_endpoint,
                public_base=self.proxy_server.public_base,
                is_allowed=lambda token: self.token_may_drive(browser, token),
                on_attach=self.on_changed,
                on_activity=lambda target_id: self._on_proxy_activity(browser, target_id),
                page_count=browser.page_count,
            )
        )
        # Chromium came up on the first URL; the remaining ones need targets.
        for url in urls[1:]:
            self.on_chromium_processes_expected()
            try:
                cdp.create_target(url)
            except CdpError as e:
                logger.debug("Reopening {} on {} was ignored ({})", url, browser.name, e)
        try:
            targets = cdp.page_targets()
        except CdpError as e:
            logger.debug("The tab list after the start of {} was ignored ({})", browser.name, e)
            targets = []
        if targets:
            index = active_tab if 0 <= active_tab < len(targets) else 0
            browser.focus_and_foreground(targets[index].target_id)
        browser.lifecycle = BrowserLifecycle.RUNNING
        logger.info("Browser {} started (cdp={})", browser.name, chromium.http_endpoint)
        self.on_changed()
        self.on_browser_running(browser.name)
        browser.open_queued_tabs()
        self.open_pending_url()

    # the gate

    def token_may_drive(self, browser: Browser, token: str) -> bool:
        """Whether a frame bearing ``token`` may be forwarded right now: the token must be the browser's current
        one, the browser running, and the Screen's lease the token owner's; a free Screen is taken on the first
        frame, so the URL ``new`` prints can drive without a separate ``acquire``."""
        owner = browser.token_owner
        if not token or token != browser.token or owner is None or not browser.is_running:
            return False
        view = self.control.view()
        if view.controller == Controller.AGENT and view.agent_id == owner:
            self.on_agent_activity(owner)
            return True
        if view.controller == Controller.NOBODY:
            status = self.control.acquire(
                owner,
                browser.token_owner_name,
                is_reclaim=False,
                is_wait=False,
                max_wait_seconds=None,
                is_enqueue_on_busy=False,
            )
            if status == AcquireStatus.ACQUIRED:
                self.on_agent_activity(owner)
                return True
        return False

    def _on_proxy_activity(self, browser: Browser, target_id: str | None) -> None:
        """The view follows the target the agent named; with none named the view is left alone, since the fleet's
        own record of the active tab is stale as soon as a human browses inside Chromium."""
        if target_id:
            browser.focus_and_foreground(target_id)

    def attach_for(self, name: BrowserName, agent_id: AgentId, agent_name: str | None) -> dict[str, Any]:
        """Issue an agent an attach URL, or say why it cannot have one: the token is bound to the agent here, the
        one place its identity header is visible (a CDP client sends none)."""
        browser = self.get(name)
        view = self.control.view()
        payload = self.control_payload(browser, view)
        if browser.lifecycle == BrowserLifecycle.CRASHED:
            return {"ok": False, "status": "crashed", **payload}
        if browser.lifecycle == BrowserLifecycle.STOPPED:
            return {
                "ok": False,
                "status": "stopped",
                "hint": f"browser {name} is stopped; {stopped_hint(name)}",
                **payload,
            }
        if not browser.is_running:
            return {"ok": False, "status": "starting", **payload}
        if view.controller == Controller.HUMAN:
            return {"ok": False, "status": "busy_human", **payload}
        if view.controller == Controller.AGENT and view.agent_id not in (None, agent_id):
            return {"ok": False, "status": "busy_agent", **payload}
        token = browser.mint_token(agent_id, agent_name)
        return {"ok": True, "attach_url": self.proxy_server.attach_url(str(name), token), **payload}

    @staticmethod
    def control_payload(browser: Browser, view: ControlView) -> dict[str, Any]:
        """The lease snapshot every fleet answer carries, so an agent sees after each call whether it still holds
        the Screen."""
        return {
            "lifecycle": browser.lifecycle.value.lower(),
            "controller": view.controller.value.lower(),
            "owner_agent_id": view.agent_id,
            "owner_name": view.agent_name,
            "human_pinned": view.human_pinned,
        }

    # stopping, starting, closing

    def stop_browser(self, name: BrowserName) -> None:
        """End Chromium while keeping the browser, its profile and its tabs (refreshed first, while Chromium can
        still be asked); refused while it is still launching."""
        browser = self.get(name)
        if browser.lifecycle == BrowserLifecycle.INIT:
            raise BrowserNotDrivableError(f"browser {name} is still launching; stop it once it is up")
        if browser.lifecycle == BrowserLifecycle.STOPPED:
            return
        browser.refresh_tabs()
        browser.mark_stopped_by_user()
        self.proxy_server.unregister(str(name))
        browser.teardown_chromium()
        self.checkpoint()
        self.on_changed()

    def start_browser(self, name: BrowserName) -> None:
        """Relaunch a stopped or crashed browser on its last known tabs from its profile; a no-op for one launching
        or running."""
        browser = self.get(name)
        if browser.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING):
            return
        if browser.lifecycle == BrowserLifecycle.CRASHED:
            self.proxy_server.unregister(str(name))
            browser.teardown_chromium()
        with self._lock:
            live = self.launched_count()
            if live >= self.max_browsers:
                raise FleetFullError(f"{live}/{self.max_browsers} browsers open -- close one first.")
            browser.lifecycle = BrowserLifecycle.INIT
            browser.is_stopped_by_user = False
        self.checkpoint()
        self.on_changed()
        self._spawn_launch(
            browser, list(browser.last_known_tabs), browser.last_known_active_tab, is_kept_stopped_on_failure=True
        )

    def close_and_forget(self, name: BrowserName) -> None:
        """End the browser, drop it from the manifest, and delete its profile (its logins with it). The manifest is
        rewritten before the profile goes, so a crash between them leaves an orphan directory the next restore
        sweeps, never an entry pointing at a deleted profile."""
        with self._lock:
            browser = self._browsers.pop(str(name), None)
        self.proxy_server.unregister(str(name))
        if browser is not None:
            browser.teardown_chromium()
        self.checkpoint()
        self.on_changed()
        shutil.rmtree(profile_dir_for(self.profile_root, name), ignore_errors=True)

    def close_active_tab(self, name: BrowserName) -> None:
        self.get(name).close_active_tab()

    # the stack's life

    def on_stack_running(self) -> None:
        """The stack came up: restore the saved browsers in the background (one Chromium at a time)."""
        with self._lock:
            self._is_stack_running = True
        thread = threading.Thread(target=self.restore, name="screen-browser-restore", daemon=True)
        self._threads.append(thread)
        thread.start()

    def on_stack_stopped(self) -> None:
        """The X server is gone and Chromium with it: every browser is stopped for this stack life, and the ones
        the user had running relaunch when the stack next runs."""
        with self._lock:
            self._is_stack_running = False
            browsers = list(self._browsers.values())
        for browser in browsers:
            self.proxy_server.unregister(str(browser.name))
            browser.teardown_chromium()
            browser.mark_stopped_with_stack()
        self.on_changed()

    def prepare_for_stack_stop(self) -> None:
        """Checkpoint the tabs while Chromium can still be asked (spec section 9.3)."""
        self.checkpoint()

    def _register_saved_locked(self, saved: BrowserManifest | None) -> None:
        """Register every saved browser not yet known, stopped: the manifest's entries with their tabs, or with
        no manifest the profiles on disk (tabs unknown, kept stopped). The caller holds the lock."""
        if saved is not None:
            for entry in sorted(saved.browsers, key=lambda e: e.id):
                if str(entry.id) in self._browsers:
                    continue
                browser = self._register_locked(entry.id)
                browser.lifecycle = BrowserLifecycle.STOPPED
                browser.last_known_tabs = list(entry.tabs)
                browser.last_known_active_tab = entry.active_tab
                browser.is_stopped_by_user = entry.stopped
        else:
            for name in self.scan_profile_names():
                if name not in self._browsers:
                    browser = self._register_locked(BrowserName(name))
                    browser.lifecycle = BrowserLifecycle.STOPPED
                    browser.is_stopped_by_user = True

    def load_saved(self) -> None:
        """Register the saved browsers as stopped at service start, before any stack runs, so a launch that arrives
        while the stack is still down starts the saved browser instead of minting a fresh name beside it -- whose
        first checkpoint would drop the saved entry from the manifest, and the next ``restore`` then sweep the saved
        profile (its logins with it). What relaunches when the stack comes up stays ``restore``'s decision."""
        saved = self.manifest_store.read()
        with self._lock:
            self._register_saved_locked(saved)
        self.on_changed()

    def restore(self) -> None:
        """Bring the saved browsers back: the ones the user had running relaunch one at a time on their tabs, the
        ones the user stopped come back stopped with their tabs, and with no manifest the profiles on disk come
        back stopped (tabs unknown) rather than costing a Chromium nobody asked for. Then the manifest is
        reconciled and profiles backing no browser are deleted."""
        saved = self.manifest_store.read()
        wanted: list[Browser] = []
        with self._lock:
            self._register_saved_locked(saved)
            if saved is not None:
                for entry in sorted(saved.browsers, key=lambda e: e.id):
                    browser = self._browsers[str(entry.id)]
                    if browser.lifecycle == BrowserLifecycle.STOPPED and not browser.is_stopped_by_user:
                        browser.is_launch_wanted = True
            wanted = [browser for _, browser in sorted(self._browsers.items()) if browser.is_launch_wanted]
            for browser in wanted:
                browser.is_launch_wanted = False
            # A workspace saved under a larger cap relaunches the first browser by name and keeps the rest stopped.
            others_launched = sum(
                1
                for browser in self._browsers.values()
                if browser not in wanted and browser.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING)
            )
            room = max(0, self.max_browsers - others_launched)
            to_launch = wanted[:room]
            for browser in wanted[room:]:
                logger.warning("The browser cap leaves no room for {}; leaving it stopped", browser.name)
                browser.lifecycle = BrowserLifecycle.STOPPED
            for browser in to_launch:
                browser.lifecycle = BrowserLifecycle.INIT
        self.on_changed()
        for browser in to_launch:
            self._launch(browser, list(browser.last_known_tabs), browser.last_known_active_tab, True)
        self.checkpoint()
        # Read right before the sweep: a browser created while the launches ran has a profile to keep too.
        with self._lock:
            kept_names = set(self._browsers)
        self.sweep_orphan_profiles(kept_names)

    def sweep_orphan_profiles(self, kept_names: set[str]) -> None:
        """Delete the profiles backing no known browser (closed ones, old numeric ones)."""
        if not self.profile_root.exists():
            return
        for child in self.profile_root.iterdir():
            if not (child.is_dir() and child.name.startswith(BROWSER_PROFILE_DIR_PREFIX)):
                continue
            if child.name[len(BROWSER_PROFILE_DIR_PREFIX) :] not in kept_names:
                shutil.rmtree(child, ignore_errors=True)

    # the clocks

    def tick(self) -> None:
        """One second passed: every ``KEEPALIVE_SECONDS`` poll the running browsers for a crash and re-checkpoint."""
        self._ticks += 1
        if self._ticks % KEEPALIVE_SECONDS != 0:
            return
        for browser in self.browsers():
            if not browser.is_running:
                continue
            if browser.note_poll(browser.is_chromium_alive()):
                self.mark_crashed(browser)
        self.checkpoint()

    def mark_crashed(self, browser: Browser) -> None:
        """Chromium died unexpectedly: the browser reports ``crashed`` to agents and viewers rather than freezing,
        and its profile and last tabs stay for a start."""
        if not browser.mark_crashed():
            return
        logger.warning("Browser {} crashed (Chromium's connection was lost)", browser.name)
        self.proxy_server.unregister(str(browser.name))
        self.checkpoint()
        self.on_changed()

    # the manifest

    def snapshot_manifest(self) -> BrowserManifest:
        entries: list[ManifestEntry] = []
        for browser in self.browsers():
            urls, active = (
                browser.refresh_tabs()
                if browser.is_running
                else (list(browser.last_known_tabs), browser.last_known_active_tab)
            )
            entries.append(
                ManifestEntry(id=browser.name, tabs=tuple(urls), active_tab=active, stopped=browser.is_stopped_by_user)
            )
        return BrowserManifest(browsers=tuple(entries))

    def checkpoint(self) -> None:
        """Write the manifest when it changed (an idle workspace produces no churn); a write that fails is logged,
        since the next checkpoint retries."""
        manifest = self.snapshot_manifest()
        blob = manifest.model_dump_json()
        with self._lock:
            if blob == self._last_manifest_json:
                return
            try:
                self.manifest_store.write(manifest)
            except OSError as e:
                logger.warning("The browser manifest could not be written ({})", e)
                return
            self._last_manifest_json = blob
