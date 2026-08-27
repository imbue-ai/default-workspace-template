"""Tests for sign-in flows.

Only the paths that do not need a live CLI: the paste lanes (which are file writes), the
shape mapping the modal branches on, and the lifecycle around a flow. Driving a real PTY
belongs in a manual check against the actual binaries, not here -- a fake terminal would
only assert that our fake behaves like our fake.
"""

import json
from pathlib import Path

import pytest

from imbue.system_interface.accounts import AccountError
from imbue.system_interface.accounts import read_index
from imbue.system_interface.harnesses.auth_flows import AuthFlowService
from imbue.system_interface.harnesses.auth_flows import FlowError
from imbue.system_interface.harnesses.auth_flows import FlowShape
from imbue.system_interface.harnesses.auth_flows import FlowState
from imbue.system_interface.harnesses.auth_flows import flow_shape
from imbue.system_interface.harnesses.lanes import get_method
from imbue.system_interface.harnesses.signed_in import SignedIn
from imbue.system_interface.testing import FakePexpectProcess


@pytest.fixture
def service(tmp_path: Path) -> AuthFlowService:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    return AuthFlowService.create(home=tmp_path, work_dir=work_dir, probe=lambda *_a: SignedIn.YES)


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
    """The index answers this now, not `is_dir()` -- so the error names the account."""
    with pytest.raises(AccountError):
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
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
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
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("google", "oauth")

    # The CLI is alive and silent, exactly as agy is after a successful sign-in.
    status = service.submit_code(started.flow_id, "4/0Aexample")

    # It reached the probe rather than parking on PENDING. Accepting PENDING here would have
    # accepted the very bug this test is named after: the fixture's probe says YES, so
    # reaching it means OK and not reaching it means PENDING -- both used to pass.
    assert status.state is FlowState.OK
    assert read_index(tmp_path).accounts != (), "reaching the probe is what commits the account"


def test_the_probe_is_not_run_before_the_code_is_handed_over(tmp_path: Path) -> None:
    """It is a network call, and before the browser round trip the answer is a foregone no."""
    process = FakePexpectProcess(
        [(0, "Select login method:"), (0, f"Visit {_AGY_URL}")],
        drain_chunks=[f"Visit {_AGY_URL}\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("google", "oauth")

    assert service.poll(started.flow_id).state is FlowState.PENDING
    assert service._session is not None and not service._session.code_submitted


def test_a_key_the_harness_will_not_accept_fails_at_the_field(tmp_path: Path) -> None:
    """Writing the file is not the harness accepting it.

    A provider id we got wrong, or a schema that drifted, produces a file pi reads as "No
    usable API key is configured" -- which used to commit anyway and surface later as a chat
    that silently could not take a turn. It now fails while the user is still looking at the
    field they typed into.
    """
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.NO
    )
    started = service.start("api-key", "api_key")

    status = service.submit_key(started.flow_id, "sk-wrong", "groq")

    assert status.state is FlowState.FAILED
    assert read_index(tmp_path).accounts == ()


def test_a_probe_that_cannot_run_does_not_throw_the_key_away(tmp_path: Path) -> None:
    """UNKNOWN is "the check failed", not "the key is bad" -- and the user just pasted it."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.UNKNOWN
    )
    started = service.start("api-key", "api_key")

    status = service.submit_key(started.flow_id, "sk-probably-fine", "groq")

    assert status.state is FlowState.OK
    assert len(read_index(tmp_path).accounts) == 1


def test_an_abandoned_re_auth_leaves_the_live_account_alone(
    service: AuthFlowService, tmp_path: Path
) -> None:
    """Aborting a re-auth used to delete the account it was re-authenticating.

    Every failure path discarded the folder, and a re-auth adopts a COMMITTED one -- so
    pressing Back, closing the modal or letting the deadline pass took the credential with
    it and orphaned every chat bound to that id.
    """
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "live-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"

    again = service.start("opencode-go", "api_key", account_id=account.id)
    service.abort(again.flow_id)

    assert path.exists(), "aborting a re-auth deleted the account it was re-authenticating"
    assert json.loads(path.read_text())["opencode-go"]["key"] == "live-key"
    assert read_index(tmp_path).accounts == (account,)


def test_a_rejected_re_auth_key_puts_the_working_one_back(tmp_path: Path) -> None:
    """The probe needs the file in place to answer, so the write comes first -- but the
    folder is a live account, and a rejected key left there breaks every bound agent
    silently, at its next turn, with the row still saying the account is fine."""
    verdicts = [SignedIn.YES, SignedIn.NO]
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: verdicts.pop(0)
    )
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "good-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"

    again = service.start("opencode-go", "api_key", account_id=account.id)
    status = service.submit_key(again.flow_id, "bad-key", "opencode-go")

    assert status.state is FlowState.FAILED
    assert json.loads(path.read_text())["opencode-go"]["key"] == "good-key"
    assert read_index(tmp_path).accounts == (account,)


def test_a_rejected_first_key_leaves_no_folder_behind(tmp_path: Path) -> None:
    """The other half of the same rule: a folder this flow minted IS ours to remove."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.NO
    )
    started = service.start("opencode-go", "api_key")

    assert service.submit_key(started.flow_id, "bad", "opencode-go").state is FlowState.FAILED
    assert list((tmp_path / ".minds" / "accounts").glob("*/")) == []


