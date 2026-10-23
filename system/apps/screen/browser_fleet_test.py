"""The browser on the Screen over an in-memory Chromium: creation and the one browser, the held page, stop and
start with the tabs kept, the manifest and the restore, the gate over the Screen's lease, crash detection, and
the stack's life around it (spec section 16)."""

from pathlib import Path

import pytest

from mock_chromium_test import FakeCdpClient
from mock_chromium_test import FakeCdpConnector
from mock_chromium_test import FakeChromiumLauncher
from mock_messenger_test import RecordingMessenger
from screen.browser_fleet import Browser
from screen.browser_fleet import BrowserFleet
from screen.browser_manifest import BrowserManifest
from screen.browser_manifest import ManifestEntry
from screen.browser_manifest import ManifestStore
from screen.cdp_client import CdpTarget
from screen.cdp_proxy import ProxyServer
from screen.control import ScreenControl
from screen.data_types import AcquireStatus
from screen.data_types import BrowserLifecycle
from screen.data_types import Controller
from screen.errors import BrowserNotDrivableError
from screen.errors import DuplicateBrowserNameError
from screen.errors import FleetFullError
from screen.errors import InvalidBrowserNameError
from screen.errors import UnknownBrowserError
from screen.primitives import AgentId
from screen.primitives import BROWSER_PROFILE_DIR_PREFIX
from screen.primitives import BrowserName
from screen.primitives import NekoSessionId
from testing import ManualClock
from testing import wait_until

_AGENT = AgentId("chat-a")
_HUMAN = NekoSessionId("human-1")


class _Harness:
    """A fleet over fakes, with the Screen's lease and every event it raised."""

    def __init__(self, tmp_path: Path, max_browsers: int = 1) -> None:
        self.clock = ManualClock()
        self.messenger = RecordingMessenger()
        self.control = ScreenControl(
            messenger=self.messenger,
            on_changed=lambda view: None,
            on_handoff_request=lambda name, reason: None,
            record_path=None,
            clock=self.clock,
        )
        self.launcher = FakeChromiumLauncher()
        self.connector = FakeCdpConnector()
        self.proxy_server = ProxyServer(requested_port=0)
        self.store = ManifestStore(path=tmp_path / "apps" / "browser" / "instances.json")
        self.profile_root = tmp_path / "profiles"
        self.changes = 0
        self.kicks = 0
        self.running: list[str] = []
        self.touched: list[str] = []
        self.install_reason: str | None = None

        def changed() -> None:
            self.changes += 1

        def kicked() -> None:
            self.kicks += 1

        self.fleet = BrowserFleet(
            launcher=self.launcher,
            cdp_connector=self.connector,
            proxy_server=self.proxy_server,
            manifest_store=self.store,
            profile_root=self.profile_root,
            executable=tmp_path / "tilion",
            install_check=lambda: self.install_reason,
            environment=lambda: {"DISPLAY": ":4242"},
            is_sandbox_disabled=True,
            control=self.control,
            on_changed=changed,
            on_agent_activity=lambda agent_id: self.touched.append(str(agent_id)),
            on_chromium_processes_expected=kicked,
            on_browser_running=lambda name: self.running.append(str(name)),
            max_browsers=max_browsers,
        )

    def stack_up(self) -> None:
        self.fleet.on_stack_running()
        assert wait_until(lambda: self.store.path.exists() or not self.fleet.browsers())

    def running_browser(self, start_url: str | None = None) -> Browser:
        browser = self.fleet.ensure_browser(start_url)
        assert wait_until(lambda: browser.is_running), browser.lifecycle
        return browser

    def manifest(self) -> BrowserManifest:
        loaded = self.store.read()
        assert loaded is not None
        return loaded

    def cdp(self) -> FakeCdpClient:
        return self.connector.clients[-1]


