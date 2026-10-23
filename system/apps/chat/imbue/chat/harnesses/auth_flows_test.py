"""Tests for sign-in flows.

Only the paths that do not need a live CLI: the paste lanes (which are file writes), the
shape mapping the modal branches on, and the lifecycle around a flow. Driving a real PTY
belongs in a manual check against the actual binaries, not here -- a fake terminal would
only assert that our fake behaves like our fake.
"""

import contextlib
import fcntl
import itertools
import json
import os
import re
import stat
import threading
from collections.abc import Callable
from collections.abc import Generator
from pathlib import Path

import pytest
from pydantic import SecretStr

from imbue.chat.accounts import AccountError
from imbue.chat.accounts import account_dir
from imbue.chat.accounts import commit_account
from imbue.chat.accounts import harness_for
from imbue.chat.accounts import mint_account_dir
from imbue.chat.accounts import read_index
from imbue.chat.harnesses.auth_flows import AuthFlowService
from imbue.chat.harnesses.auth_flows import FlowError
from imbue.chat.harnesses.auth_flows import FlowShape
from imbue.chat.harnesses.auth_flows import FlowState
from imbue.chat.harnesses.auth_flows import _auth_command_signatures
from imbue.chat.harnesses.auth_flows import flow_shape
from imbue.chat.harnesses.grok.testing import GROK_SESSION_SCOPE
from imbue.chat.harnesses.grok.testing import write_grok_auth
from imbue.chat.harnesses.harness_type import HarnessType
from imbue.chat.harnesses.lanes import get_method
from imbue.chat.harnesses.registry import build_account_binding
from imbue.chat.harnesses.sign_in_relay import RelayCallbackError
from imbue.chat.harnesses.signed_in import SignedIn
from imbue.chat.testing import FakePexpectProcess


@pytest.fixture
def service(tmp_path: Path) -> AuthFlowService:
    work_dir = tmp_path / "workspace"
    work_dir.mkdir()
    return AuthFlowService.create(
        home=tmp_path, work_dir=work_dir, probe=lambda *_a: SignedIn.YES, xai_key_probe=lambda _key: SignedIn.YES
    )


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
    # The same lane as the device flow above, with the other shape.
    assert flow_shape(get_method("openai", "api_key")) is FlowShape.PASTE
    # grok's device sign-in is a terminal one, and still shows a code rather than taking one back.
    assert flow_shape(get_method("xai", "device")) is FlowShape.CODE_THEN_WAIT
    assert flow_shape(get_method("xai", "api_key")) is FlowShape.PASTE


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


def test_a_codex_key_lands_in_the_account_auth_json(service: AuthFlowService, tmp_path: Path) -> None:
    """The file codex reads its credential from, written directly in its API-key shape rather than
    the token shape a device login leaves there -- which is what makes an OpenAI account mintable
    without a person at a browser."""
    started = service.start("openai", "api_key")
    assert started.shape is FlowShape.PASTE

    status = service.submit_key(started.flow_id, "sk-openai-123")

    assert status.state is FlowState.OK
    (account,) = read_index(tmp_path).accounts
    assert account.display == "OpenAI"
    assert harness_for(account) is HarnessType.CODEX
    account_path = tmp_path / ".minds" / "accounts" / account.id
    path = account_path / "auth.json"
    assert json.loads(path.read_text()) == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-openai-123"}
    # codex writes its own auth.json 0600, and this file holds the same secret.
    assert path.stat().st_mode & 0o077 == 0
    # A device login writes its own credential wherever codex keeps one; a paste writes this file
    # and nothing else, so the store pin beside it is what decides whether codex reads it at all.
    assert 'cli_auth_credentials_store = "file"' in (account_path / "config.toml").read_text()


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


def test_re_authenticating_reuses_the_folder_so_bound_chats_recover(service: AuthFlowService, tmp_path: Path) -> None:
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

    # It reached the probe rather than parking on PENDING. Asserting OK specifically is what
    # makes this test mean anything: the fixture's probe says YES, so reaching it gives OK and
    # not reaching it gives PENDING -- and accepting either would accept the bug it is named
    # after.
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
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.NO)
    started = service.start("api-key", "api_key")

    status = service.submit_key(started.flow_id, "sk-wrong", "groq")

    assert status.state is FlowState.FAILED
    assert read_index(tmp_path).accounts == ()


def test_a_probe_that_cannot_run_does_not_throw_the_key_away(tmp_path: Path) -> None:
    """UNKNOWN is "the check failed", not "the key is bad" -- and the user just pasted it."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.UNKNOWN)
    started = service.start("api-key", "api_key")

    status = service.submit_key(started.flow_id, "sk-probably-fine", "groq")

    assert status.state is FlowState.OK
    assert len(read_index(tmp_path).accounts) == 1


def test_an_abandoned_re_auth_leaves_the_live_account_alone(service: AuthFlowService, tmp_path: Path) -> None:
    """Aborting a re-auth must not delete the account it is re-authenticating.

    A re-auth adopts a COMMITTED folder, so a failure path that discards unconditionally means
    pressing Back, closing the modal or letting the deadline pass takes the credential with it
    and orphans every chat bound to that id.
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


