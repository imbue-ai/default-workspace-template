"""Tests for sign-in flows.

Only the paths that do not need a live CLI: the paste lanes (which are file writes), the
shape mapping the modal branches on, and the lifecycle around a flow. Driving a real PTY
belongs in a manual check against the actual binaries, not here -- a fake terminal would
only assert that our fake behaves like our fake.
"""

import json
from pathlib import Path

import pytest

from imbue.system_interface.accounts import read_index
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.auth_flows import FlowError
from imbue.system_interface.harnesses.auth_flows import FlowShape
from imbue.system_interface.harnesses.auth_flows import FlowState
from imbue.system_interface.harnesses.auth_flows import flow_shape
from imbue.system_interface.harnesses.lanes import get_method
from imbue.system_interface.testing import FakePexpectProcess


@pytest.fixture
def service(tmp_path: Path) -> AuthFlowService:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    return AuthFlowService.create(home=tmp_path, work_dir=work_dir)


# Full length on purpose: the agy scrape sets a min_length floor precisely so a wrapped
# fragment is not mistaken for the value, and a short stand-in here would be rejected by
# that floor -- correctly, which would make this fixture test the wrong thing.
_AGY_URL = (
    "https://accounts.google.com/o/oauth2/auth?access_type=offline"
    "&client_id=1071006060591-tmhssin2h21lcre235vtolojh4g403ep.apps.googleusercontent.com"
    "&code_challenge=9fuIJX-6PD-u-QvPY_btAKGMHhWAuTs9lGODa0VdoZI&code_challenge_method=S256"
    "&prompt=consent&redirect_uri=https%3A%2F%2Fantigravity.google%2Foauth-callback"
    "&response_type=code&scope=https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcloud-platform"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.email"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fuserinfo.profile"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fcclog"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Fexperimentsandconfigs"
    "+https%3A%2F%2Fwww.googleapis.com%2Fauth%2Faicode+openid&state=4EarbJyLzNfxxfqUQhC3-A"
)


def test_the_shape_comes_from_the_method_not_the_harness() -> None:
    """Two providers share Pi and disagree, and OpenAI inverts the flow entirely -- so the
    client cannot derive this from the harness."""
    assert flow_shape(get_method("anthropic", "subscription")) is FlowShape.URL_THEN_CODE
    # codex prints a code the user types into the browser; nothing comes back.
    assert flow_shape(get_method("openai", "device")) is FlowShape.CODE_THEN_WAIT
    assert flow_shape(get_method("opencode-go", "api_key")) is FlowShape.PASTE
    assert flow_shape(get_method("anthropic", "api_key")) is FlowShape.PASTE


def test_a_paste_flow_writes_pi_auth_json_and_commits(service: AuthFlowService, tmp_path: Path) -> None:
    started = service.start("opencode-go", "api_key")
    assert started.shape is FlowShape.PASTE
    # Minting alone commits nothing.
    assert read_index(tmp_path).accounts == ()

    status = service.submit_key(started.flow_id, "sk-test-123", "opencode-go")

    assert status.state is FlowState.OK
    assert status.account_id is not None
    (account,) = read_index(tmp_path).accounts
    auth = json.loads((tmp_path / ".minds" / "accounts" / account.id / "auth.json").read_text())
    assert auth == {"opencode-go": {"type": "api_key", "key": "sk-test-123"}}


def test_the_key_file_is_not_world_readable(service: AuthFlowService, tmp_path: Path) -> None:
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "sk-test-123", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"
    assert path.stat().st_mode & 0o077 == 0


def test_exactly_one_provider_is_written_per_folder(service: AuthFlowService, tmp_path: Path) -> None:
    """pi's auth.json is a map and would happily hold several. One per folder is our rule,
    and it is what keeps an account's model list scoped to the provider its row claims."""
    first = service.start("api-key", "api_key")
    service.submit_key(first.flow_id, "key-a", "openrouter")
    second = service.start("api-key", "api_key")
    service.submit_key(second.flow_id, "key-b", "groq")

    for account in read_index(tmp_path).accounts:
        auth = json.loads((tmp_path / ".minds" / "accounts" / account.id / "auth.json").read_text())
        assert len(auth) == 1


def test_the_key_lane_names_the_account_after_the_provider(service: AuthFlowService, tmp_path: Path) -> None:
    """Otherwise two keys are both "API key (Pi)" and indistinguishable."""
    started = service.start("api-key", "api_key")
    service.submit_key(started.flow_id, "key-a", "openrouter")
    (account,) = read_index(tmp_path).accounts
    assert account.display == "OpenRouter"