def test_an_account_id_that_is_a_path_is_refused(service: AuthFlowService) -> None:
    """`Path` joins swallow an absolute segment whole and ".." walks out of the root, and
    every failure path removes the resolved directory -- so an id off the wire has to be
    resolved through the index, not the filesystem."""
    for hostile in ("../..", "/home/user/workspace", "../../.claude"):
        with pytest.raises((FlowError, AccountError)):
            service.start("opencode-go", "api_key", account_id=hostile)


# ----- a method whose credential is printed, not persisted by the CLI ---------------------

_OAT = "sk-ant-oat01-" + "A" * 80


def test_a_minted_token_is_written_into_the_account(tmp_path: Path) -> None:
    """`claude setup-token` prints a 1-year token and persists NOTHING -- its credential-store
    write is on the other arm of the OAuth completion. The token has to come off the screen
    and into the account, or the probe reads an empty folder and the flow fails with a valid
    token sitting in the pane."""
    process = FakePexpectProcess(
        [(0, "Visit https://claude.ai/oauth/authorize?code=1")],
        drain_chunks=["Visit https://claude.ai/oauth/authorize?code=1\r\n", f"{_OAT}\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("anthropic", "setup_token")
    process.exit()

    status = service.poll(started.flow_id)

    assert status.state is FlowState.OK
    (account,) = read_index(tmp_path).accounts
    settings = json.loads((tmp_path / ".minds" / "accounts" / account.id / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _OAT}


def test_a_token_flow_that_prints_nothing_fails_rather_than_committing(tmp_path: Path) -> None:
    """Committing here would offer an account whose folder holds no credential at all."""
    process = FakePexpectProcess(
        [(0, "Visit https://claude.ai/oauth/authorize?code=1")],
        drain_chunks=["Visit https://claude.ai/oauth/authorize?code=1\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("anthropic", "setup_token")
    process.exit()

    assert service.poll(started.flow_id).state is FlowState.FAILED
    assert read_index(tmp_path).accounts == ()


def test_a_token_flow_still_running_keeps_waiting(tmp_path: Path) -> None:
    """The token appears well after the URL does; a poll in between is not a failure."""
    process = FakePexpectProcess(
        [(0, "Visit https://claude.ai/oauth/authorize?code=1")],
        drain_chunks=["Visit https://claude.ai/oauth/authorize?code=1\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("anthropic", "setup_token")

    assert service.poll(started.flow_id).state is FlowState.PENDING


def test_a_bare_oauth_token_pasted_into_the_key_field_is_not_read_as_an_api_key(
    tmp_path: Path,
) -> None:
    """They are different managed keys and claude reads them from different variables, so
    filing a token under ANTHROPIC_API_KEY leaves the account signed out."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES
    )
    started = service.start("anthropic", "api_key")

    service.submit_key(started.flow_id, _OAT)

    (account,) = read_index(tmp_path).accounts
    settings = json.loads((tmp_path / ".minds" / "accounts" / account.id / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _OAT}


def test_a_pasted_api_key_is_approved_so_claude_does_not_challenge_it(tmp_path: Path) -> None:
    """Interactive claude blocks on a "do you want to use this API key?" dialog for any key
    it has not been told about -- and blocks before signalling ready, so `mngr create`
    destroys the agent on its readiness timeout."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES
    )
    started = service.start("anthropic", "api_key")

    service.submit_key(started.flow_id, "sk-ant-api03-" + "B" * 40)

    (account,) = read_index(tmp_path).accounts
    config = json.loads((tmp_path / ".minds" / "accounts" / account.id / ".claude.json").read_text())
    assert config["customApiKeyResponses"]["approved"], "the key was not pre-approved"


def test_re_keying_through_imbue_reuses_its_own_account(tmp_path: Path) -> None:
    """The keys page posts on every visit. A fresh account per visit leaves a row per
    re-key, all but the newest holding a dead credential -- and the newest silently becoming
    the account every new chat launches on."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES
    )

    first = service.adopt_claude_credentials("sk-ant-api03-" + "A" * 40)
    second = service.adopt_claude_credentials("sk-ant-api03-" + "B" * 40)

    assert second.id == first.id
    assert len(read_index(tmp_path).accounts) == 1
    settings = json.loads((tmp_path / ".minds" / "accounts" / first.id / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"].endswith("B" * 40)


def test_re_keying_does_not_overwrite_a_browser_sign_in(tmp_path: Path) -> None:
    """Both live on the anthropic lane, so matching on the lane alone would let the keys
    page silently replace the credential the user signed in with."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES
    )
    started = service.start("anthropic", "api_key")
    service.submit_key(started.flow_id, "sk-ant-api03-" + "C" * 40)
    (signed_in,) = read_index(tmp_path).accounts

    adopted = service.adopt_claude_credentials("sk-ant-api03-" + "D" * 40)

    assert adopted.id != signed_in.id
    settings = json.loads((tmp_path / ".minds" / "accounts" / signed_in.id / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"].endswith("C" * 40)


def test_a_re_auth_is_judged_on_the_new_sign_in_not_the_old_credential(tmp_path: Path) -> None:
    """The one thing re-auth exists for, and it could not fail.

    A re-auth keeps the folder, and three of the four promote probes are presence checks --
    `claude auth status --json` reports loggedIn for a bogus key, and so do codex and pi. So
    a user whose account died, who hit re-auth and then declined in the browser, was told
    "signed in again" on the strength of the credential that was already there.
    """
    saw: list[bool] = []

    def probe(_harness: object, path: Path) -> SignedIn:
        # What the CLI would see: is there a credential in this folder right now?
        saw.append((path / "auth.json").exists())
        return SignedIn.YES if saw[-1] else SignedIn.NO

    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=probe)
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "live-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"

    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    driving = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process, probe=probe
    )
    driving.start("google", "oauth", account_id=account.id)

    assert not path.exists(), "the old credential is still there for the probe to answer with"


def test_an_abandoned_re_auth_puts_the_old_credential_back(tmp_path: Path) -> None:
    """Taking it away is only safe if every exit restores it: the credential the account had
    is more use than none, and the user asked to REPLACE it, not to lose it."""
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES
    )
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "live-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"
    before = path.read_text()

    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    driving = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process, probe=lambda *_a: SignedIn.YES
    )
    again = driving.start("google", "oauth", account_id=account.id)
    driving.abort(again.flow_id)

    assert path.read_text() == before


def test_a_first_sign_in_clears_nothing(tmp_path: Path) -> None:
    """There is nothing to take away, and nothing to put back if it fails."""
    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    service = AuthFlowService.create(
        home=tmp_path, work_dir=tmp_path / "work", spawner=lambda *_a, **_k: process, probe=lambda *_a: SignedIn.NO
    )
    started = service.start("google", "oauth")
    service.abort(started.flow_id)

    assert read_index(tmp_path).accounts == ()


def test_a_successful_re_auth_restarts_the_chats_bound_to_that_account(tmp_path: Path) -> None:
    """Re-auth is only worth doing if the chats come back, and they do not on their own:
    claude reads its settings env at process start, and nothing establishes that codex's
    daemon re-reads a swapped credential."""
    restarted: list[str] = []
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        probe=lambda *_a: SignedIn.YES,
        restart_bound_agents=lambda account_id: (restarted.append(account_id), 2)[1],
    )
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "first-key", "opencode-go")
    (account,) = read_index(tmp_path).accounts
    assert restarted == [], "a first sign-in has no chats to restart"

    again = service.start("opencode-go", "api_key", account_id=account.id)
    service.submit_key(again.flow_id, "second-key", "opencode-go")

    assert restarted == [account.id]


def test_a_failed_re_auth_restarts_nothing(tmp_path: Path) -> None:
    """The account still holds the credential it had, so its chats are working. Restarting
    them would interrupt turns for no reason."""
    verdicts = [SignedIn.YES, SignedIn.NO]
    restarted: list[str] = []
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        probe=lambda *_a: verdicts.pop(0),
        restart_bound_agents=lambda account_id: (restarted.append(account_id), 1)[1],
    )
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "good", "opencode-go")
    (account,) = read_index(tmp_path).accounts

    again = service.start("opencode-go", "api_key", account_id=account.id)
    assert service.submit_key(again.flow_id, "bad", "opencode-go").state is FlowState.FAILED

    assert restarted == []