@pytest.mark.parametrize(
    ("lane_id", "key_provider"),
    # Both sinks that write an account's auth.json, since the rollback restores whatever the
    # lane's sink put there and the per-lane part of it is naming that file at all.
    [("opencode-go", "opencode-go"), ("openai", None)],
)
def test_a_rejected_re_auth_key_puts_the_working_one_back(
    tmp_path: Path, lane_id: str, key_provider: str | None
) -> None:
    """The probe needs the file in place to answer, so the write comes first -- but the
    folder is a live account, and a rejected key left there breaks every bound agent
    silently, at its next turn, with the row still saying the account is fine."""
    verdicts = [SignedIn.YES, SignedIn.NO]
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: verdicts.pop(0))
    started = service.start(lane_id, "api_key")
    service.submit_key(started.flow_id, "good-key", key_provider)
    (account,) = read_index(tmp_path).accounts
    path = tmp_path / ".minds" / "accounts" / account.id / "auth.json"
    accepted = path.read_text()
    assert "good-key" in accepted

    again = service.start(lane_id, "api_key", account_id=account.id)
    status = service.submit_key(again.flow_id, "bad-key", key_provider)

    assert status.state is FlowState.FAILED
    # Byte for byte what the accepted key wrote: a restore that put the key back but lost the rest
    # of the file would leave the account just as broken.
    assert path.read_text() == accepted
    assert read_index(tmp_path).accounts == (account,)


def test_a_rejected_first_key_leaves_no_folder_behind(tmp_path: Path) -> None:
    """The other half of the same rule: a folder this flow minted IS ours to remove."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.NO)
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


# a method whose credential is printed, not persisted by the CLI

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
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    started = service.start("anthropic", "api_key")

    service.submit_key(started.flow_id, _OAT)

    (account,) = read_index(tmp_path).accounts
    settings = json.loads((tmp_path / ".minds" / "accounts" / account.id / "settings.json").read_text())
    assert settings["env"] == {"CLAUDE_CODE_OAUTH_TOKEN": _OAT}


def test_a_pasted_api_key_is_approved_so_claude_does_not_challenge_it(tmp_path: Path) -> None:
    """Interactive claude blocks on a "do you want to use this API key?" dialog for any key
    it has not been told about -- and blocks before signalling ready, so `mngr create`
    destroys the agent on its readiness timeout."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    started = service.start("anthropic", "api_key")

    service.submit_key(started.flow_id, "sk-ant-api03-" + "B" * 40)

    (account,) = read_index(tmp_path).accounts
    config = json.loads((tmp_path / ".minds" / "accounts" / account.id / ".claude.json").read_text())
    assert config["customApiKeyResponses"]["approved"], "the key was not pre-approved"


def test_re_keying_through_imbue_reuses_its_own_account(tmp_path: Path) -> None:
    """The keys page posts on every visit. A fresh account per visit leaves a row per
    re-key, all but the newest holding a dead credential -- and the newest silently becoming
    the account every new chat launches on."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)

    first = service.adopt_claude_credentials("sk-ant-api03-" + "A" * 40)
    second = service.adopt_claude_credentials("sk-ant-api03-" + "B" * 40)

    assert second.id == first.id
    assert len(read_index(tmp_path).accounts) == 1
    settings = json.loads((tmp_path / ".minds" / "accounts" / first.id / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"].endswith("B" * 40)


def test_re_keying_does_not_overwrite_a_browser_sign_in(tmp_path: Path) -> None:
    """Both live on the anthropic lane, so matching on the lane alone would let the keys
    page silently replace the credential the user signed in with."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    started = service.start("anthropic", "api_key")
    service.submit_key(started.flow_id, "sk-ant-api03-" + "C" * 40)
    (signed_in,) = read_index(tmp_path).accounts

    adopted = service.adopt_claude_credentials("sk-ant-api03-" + "D" * 40)

    assert adopted.id != signed_in.id
    settings = json.loads((tmp_path / ".minds" / "accounts" / signed_in.id / "settings.json").read_text())
    assert settings["env"]["ANTHROPIC_API_KEY"].endswith("C" * 40)


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
        restart_bound_agents=restarted.append,
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
        restart_bound_agents=restarted.append,
    )
    started = service.start("opencode-go", "api_key")
    service.submit_key(started.flow_id, "good", "opencode-go")
    (account,) = read_index(tmp_path).accounts

    again = service.start("opencode-go", "api_key", account_id=account.id)
    assert service.submit_key(again.flow_id, "bad", "opencode-go").state is FlowState.FAILED

    assert restarted == []