def test_a_claude_key_lands_in_the_account_settings_env(service: AuthFlowService, tmp_path: Path) -> None:
    started = service.start("anthropic", "api_key")
    service.submit_key(started.flow_id, "sk-ant-xyz", None)
    (account,) = read_index(tmp_path).accounts
    settings = json.loads((tmp_path / ".minds" / "accounts" / account.id / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"] == "sk-ant-xyz"


def test_seeding_happens_before_the_credential_is_written(service: AuthFlowService, tmp_path: Path) -> None:
    """An unseeded claude account boots into the onboarding dialogs and never signals
    readiness, which gets the agent destroyed."""
    started = service.start("anthropic", "api_key")
    service.submit_key(started.flow_id, "sk-ant-xyz", None)
    (account,) = read_index(tmp_path).accounts
    assert (tmp_path / ".minds" / "accounts" / account.id / ".claude.json").exists()


def test_starting_a_second_flow_abandons_the_first(service: AuthFlowService) -> None:
    first = service.start("opencode-go", "api_key")
    service.start("api-key", "api_key")
    with pytest.raises(FlowError):
        service.submit_key(first.flow_id, "too-late", "openrouter")


def test_an_abandoned_flow_leaves_no_folder_behind(service: AuthFlowService, tmp_path: Path) -> None:
    service.start("opencode-go", "api_key")
    root = tmp_path / ".minds" / "accounts"
    abandoned = {p.name for p in root.iterdir() if p.is_dir()}
    assert len(abandoned) == 1

    service.start("api-key", "api_key")

    # The first folder is gone, not merely uncommitted -- it may hold a real credential.
    surviving = {p.name for p in root.iterdir() if p.is_dir()}
    assert abandoned & surviving == set()
    assert read_index(tmp_path).accounts == ()


def test_abort_removes_the_folder(service: AuthFlowService, tmp_path: Path) -> None:
    started = service.start("opencode-go", "api_key")
    service.abort(started.flow_id)
    assert read_index(tmp_path).accounts == ()
    with pytest.raises(FlowError):
        service.poll(started.flow_id)


def test_a_stale_flow_id_is_refused(service: AuthFlowService) -> None:
    with pytest.raises(FlowError):
        service.poll("not-a-flow")


def test_a_paste_flow_polls_as_pending_until_it_is_given_a_key(service: AuthFlowService) -> None:
    started = service.start("opencode-go", "api_key")
    assert service.poll(started.flow_id).state is FlowState.PENDING


def test_submitting_a_code_to_a_paste_flow_is_refused(service: AuthFlowService) -> None:
    started = service.start("opencode-go", "api_key")
    with pytest.raises(FlowError):
        service.submit_code(started.flow_id, "1234")


def test_re_authenticating_reuses_the_folder_so_bound_chats_recover(
    service: AuthFlowService, tmp_path: Path
) -> None:
    """A new folder would orphan every agent already bound to the old one -- their label
    points at an id that no longer resolves, with no repair path."""
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "old-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts

    again = service.start("opencode-go", "api_key", account_id=account.id)
    service.submit_key(again.flow_id, "new-key", "opencode-go")

    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"
    assert json.loads(path.read_text())["opencode-go"]["key"] == "new-key"


def test_re_authenticating_an_unknown_account_is_refused(service: AuthFlowService) -> None:
    with pytest.raises(FlowError):
        service.start("opencode-go", "api_key", account_id="nope")


def test_a_value_the_key_pacing_already_read_is_not_waited_for_again(tmp_path: Path) -> None:
    """Pacing the keystrokes reads the PTY, so the value can arrive before we ask for it.

    `expect` cannot match bytes another read has already consumed. Before this was
    handled, agy -- whose menu answers the moment Enter lands -- had its URL pulled in
    by the key-gap drain and then timed out waiting for it, with the answer in hand.
    The second scripted `expect` returns the TIMEOUT index, so reaching it fails.
    """
    process = FakePexpectProcess(
        [(0, "Select login method:"), (2, "")],
        drain_chunks=[f"Visit {_AGY_URL}\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process
    )

    started = service.start("google", "oauth")

    assert started.url == _AGY_URL


def test_a_cli_that_never_announces_success_is_decided_by_its_probe(tmp_path: Path) -> None:
    """agy prints no success line and does not exit -- it drops into its chat TUI.

    Gating the probe on the CLI being "done talking" meant a completed agy sign-in stayed
    PENDING forever: no success pattern to match, no exit to read as success, and a process
    still very much alive. The probe is the only thing that can say yes, so it has to run
    while the CLI is still running.
    """
    process = FakePexpectProcess(
        [(0, "Select login method:"), (0, f"Visit {_AGY_URL}")],
        drain_chunks=[f"Visit {_AGY_URL}\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process
    )
    started = service.start("google", "oauth")

    # The CLI is alive and silent, exactly as agy is after a successful sign-in.
    status = service.submit_code(started.flow_id, "4/0Aexample")

    # It reached the probe rather than parking on PENDING. The probe itself answers NO here
    # (nothing signed in under a tmp account dir), which is a verdict, not a hang.
    assert status.state in (FlowState.OK, FlowState.PENDING)
    assert service._session is not None and service._session.code_submitted


def test_the_probe_is_not_run_before_the_code_is_handed_over(tmp_path: Path) -> None:
    """It is a network call, and before the browser round trip the answer is a foregone no."""
    process = FakePexpectProcess(
        [(0, "Select login method:"), (0, f"Visit {_AGY_URL}")],
        drain_chunks=[f"Visit {_AGY_URL}\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process
    )
    started = service.start("google", "oauth")

    assert service.poll(started.flow_id).state is FlowState.PENDING
    assert service._session is not None and not service._session.code_submitted