def test_create_registers_the_browser_at_once_and_launches_it_on_the_start_page(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.fleet.create(None, "https://example.com")
    assert browser.name == "browser-1"
    assert browser.lifecycle in (BrowserLifecycle.INIT, BrowserLifecycle.RUNNING)
    assert wait_until(lambda: browser.is_running)
    request = harness.launcher.requests[0]
    assert request.start_url == "https://example.com"
    assert request.profile_dir == harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}browser-1"
    assert request.environment == {"DISPLAY": ":4242"}
    assert request.is_sandbox_disabled is True
    assert harness.connector.connected == [harness.launcher.handles[0].http_endpoint]
    assert harness.proxy_server.lookup("browser-1") is not None
    # The lifecycle flips before the service is told, so the window raise is waited for rather than read at once.
    assert wait_until(lambda: harness.running == ["browser-1"])
    assert harness.kicks >= 1
    assert browser.token and browser.token_owner is None
    assert harness.fleet.chromium_view().model_dump() == {"state": "running", "name": "browser-1", "pending_url": None}


def test_the_one_browser_is_reused_and_a_start_page_opens_as_a_tab_in_front(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    again = harness.fleet.ensure_browser("https://two.example")
    assert again is browser
    cdp = harness.cdp()
    assert cdp.created == ["https://two.example"]
    assert cdp.activated[-1] == "new-1"
    assert len(harness.launcher.requests) == 1


def test_names_are_validated_and_the_cap_and_duplicates_are_refused(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    with pytest.raises(InvalidBrowserNameError):
        harness.fleet.create("Bad Name", None)
    harness.running_browser()
    with pytest.raises(FleetFullError):
        harness.fleet.create("second", None)
    harness.fleet.stop_browser(BrowserName("browser-1"))
    with pytest.raises(DuplicateBrowserNameError):
        harness.fleet.create("browser-1", None)
    with pytest.raises(UnknownBrowserError):
        harness.fleet.get(BrowserName("nobody"))


def test_a_browser_asked_for_while_the_stack_is_down_launches_when_the_stack_comes_up(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    browser = harness.fleet.ensure_browser("https://example.com")
    assert browser.lifecycle == BrowserLifecycle.INIT
    assert browser.is_launch_wanted is True
    assert harness.launcher.requests == []
    # A second ask meanwhile queues its page for the launch; it is not a page held from the human, so the viewer
    # offers nothing to take control for.
    harness.fleet.ensure_browser("https://later.example")
    assert browser.launch_tabs == ["https://later.example"]
    assert browser.pending_url is None
    harness.fleet.on_stack_running()
    assert wait_until(lambda: browser.is_running)
    assert harness.launcher.requests[0].start_url == "https://example.com"
    assert wait_until(lambda: harness.cdp().created == ["https://later.example"])
    assert browser.launch_tabs == []


def test_a_page_is_opened_at_once_on_a_running_browser_and_queued_on_one_still_launching(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    browser = harness.fleet.ensure_browser(None)
    assert browser.lifecycle == BrowserLifecycle.INIT
    assert browser.open_tab_or_queue_for_launch("https://queued.example") is False
    assert browser.launch_tabs == ["https://queued.example"]
    harness.stack_up()
    assert wait_until(lambda: browser.is_running)
    assert wait_until(lambda: harness.cdp().created == ["https://queued.example"])
    # Once Chromium is up the page goes straight to it, and the fleet is told to expect its processes.
    kicks = harness.kicks
    assert browser.open_tab_or_queue_for_launch("https://now.example") is True
    assert browser.launch_tabs == []
    assert harness.cdp().created == ["https://queued.example", "https://now.example"]
    harness.fleet.ensure_browser("https://again.example")
    assert harness.cdp().created == ["https://queued.example", "https://now.example", "https://again.example"]
    assert harness.kicks == kicks + 1


def test_a_page_asked_for_while_an_agent_drives_is_held_and_opens_when_the_human_takes_control(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    assert (
        harness.control.acquire(
            _AGENT, "Alice", is_reclaim=False, is_wait=False, max_wait_seconds=None, is_enqueue_on_busy=False
        )
        == AcquireStatus.ACQUIRED
    )
    held = harness.fleet.open_url_for_human("http://localhost:3000/")
    assert held is browser
    assert browser.pending_url == "http://localhost:3000/"
    assert harness.cdp().created == []
    assert harness.fleet.chromium_view().pending_url == "http://localhost:3000/"
    # Still the agent's: nothing opens yet.
    harness.fleet.open_pending_url()
    assert harness.cdp().created == []
    harness.control.take_control(_HUMAN, "Josh")
    harness.fleet.open_pending_url()
    assert harness.cdp().created == ["http://localhost:3000/"]
    assert browser.pending_url is None


def test_the_humans_page_is_held_while_an_agent_drives_but_the_agents_own_opens_at_once(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    harness.control.acquire(
        _AGENT, "Alice", is_reclaim=False, is_wait=False, max_wait_seconds=None, is_enqueue_on_busy=False
    )
    # The launch path's start page is the human's ask.
    held = harness.fleet.open_url_for_human("https://example.org/")
    assert held is browser
    assert browser.pending_url == "https://example.org/"
    assert harness.cdp().created == []
    # A nameless create's page (an agent's ``new --url``) is the agent's own and is not held for the human.
    assert harness.fleet.ensure_browser("https://agent.example/") is browser
    assert harness.cdp().created == ["https://agent.example/"]
    assert browser.pending_url == "https://example.org/"
    harness.control.release(_AGENT)
    harness.fleet.open_pending_url()
    assert harness.cdp().created == ["https://agent.example/", "https://example.org/"]
    assert browser.pending_url is None
    # With nobody driving, the human's page opens at once.
    harness.fleet.open_url_for_human("https://example.net/")
    assert harness.cdp().created == ["https://agent.example/", "https://example.org/", "https://example.net/"]


def test_a_cancelled_page_never_opens_and_leaves_the_agent_driving(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    harness.control.acquire(
        _AGENT, "Alice", is_reclaim=False, is_wait=False, max_wait_seconds=None, is_enqueue_on_busy=False
    )
    harness.fleet.open_url_for_human("http://localhost:3000/")
    assert harness.fleet.cancel_pending_url(browser.name) is True
    assert harness.fleet.cancel_pending_url(browser.name) is False
    harness.control.release(_AGENT)
    harness.fleet.open_pending_url()
    assert harness.cdp().created == []


def test_a_page_for_a_free_browser_opens_at_once(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.running_browser()
    harness.fleet.open_url_for_human("http://localhost:3000/app")
    assert harness.cdp().created == ["http://localhost:3000/app"]


def test_stop_keeps_the_browser_and_its_tabs_and_start_relaunches_them(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(
        FakeCdpClient(
            targets=[
                CdpTarget(target_id="t1", url="https://a.example"),
                CdpTarget(target_id="t2", url="about:blank"),
                CdpTarget(target_id="t3", url="https://b.example"),
            ],
            shown_target_id="t3",
        )
    )
    browser = harness.running_browser()
    browser.focus_and_foreground("t3")
    harness.fleet.stop_browser(browser.name)
    assert browser.lifecycle == BrowserLifecycle.STOPPED
    assert browser.last_known_tabs == ["https://a.example", "https://b.example"]
    assert browser.last_known_active_tab == 1
    assert harness.launcher.handles[0].kills == 1
    assert harness.connector.clients[0].is_closed is True
    assert harness.proxy_server.lookup("browser-1") is None
    entry = harness.manifest().browsers[0]
    assert entry.stopped is True and entry.tabs == ("https://a.example", "https://b.example") and entry.active_tab == 1
    assert harness.fleet.launched_count() == 0
    with pytest.raises(BrowserNotDrivableError):
        browser.open_tab("https://x")

    harness.fleet.start_browser(browser.name)
    assert wait_until(lambda: browser.is_running)
    assert harness.launcher.requests[1].start_url == "https://a.example"
    assert harness.cdp().created == ["https://b.example"]
    assert harness.manifest().browsers[0].stopped is False


def test_a_relaunch_that_fails_leaves_the_browser_stopped_with_its_tabs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(FakeCdpClient(targets=[CdpTarget(target_id="t1", url="https://a.example")]))
    browser = harness.running_browser("https://a.example")
    harness.fleet.stop_browser(browser.name)
    harness.launcher.failure = "no display"
    harness.fleet.start_browser(browser.name)
    assert wait_until(lambda: browser.lifecycle == BrowserLifecycle.STOPPED and len(harness.launcher.requests) == 2)
    assert browser.last_known_tabs == ["https://a.example"]
    assert harness.fleet.browsers() == [browser]


def test_a_first_launch_that_fails_removes_the_browser(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.launcher.failure = "no display"
    harness.fleet.create(None, None)
    assert wait_until(lambda: harness.fleet.browsers() == [])
    assert harness.fleet.chromium_view().state == "absent"


def test_chromium_not_installed_refuses_the_launch_and_says_so(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.install_reason = "Chromium is still installing"
    harness.stack_up()
    assert harness.fleet.install_reason() == "Chromium is still installing"
    harness.fleet.create(None, None)
    assert wait_until(lambda: harness.fleet.browsers() == [])
    assert harness.launcher.requests == []


def test_close_and_forget_ends_the_browser_drops_it_from_the_manifest_and_deletes_its_profile(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    profile = harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}browser-1"
    profile.mkdir(parents=True)
    (profile / "Cookies").write_text("secret")
    harness.fleet.close_and_forget(browser.name)
    assert harness.fleet.browsers() == []
    assert harness.manifest().browsers == ()
    assert not profile.exists()
    assert harness.launcher.handles[0].kills == 1
    # The name is free again.
    assert harness.fleet.create(None, None).name == "browser-1"


def test_the_manifest_is_checkpointed_with_the_live_tabs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(
        FakeCdpClient(
            targets=[
                CdpTarget(target_id="t1", url="https://a.example"),
                CdpTarget(target_id="t2", url="https://b.example"),
            ]
        )
    )
    browser = harness.running_browser()
    browser.focus_and_foreground("t2")
    harness.fleet.checkpoint()
    entry = harness.manifest().browsers[0]
    assert (
        entry.id == "browser-1" and entry.tabs == ("https://a.example", "https://b.example") and entry.active_tab == 1
    )
    assert entry.stopped is False


def test_a_tab_the_human_switched_to_inside_chromium_is_the_active_one_listed_and_saved(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(
        FakeCdpClient(
            targets=[
                CdpTarget(target_id="t1", url="https://a.example"),
                CdpTarget(target_id="t2", url="https://b.example"),
            ]
        )
    )
    browser = harness.running_browser()
    browser.focus_and_foreground("t1")
    # The human clicks the second tab in Chromium itself: no CDP command of the fleet's records it.
    harness.cdp().shown_target_id = "t2"
    assert [tab["url"] for tab in browser.tab_list() if tab["active"]] == ["https://b.example"]
    harness.fleet.checkpoint()
    assert harness.manifest().browsers[0].active_tab == 1


def test_restore_relaunches_the_browser_the_user_had_running_and_keeps_a_stopped_one_stopped(tmp_path: Path) -> None:
    harness = _Harness(tmp_path, max_browsers=2)
    harness.store.write(
        BrowserManifest(
            browsers=(
                ManifestEntry(
                    id=BrowserName("browser-1"), tabs=("https://a.example", "https://b.example"), active_tab=1
                ),
                ManifestEntry(id=BrowserName("browser-2"), tabs=("https://c.example",), stopped=True),
            )
        )
    )
    harness.fleet.on_stack_running()
    assert wait_until(lambda: len(harness.fleet.browsers()) == 2)
    one = harness.fleet.get(BrowserName("browser-1"))
    two = harness.fleet.get(BrowserName("browser-2"))
    assert wait_until(lambda: one.is_running)
    assert harness.launcher.requests[0].start_url == "https://a.example"
    assert wait_until(lambda: harness.cdp().created == ["https://b.example"])
    assert two.lifecycle == BrowserLifecycle.STOPPED
    assert two.last_known_tabs == ["https://c.example"]
    assert len(harness.launcher.requests) == 1


def test_restore_beyond_the_cap_keeps_the_extra_browser_stopped(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.store.write(
        BrowserManifest(
            browsers=(
                ManifestEntry(id=BrowserName("browser-1"), tabs=("https://a.example",)),
                ManifestEntry(id=BrowserName("browser-2"), tabs=("https://c.example",)),
            )
        )
    )
    harness.fleet.on_stack_running()
    assert wait_until(lambda: len(harness.fleet.browsers()) == 2)
    assert wait_until(lambda: harness.fleet.get(BrowserName("browser-1")).is_running)
    assert harness.fleet.get(BrowserName("browser-2")).lifecycle == BrowserLifecycle.STOPPED
    assert len(harness.launcher.requests) == 1


def test_a_lost_manifest_brings_the_profiles_on_disk_back_stopped_and_sweeps_orphans(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    (harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}browser-1").mkdir(parents=True)
    (harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}0").mkdir()
    harness.fleet.on_stack_running()
    assert wait_until(lambda: harness.store.path.exists())
    browser = harness.fleet.get(BrowserName("browser-1"))
    assert browser.lifecycle == BrowserLifecycle.STOPPED
    assert harness.launcher.requests == []
    assert wait_until(lambda: not (harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}0").exists())
    assert (harness.profile_root / f"{BROWSER_PROFILE_DIR_PREFIX}browser-1").exists()


def test_the_stack_ending_stops_the_browser_for_its_life_and_the_next_start_brings_it_back(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(FakeCdpClient(targets=[CdpTarget(target_id="t1", url="https://a.example")]))
    browser = harness.running_browser("https://a.example")
    harness.fleet.prepare_for_stack_stop()
    harness.fleet.on_stack_stopped()
    assert browser.lifecycle == BrowserLifecycle.STOPPED
    assert browser.is_stopped_by_user is False
    assert harness.launcher.handles[0].kills == 1
    assert harness.manifest().browsers[0].stopped is False
    harness.fleet.on_stack_running()
    assert wait_until(lambda: browser.is_running and len(harness.launcher.requests) == 2)
    assert harness.launcher.requests[1].start_url == "https://a.example"


def test_a_launch_the_stack_ends_under_is_ended_and_the_browser_launches_with_the_next_stack(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.launcher.release.clear()
    browser = harness.fleet.create(None, "https://a.example")
    assert harness.launcher.entered.wait(5.0)
    # A page asked for during the launch is queued for it, and goes with the launch the stack cuts short.
    harness.fleet.ensure_browser("https://later.example")
    assert browser.launch_tabs == ["https://later.example"]
    harness.fleet.on_stack_stopped()
    harness.launcher.release.set()
    # The Chromium that came up on a dead display is ended, never attached, and the browser is kept for the next
    # stack rather than removed as a failed first launch.
    assert wait_until(lambda: len(harness.launcher.handles) == 1 and harness.launcher.handles[0].kills == 1)
    assert harness.connector.connected == []
    assert wait_until(lambda: browser.lifecycle == BrowserLifecycle.STOPPED and browser.is_launch_wanted)
    assert browser.launch_tabs == []
    assert harness.fleet.browsers() == [browser]
    assert harness.proxy_server.lookup("browser-1") is None
    harness.fleet.on_stack_running()
    assert wait_until(lambda: browser.is_running and len(harness.launcher.requests) == 2)
    assert harness.launcher.requests[1].start_url == "https://a.example"
    assert harness.cdp().created == []


def test_the_gate_lets_the_token_owner_drive_takes_a_free_screen_on_the_first_frame_and_refuses_a_human(
    tmp_path: Path,
) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    # No token issued yet: nothing drives.
    assert harness.fleet.token_may_drive(browser, browser.token) is False
    answer = harness.fleet.attach_for(browser.name, _AGENT, "Alice")
    assert answer["ok"] is True
    token = answer["attach_url"].rsplit("/", 1)[1]
    assert token == browser.token
    assert harness.fleet.token_may_drive(browser, "someone-elses") is False
    # The first frame takes the free Screen for the token's agent.
    assert harness.fleet.token_may_drive(browser, token) is True
    assert harness.control.view().controller == Controller.AGENT and harness.control.view().agent_id == _AGENT
    assert harness.touched == ["chat-a"]
    # The human takes control: the very next frame is refused, the socket untouched.
    harness.control.take_control(_HUMAN, "Josh")
    assert harness.fleet.token_may_drive(browser, token) is False
    refused = harness.fleet.attach_for(browser.name, _AGENT, "Alice")
    assert refused["status"] == "busy_human" and refused["ok"] is False
    # The human hands back: the agent, first in the resume queue, drives again on the same token.
    harness.control.return_to_agents(_HUMAN, is_owner=False)
    assert harness.control.view().agent_id == _AGENT
    assert harness.fleet.token_may_drive(browser, token) is True


def test_another_agent_cannot_get_a_token_for_a_held_browser_and_taking_it_rotates_the_token(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    first = harness.fleet.attach_for(browser.name, _AGENT, "Alice")
    assert harness.fleet.token_may_drive(browser, browser.token)
    other = harness.fleet.attach_for(browser.name, AgentId("chat-b"), "Bob")
    assert other["status"] == "busy_agent"
    harness.control.release(_AGENT)
    taken = harness.fleet.attach_for(browser.name, AgentId("chat-b"), "Bob")
    assert taken["ok"] is True
    assert taken["attach_url"] != first["attach_url"]
    assert harness.fleet.token_may_drive(browser, first["attach_url"].rsplit("/", 1)[1]) is False


def test_attach_reports_a_starting_stopped_or_crashed_browser(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.launcher.release.clear()
    harness.stack_up()
    browser = harness.fleet.create(None, None)
    assert harness.fleet.attach_for(browser.name, _AGENT, None)["status"] == "starting"
    harness.launcher.release.set()
    assert wait_until(lambda: browser.is_running)
    harness.fleet.stop_browser(browser.name)
    stopped = harness.fleet.attach_for(browser.name, _AGENT, None)
    assert stopped["status"] == "stopped" and "new" in stopped["hint"]


def test_a_crash_is_detected_with_nobody_attached_within_two_polls(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    harness.launcher.handles[0].alive = False
    for _ in range(10):
        harness.fleet.tick()
    assert browser.is_running
    for _ in range(10):
        harness.fleet.tick()
    assert browser.lifecycle == BrowserLifecycle.CRASHED
    assert harness.proxy_server.lookup("browser-1") is None
    assert harness.fleet.attach_for(browser.name, _AGENT, None)["status"] == "crashed"
    assert harness.manifest().browsers[0].id == "browser-1"
    # A start after a crash ends the leftovers and relaunches on the profile.
    harness.fleet.start_browser(browser.name)
    assert wait_until(lambda: browser.is_running and len(harness.launcher.requests) == 2)


def test_a_transient_cdp_drop_the_client_recovers_from_is_not_a_crash(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    browser = harness.running_browser()
    harness.cdp().alive = False
    harness.cdp().ping_answer = True
    for _ in range(20):
        harness.fleet.tick()
    assert browser.is_running


def test_close_active_tab_closes_the_tab_in_front_and_replaces_the_last_one(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(
        FakeCdpClient(
            targets=[CdpTarget(target_id="t1", url="https://one"), CdpTarget(target_id="t2", url="https://two")],
            shown_target_id="t2",
        )
    )
    browser = harness.running_browser()
    cdp = harness.cdp()
    cdp.shown_target_id = "t2"
    harness.fleet.close_active_tab(browser.name)
    assert cdp.closed == ["t2"]
    assert cdp.activated[-1] == "t1"
    harness.fleet.close_active_tab(browser.name)
    assert cdp.created == ["about:blank"]
    assert cdp.closed == ["t2", "t1"]
    assert [target.target_id for target in cdp.targets] == ["new-1"]
    harness.fleet.stop_browser(browser.name)
    with pytest.raises(BrowserNotDrivableError):
        harness.fleet.close_active_tab(browser.name)


def test_describe_carries_the_lease_the_lifecycle_and_the_tabs(tmp_path: Path) -> None:
    harness = _Harness(tmp_path)
    harness.stack_up()
    harness.connector.clients.append(
        FakeCdpClient(targets=[CdpTarget(target_id="t1", url="https://one", title="One")])
    )
    browser = harness.running_browser()
    browser.focus_and_foreground("t1")
    harness.control.take_control(_HUMAN, "Josh")
    described = harness.fleet.describe(browser, harness.control.view())
    assert described["id"] == "browser-1"
    assert described["lifecycle"] == "running"
    assert (
        described["controller"] == "human" and described["human_pinned"] is True and described["human_name"] == "Josh"
    )
    assert described["crashed"] is False
    assert described["tabs"] == [{"index": 0, "title": "One", "url": "https://one", "active": True}]