def _agy_account(tmp_path: Path, service: AuthFlowService) -> tuple[str, Path]:
    """A committed google account with a credential file where agy keeps one."""
    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    service._spawner = lambda *_a, **_k: process
    started = service.start("google", "oauth")
    service.submit_code(started.flow_id, "4/0Aexample")
    (account,) = read_index(tmp_path).accounts
    (token,) = build_account_binding(HarnessType.ANTIGRAVITY).credential_paths(
        tmp_path / ".minds" / "accounts" / account.id
    )
    token.parent.mkdir(parents=True, exist_ok=True)
    token.write_text("live-token")
    return account.id, token


def test_a_re_auth_leaves_the_live_credential_in_place(tmp_path: Path) -> None:
    """The account keeps working while its owner signs in again; the CLI writes over it."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    account_id, token = _agy_account(tmp_path, service)

    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    service._spawner = lambda *_a, **_k: process
    service.start("google", "oauth", account_id=account_id)

    assert token.read_text() == "live-token"


def test_a_re_auth_is_not_decided_by_the_credential_it_replaces(tmp_path: Path) -> None:
    """agy's probe only sees that a credential is there, so it says yes for the old one as well.
    The re-auth stays pending until agy writes a new credential, and then the probe decides."""
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    account_id, token = _agy_account(tmp_path, service)
    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    service._spawner = lambda *_a, **_k: process
    again = service.start("google", "oauth", account_id=account_id)

    assert service.submit_code(again.flow_id, "4/0Aexample").state is FlowState.PENDING

    token.write_text("new-token")
    assert service.poll(again.flow_id).state is FlowState.OK


def _finished_claude(output: str, exitstatus: int | None) -> FakePexpectProcess:
    process = FakePexpectProcess(
        [(0, f"Browser didn't open? Use the url below\r\n{_CLAUDE_MANUAL_URL}")],
        drain_chunks=[output],
        exitstatus=exitstatus,
    )
    process.exit()
    return process


@pytest.mark.parametrize(
    ("output", "exitstatus", "probe_answer", "state", "is_probed"),
    [
        pytest.param("Login successful.\r\n", 0, SignedIn.NO, FlowState.OK, False, id="success-line-and-clean-exit"),
        pytest.param("", 0, SignedIn.YES, FlowState.OK, True, id="clean-exit-reworded-probe-confirms"),
        pytest.param("", 0, SignedIn.NO, FlowState.FAILED, True, id="clean-exit-reworded-probe-refuses"),
        pytest.param("Login successful.\r\n", 1, SignedIn.YES, FlowState.FAILED, False, id="line-then-an-error-exit"),
        pytest.param("Login successful.\r\n", None, SignedIn.YES, FlowState.FAILED, False, id="killed"),
    ],
)
def test_claude_decides_its_own_sign_in(
    tmp_path: Path, output: str, exitstatus: int | None, probe_answer: SignedIn, state: FlowState, is_probed: bool
) -> None:
    """The CLI's exit decides. The probe, which would also answer yes for the credential a re-auth
    replaces, is asked only about a clean exit whose success line did not match its wording."""
    probed: list[HarnessType] = []

    def probe(harness: HarnessType, _path: Path) -> SignedIn:
        probed.append(harness)
        return probe_answer

    process = _finished_claude(output, exitstatus)
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=probe,
    )
    started = service.start("anthropic", "subscription")

    assert service.poll(started.flow_id).state is state
    assert (probed != []) is is_probed


def test_a_browser_re_auth_takes_away_the_env_credential_that_would_outrank_it(tmp_path: Path) -> None:
    """claude ranks a key or token in the settings env above what `claude auth login` writes, so one
    left behind would keep every chat on the old credential after "Signed in again"."""
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: _finished_claude("Login successful.\r\n", 0),
        probe=lambda *_a: SignedIn.YES,
    )
    pasted = service.start("anthropic", "api_key")
    service.submit_key(pasted.flow_id, "sk-ant-old", None)
    (account,) = read_index(tmp_path).accounts
    settings_path = tmp_path / ".minds" / "accounts" / account.id / "settings.json"
    settings = json.loads(settings_path.read_text())
    settings["env"]["DISABLE_TELEMETRY"] = "1"
    settings_path.write_text(json.dumps(settings))

    again = service.start("anthropic", "subscription", account_id=account.id)

    assert service.poll(again.flow_id).state is FlowState.OK
    assert json.loads(settings_path.read_text())["env"] == {"DISABLE_TELEMETRY": "1"}


def test_a_claude_sign_in_still_running_waits(tmp_path: Path) -> None:
    process = FakePexpectProcess(
        [(0, f"Browser didn't open? Use the url below\r\n{_CLAUDE_MANUAL_URL}")],
        drain_chunks=["Login successful.\r\n"],
    )
    service = AuthFlowService.create(
        home=tmp_path,
        work_dir=tmp_path / "work",
        spawner=lambda *_a, **_k: process,
        probe=lambda *_a: SignedIn.YES,
    )
    started = service.start("anthropic", "subscription")

    assert service.poll(started.flow_id).state is FlowState.PENDING


def test_an_abandoned_re_auth_leaves_the_credential_as_it_was(tmp_path: Path) -> None:
    service = AuthFlowService.create(home=tmp_path, work_dir=tmp_path / "work", probe=lambda *_a: SignedIn.YES)
    account_id, token = _agy_account(tmp_path, service)

    process = FakePexpectProcess([(0, f"Visit {_AGY_URL}")], drain_chunks=[f"Visit {_AGY_URL}\r\n"])
    service._spawner = lambda *_a, **_k: process
    again = service.start("google", "oauth", account_id=account_id)
    service.abort(again.flow_id)

    assert token.read_text() == "live-token"


# What `claude auth login` prints for a person to open by hand: its callback is Anthropic's own page.
_CLAUDE_MANUAL_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=user%3Ainference&code_challenge=abc&code_challenge_method=S256&state=manual-state"
)
# What it hands `$BROWSER`: the same sign-in, called back on its own loopback listener.
_CLAUDE_RELAY_URL = (
    "https://claude.ai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=http%3A%2F%2Flocalhost%3A54871%2Fcallback"
    "&scope=user%3Ainference&code_challenge=abc&code_challenge_method=S256&state=relay-state"
)


def _claude_flow_service(
    tmp_path: Path,
    process: FakePexpectProcess,
    relay_url: str | None,
    fetched: list[tuple[int, str]],
    on_callback: Callable[[], None] = lambda: None,
) -> AuthFlowService:
    """A service whose fake `claude` runs `$BROWSER` with `relay_url`, as the real one does.

    `on_callback` is what the CLI does when the relayed callback reaches its listener. The clock
    moves ten seconds each time it is read, so a flow still pending after the callback is reported
    as such within a few polls rather than after the real wait.
    """

    def spawner(*_args: object, env: dict[str, str], **_kwargs: object) -> FakePexpectProcess:
        if relay_url is not None:
            Path(env["MINDS_SIGNIN_URL_FILE"]).write_text(relay_url + "\n")
        return process

    def fetch(port: int, path_and_query: str) -> None:
        fetched.append((port, path_and_query))
        on_callback()

    ticks = itertools.count(start=0.0, step=10.0)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    return AuthFlowService.create(
        home=tmp_path,
        work_dir=work_dir,
        spawner=spawner,
        probe=lambda *_a: SignedIn.YES,
        fetch_callback=fetch,
        clock=lambda: next(ticks),
    )


def _claude_process() -> FakePexpectProcess:
    return FakePexpectProcess([(0, f"Browser didn't open? Use the url below\r\n{_CLAUDE_MANUAL_URL}")])


def test_a_claude_sign_in_offers_the_page_its_cli_opened_for_relaying(tmp_path: Path) -> None:
    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_RELAY_URL, [])

    started = service.start("anthropic", "subscription")

    assert started.url == _CLAUDE_MANUAL_URL
    assert started.relay_url == _CLAUDE_RELAY_URL


def test_a_claude_sign_in_whose_cli_opened_nothing_offers_no_relay(tmp_path: Path) -> None:
    service = _claude_flow_service(tmp_path, _claude_process(), None, [])

    started = service.start("anthropic", "subscription")

    assert started.url == _CLAUDE_MANUAL_URL
    assert started.relay_url is None


def test_a_page_whose_callback_is_not_loopback_is_not_offered_for_relaying(tmp_path: Path) -> None:
    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_MANUAL_URL, [])

    assert service.start("anthropic", "subscription").relay_url is None


def test_the_callback_is_replayed_against_the_port_the_sign_in_named_and_answered_with_the_verdict(
    tmp_path: Path,
) -> None:
    fetched: list[tuple[int, str]] = []
    process = _claude_process()

    def sign_in() -> None:
        process.print_output("Login successful.\r\n")
        process.exit(0)

    service = _claude_flow_service(tmp_path, process, _CLAUDE_RELAY_URL, fetched, on_callback=sign_in)
    started = service.start("anthropic", "subscription")

    status = service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")

    assert fetched == [(54871, "/callback?code=the-code&state=relay-state")]
    assert status.state is FlowState.OK
    assert status.account_id is not None


def test_a_callback_carrying_a_denial_is_answered_with_why_the_sign_in_failed(tmp_path: Path) -> None:
    process = _claude_process()

    def deny() -> None:
        process.print_output("Login failed: No authorization code received\r\n")
        process.exit(1)

    service = _claude_flow_service(tmp_path, process, _CLAUDE_RELAY_URL, [], on_callback=deny)
    started = service.start("anthropic", "subscription")

    status = service.relay_callback(started.flow_id, "/callback?error=access_denied&state=relay-state")

    assert status.state is FlowState.FAILED
    assert status.detail == "Access wasn't approved on Claude's page."


def test_a_callback_the_cli_does_not_take_is_answered_with_how_the_flow_stands(tmp_path: Path) -> None:
    def unreachable() -> None:
        raise RelayCallbackError("the sign-in did not answer (ConnectTimeout)")

    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_RELAY_URL, [], on_callback=unreachable)
    started = service.start("anthropic", "subscription")

    status = service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")

    assert status.state is FlowState.PENDING


def test_a_relayed_callback_gives_the_flow_the_short_verdict_deadline(tmp_path: Path) -> None:
    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_RELAY_URL, [])
    started = service.start("anthropic", "subscription")

    status = service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")

    assert status.state is FlowState.PENDING
    session = service._session
    assert session is not None and session.timer is not None
    assert session.timer.interval == 120.0
    service.abort(started.flow_id)


@pytest.mark.parametrize(
    "path_and_query",
    [
        pytest.param("/callback?code=the-code&state=someone-elses", id="wrong-state"),
        pytest.param("/callback?code=the-code", id="no-state"),
        pytest.param("/elsewhere?code=the-code&state=relay-state", id="wrong-path"),
        pytest.param("//evil.example/callback?state=relay-state", id="another-host"),
        pytest.param("http://127.0.0.1:22/?state=relay-state", id="absolute-url"),
    ],
)
def test_a_first_request_that_is_not_this_sign_ins_callback_is_refused(tmp_path: Path, path_and_query: str) -> None:
    fetched: list[tuple[int, str]] = []
    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_RELAY_URL, fetched)
    started = service.start("anthropic", "subscription")

    with pytest.raises(FlowError):
        service.relay_callback(started.flow_id, path_and_query)

    assert fetched == []


def test_the_callback_is_taken_only_once(tmp_path: Path) -> None:
    fetched: list[tuple[int, str]] = []
    service = _claude_flow_service(tmp_path, _claude_process(), _CLAUDE_RELAY_URL, fetched)
    started = service.start("anthropic", "subscription")

    service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")
    with pytest.raises(FlowError, match="already been handled"):
        service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")

    assert fetched == [(54871, "/callback?code=the-code&state=relay-state")]


def test_a_sign_in_without_a_relay_takes_no_callback(tmp_path: Path) -> None:
    service = _claude_flow_service(tmp_path, _claude_process(), None, [])
    started = service.start("anthropic", "subscription")

    with pytest.raises(FlowError):
        service.relay_callback(started.flow_id, "/callback?code=the-code&state=relay-state")


def test_the_sign_in_url_file_goes_with_the_flow(tmp_path: Path) -> None:
    url_files: list[Path] = []

    def spawner(*_args: object, env: dict[str, str], **_kwargs: object) -> FakePexpectProcess:
        url_files.append(Path(env["MINDS_SIGNIN_URL_FILE"]))
        url_files[0].write_text(_CLAUDE_RELAY_URL + "\n")
        return _claude_process()

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    service = AuthFlowService.create(home=tmp_path, work_dir=work_dir, spawner=spawner, probe=lambda *_a: SignedIn.YES)
    started = service.start("anthropic", "subscription")
    assert url_files[0].exists()

    service.abort(started.flow_id)

    assert not url_files[0].parent.exists()


def test_the_cli_is_given_the_browser_shim(tmp_path: Path) -> None:
    browsers: list[str] = []

    def spawner(*_args: object, env: dict[str, str], **_kwargs: object) -> FakePexpectProcess:
        browsers.append(env["BROWSER"])
        return _claude_process()

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    service = AuthFlowService.create(home=tmp_path, work_dir=work_dir, spawner=spawner, probe=lambda *_a: SignedIn.YES)

    service.start("anthropic", "console")

    assert browsers == [str(work_dir / "system" / "scripts" / "minds_browser_shim")]


# grok's device sign-in, as `grok login --device-auth` 1.0.62 prints it on an 80-column PTY, with
# xAI's own page in place of the loopback issuer it was measured against.

_GROK_PAGE = "https://accounts.x.ai/oauth2/device?user_code=ABCD-1234"
_GROK_CODE = "ABCD-1234"
_GROK_PROMPT = (
    f"\r\nTo sign in, open this URL in your browser:\r\n\r\n  {_GROK_PAGE}\r\n\r\n"
    "Confirm this code in your browser:\r\n\r\n"
)
_GROK_DEVICE_SCREEN = (
    f"{_GROK_PROMPT}  {_GROK_CODE}\r\n\r\n"
    "\x1b[90mOnly continue with a code you requested. Don't share it with anyone.\x1b[0m\r\n\r\n"
    "Waiting for authorization...\r\n"
)
# What it prints when xAI's page does not carry the code, and when no browser opens.
_GROK_DEVICE_SCREEN_TO_TYPE = (
    "\r\nTo sign in, open this URL in your browser:\r\n\r\n  https://accounts.x.ai/oauth2/device\r\n\r\n"
    "  (Could not open browser automatically \u2014 open the URL above manually.)\r\n\r\n"
    f"Then enter this code:\r\n\r\n  {_GROK_CODE}\r\n\r\n"
    "Waiting for authorization...\r\n"
)
_GROK_SIGNED_IN = "\r\x1b[K\u2713 Signed in as someone@example.test\r\n\r\n"
_GROK_LOGIN = {"key": "access-token", "auth_mode": "oidc", "refresh_token": "refresh-token"}


def _grok_service(
    tmp_path: Path,
    process: FakePexpectProcess | None = None,
    restart_bound_agents: Callable[[str], None] = lambda _id: None,
    probe: Callable[[HarnessType, Path], SignedIn] = lambda *_a: SignedIn.YES,
    xai_key_probe: Callable[[SecretStr], SignedIn] = lambda _key: SignedIn.YES,
    grok_auth_lock_wait_seconds: float | None = None,
    spawner: Callable[..., FakePexpectProcess] | None = None,
) -> AuthFlowService:
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    return AuthFlowService.create(
        home=tmp_path,
        work_dir=work_dir,
        spawner=spawner or (lambda *_a, **_k: process),
        probe=probe,
        restart_bound_agents=restart_bound_agents,
        xai_key_probe=xai_key_probe,
        grok_auth_lock_wait_seconds=grok_auth_lock_wait_seconds,
    )


def _grok_auth_path(tmp_path: Path, account_id: str) -> Path:
    return account_dir(account_id, tmp_path) / "auth.json"


def _start_device_reauth_over_a_pasted_key(service: AuthFlowService, tmp_path: Path) -> tuple[str, str, Path]:
    """An xAI account holding a pasted key, mid device re-auth, once grok has added its login beside the key.

    Returns the re-auth's flow id, the account id and the account's auth.json."""
    pasted = service.start("xai", "api_key")
    service.submit_key(pasted.flow_id, "xai-old-key")
    (account,) = read_index(tmp_path).accounts
    auth_path = _grok_auth_path(tmp_path, account.id)
    again = service.start("xai", "device", account_id=account.id)
    write_grok_auth(auth_path.parent, {**json.loads(auth_path.read_text()), GROK_SESSION_SCOPE: _GROK_LOGIN})
    return again.flow_id, account.id, auth_path


def _grok_session_account(tmp_path: Path) -> tuple[str, Path]:
    """A committed xAI account whose auth.json holds a session login alone, as `grok login` leaves one."""
    account_id, folder = mint_account_dir(tmp_path)
    write_grok_auth(folder, {GROK_SESSION_SCOPE: _GROK_LOGIN})
    commit_account(account_id, "xai", "xAI", tmp_path)
    return account_id, folder / "auth.json"


def _take_grok_lock(auth_path: Path) -> int:
    """Take the lock grok holds while it rewrites ``auth_path``; closing the returned fd releases it."""
    lock_fd = os.open(auth_path.with_name("auth.json.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    return lock_fd


@contextlib.contextmanager
def _grok_holds_its_lock(auth_path: Path) -> Generator[None, None, None]:
    lock_fd = _take_grok_lock(auth_path)
    try:
        yield
    finally:
        os.close(lock_fd)


@pytest.mark.parametrize(
    ("screen", "page"),
    [
        pytest.param(_GROK_DEVICE_SCREEN, _GROK_PAGE, id="page-carries-the-code"),
        pytest.param(_GROK_DEVICE_SCREEN_TO_TYPE, "https://accounts.x.ai/oauth2/device", id="code-to-type"),
    ],
)
def test_a_grok_device_sign_in_shows_the_page_and_the_code_and_waits(tmp_path: Path, screen: str, page: str) -> None:
    process = FakePexpectProcess([(0, screen)])
    service = _grok_service(tmp_path, process)

    started = service.start("xai", "device")

    assert started.shape is FlowShape.CODE_THEN_WAIT
    assert (started.url, started.code) == (page, _GROK_CODE)
    assert service.poll(started.flow_id).state is FlowState.PENDING


def test_a_grok_device_sign_in_reads_on_until_the_code_has_arrived(tmp_path: Path) -> None:
    """The code follows the page, so a screen showing only the page is not ready to read."""
    service = _grok_service(tmp_path, FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)]))
    service.start("xai", "device")
    session = service._session
    assert session is not None

    assert not session.is_value_ready(_GROK_PROMPT)
    assert session.is_value_ready(_GROK_DEVICE_SCREEN)


def test_a_grok_device_sign_in_gives_its_cli_the_browser_shim_and_relays_nothing(tmp_path: Path) -> None:
    """grok opens its page itself, and the page has no loopback callback for the desktop app to relay."""
    browsers: list[str] = []

    def spawner(*_args: object, env: dict[str, str], **_kwargs: object) -> FakePexpectProcess:
        browsers.append(env["BROWSER"])
        Path(env["MINDS_SIGNIN_URL_FILE"]).write_text(_GROK_PAGE + "\n")
        return FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])

    service = _grok_service(tmp_path, spawner=spawner)

    started = service.start("xai", "device")

    assert browsers == [str(tmp_path / "work" / "system" / "scripts" / "minds_browser_shim")]
    assert started.relay_url is None


def test_a_grok_device_sign_in_takes_no_code_back(tmp_path: Path) -> None:
    """Its code goes into the browser; typed into the CLI it would only cut the user's time to confirm it."""
    process = FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])
    service = _grok_service(tmp_path, process)
    started = service.start("xai", "device")

    with pytest.raises(FlowError):
        service.submit_code(started.flow_id, _GROK_CODE)

    assert process.send_calls == []


@pytest.mark.parametrize(
    ("output", "exitstatus", "state", "detail"),
    [
        pytest.param(_GROK_SIGNED_IN, 0, FlowState.OK, None, id="signed-in-as"),
        pytest.param("\r\x1b[K\u2713 Signed in\r\n\r\n", 0, FlowState.OK, None, id="signed-in-without-an-email"),
        pytest.param(
            "Error: Authorization denied. The user rejected the request.\r\n",
            1,
            FlowState.FAILED,
            "Access wasn't approved on xAI's page.",
            id="denied",
        ),
        pytest.param(
            "Error: Device code expired. Run `grok login --device-auth` again.\r\n",
            1,
            FlowState.FAILED,
            "The code expired before it was confirmed. Start over to get a fresh one.",
            id="expired",
        ),
        pytest.param(
            "Error: Token exchange error: invalid grant\r\n",
            1,
            FlowState.FAILED,
            "Token exchange error: invalid grant",
            id="any-other-error-in-grok-s-words",
        ),
        pytest.param("", 1, FlowState.FAILED, "The sign-in did not complete.", id="an-error-exit-with-no-line"),
    ],
)
def test_grok_decides_its_own_device_sign_in(
    tmp_path: Path, output: str, exitstatus: int, state: FlowState, detail: str | None
) -> None:
    """Every line here is one `grok login --device-auth` 1.0.62 printed, and its exit is the one it made."""
    probed: list[HarnessType] = []

    def probe(harness: HarnessType, _path: Path) -> SignedIn:
        probed.append(harness)
        return SignedIn.YES

    process = FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])
    service = _grok_service(tmp_path, process, probe=probe)
    started = service.start("xai", "device")
    process.print_output(output)
    process.exit(exitstatus)

    status = service.poll(started.flow_id)

    assert (status.state, status.detail) == (state, detail)
    assert probed == []


def test_a_grok_device_sign_in_takes_away_a_pasted_key_and_keeps_the_login(tmp_path: Path) -> None:
    """grok keeps a stored key beside the login it adds, and turns to the key whenever the login is not usable."""
    restarted: list[str] = []
    process = FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])
    service = _grok_service(tmp_path, process, restart_bound_agents=restarted.append)
    flow_id, account_id, auth_path = _start_device_reauth_over_a_pasted_key(service, tmp_path)
    process.print_output(_GROK_SIGNED_IN)
    process.exit(0)

    assert service.poll(flow_id).state is FlowState.OK
    assert json.loads(auth_path.read_text()) == {GROK_SESSION_SCOPE: _GROK_LOGIN}
    assert restarted == [account_id]


def test_a_first_grok_device_sign_in_leaves_the_file_as_grok_wrote_it(tmp_path: Path) -> None:
    process = FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])
    service = _grok_service(tmp_path, process)
    started = service.start("xai", "device")
    session = service._session
    assert session is not None
    auth_path = _grok_auth_path(tmp_path, session.account_id)
    write_grok_auth(auth_path.parent, {GROK_SESSION_SCOPE: _GROK_LOGIN})
    written = auth_path.read_text()
    inode = auth_path.stat().st_ino
    process.print_output(_GROK_SIGNED_IN)
    process.exit(0)

    assert service.poll(started.flow_id).state is FlowState.OK
    assert auth_path.read_text() == written
    assert auth_path.stat().st_ino == inode


def test_a_grok_key_lands_in_the_scope_grok_keeps_a_key_in(service: AuthFlowService, tmp_path: Path) -> None:
    """The entry grok's own key store writes, so grok reads it as an API key (measured on 1.0.62)."""
    started = service.start("xai", "api_key")
    assert started.shape is FlowShape.PASTE

    status = service.submit_key(started.flow_id, "xai-test-123")

    assert status.state is FlowState.OK
    (account,) = read_index(tmp_path).accounts
    assert account.display == "xAI"
    assert harness_for(account) is HarnessType.GROK
    path = _grok_auth_path(tmp_path, account.id)
    (scope, entry), *others = json.loads(path.read_text()).items()
    assert (scope, others) == ("xai::api_key", [])
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{6}Z", entry.pop("create_time"))
    assert entry == {
        "key": "xai-test-123",
        "auth_mode": "api_key",
        "user_id": "",
        "email": None,
        "coding_data_retention_opt_out": True,
    }
    assert path.stat().st_mode & 0o077 == 0


def test_a_key_xai_refuses_never_touches_a_grok_account_s_file(tmp_path: Path) -> None:
    """xAI judges the key itself, before anything lands. The login in the file, which would pass any key, never
    decides, and nothing is put back over a file the account's agents share and refresh."""
    judged: list[str] = []
    probed: list[Path] = []

    def xai_key_probe(key: SecretStr) -> SignedIn:
        judged.append(key.get_secret_value())
        return SignedIn.NO

    def probe(_harness: HarnessType, path: Path) -> SignedIn:
        probed.append(path)
        return SignedIn.YES

    service = _grok_service(tmp_path, probe=probe, xai_key_probe=xai_key_probe)
    account_id, path = _grok_session_account(tmp_path)
    before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)

    again = service.start("xai", "api_key", account_id=account_id)
    status = service.submit_key(again.flow_id, "xai-typo-key")

    assert (status.state, status.detail) == (FlowState.FAILED, "xAI did not accept that key.")
    assert judged == ["xai-typo-key"]
    assert probed == []
    assert (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) == before
    assert [account.id for account in read_index(tmp_path).accounts] == [account_id]


def test_a_key_xai_accepts_replaces_an_expired_login_and_the_file_whole(tmp_path: Path) -> None:
    """The login is usually why a key is pasted: it no longer works, and grok's probe of the account says no
    whatever the key. Left in the file it would outrank the key. The file is renamed into place, since every agent
    on the account reads it as it changes."""
    restarted: list[str] = []
    service = _grok_service(tmp_path, probe=lambda *_a: SignedIn.NO, restart_bound_agents=restarted.append)
    account_id, path = _grok_session_account(tmp_path)
    inode = path.stat().st_ino

    again = service.start("xai", "api_key", account_id=account_id)
    status = service.submit_key(again.flow_id, "xai-new-key")

    assert status.state is FlowState.OK
    after = json.loads(path.read_text())
    assert list(after) == ["xai::api_key"]
    assert after["xai::api_key"]["key"] == "xai-new-key"
    assert path.stat().st_ino != inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert restarted == [account_id]


def test_a_grok_key_is_refused_while_grok_holds_its_file_and_can_be_sent_again(tmp_path: Path) -> None:
    service = _grok_service(tmp_path, grok_auth_lock_wait_seconds=0.2)
    account_id, path = _grok_session_account(tmp_path)
    before = (path.read_bytes(), path.stat().st_ino)
    again = service.start("xai", "api_key", account_id=account_id)

    with _grok_holds_its_lock(path):
        with pytest.raises(FlowError, match="in use"):
            service.submit_key(again.flow_id, "xai-new-key")
        assert (path.read_bytes(), path.stat().st_ino) == before

    assert service.submit_key(again.flow_id, "xai-new-key").state is FlowState.OK
    assert json.loads(path.read_text())["xai::api_key"]["key"] == "xai-new-key"


def test_a_grok_device_sign_in_leaves_the_key_while_grok_holds_its_file(tmp_path: Path) -> None:
    """As grok's own scope removal does: the login just landed outranks the key anyway."""
    process = FakePexpectProcess([(0, _GROK_DEVICE_SCREEN)])
    service = _grok_service(tmp_path, process, grok_auth_lock_wait_seconds=0.2)
    flow_id, _account_id, auth_path = _start_device_reauth_over_a_pasted_key(service, tmp_path)
    written = auth_path.read_bytes()
    process.print_output(_GROK_SIGNED_IN)
    process.exit(0)

    with _grok_holds_its_lock(auth_path):
        assert service.poll(flow_id).state is FlowState.OK

    assert auth_path.read_bytes() == written


def test_a_grok_key_waits_out_grok_s_own_write_to_the_file(service: AuthFlowService, tmp_path: Path) -> None:
    """grok holds its lock from reading the file to writing it back. A paste that did not wait for it would
    have its key overwritten by what grok read before the paste."""
    first = service.start("xai", "api_key")
    service.submit_key(first.flow_id, "xai-old-key")
    (account,) = read_index(tmp_path).accounts
    path = _grok_auth_path(tmp_path, account.id)
    lock_fd = _take_grok_lock(path)
    read_by_grok = json.loads(path.read_text())

    def grok_writes_back_and_unlocks() -> None:
        write_grok_auth(path.parent, {**read_by_grok, GROK_SESSION_SCOPE: _GROK_LOGIN})
        os.close(lock_fd)

    again = service.start("xai", "api_key", account_id=account.id)
    grok_write = threading.Timer(0.5, grok_writes_back_and_unlocks)
    grok_write.start()
    status = service.submit_key(again.flow_id, "xai-new-key")
    grok_write.join()

    assert status.state is FlowState.OK
    after = json.loads(path.read_text())
    assert list(after) == ["xai::api_key"]
    assert after["xai::api_key"]["key"] == "xai-new-key"


def test_an_orphaned_grok_sign_in_is_one_the_boot_sweep_recognises() -> None:
    assert ("grok", "login", "--device-auth") in _auth_command_signatures()
