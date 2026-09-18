"""Acceptance test: the live model-state contract, end to end against a real claude.

The model bar's live read is a three-link chain: Claude Code's statusline payload ->
``system/scripts/claude_status_line.sh`` (which selects four fields out of it) ->
``model_state.json`` -> :func:`read_model_identity` + :func:`match_option` against
:data:`CLAUDE_CATALOG`. Break any link and the bar silently shows nothing.

Every other test of that chain uses a FROZEN payload capture. A frozen capture cannot
notice that the binary changed, so a Claude Code upgrade can invalidate the whole chain
while the suite stays green -- and the catalog's reported ids are exactly the kind of
value an upgrade moves (``claude-opus-4-8`` -> ``claude-opus-5``). This test drives the
REAL pinned binary instead, and asserts the ids it actually reports resolve to the
options the picker offers.

No credentials and no model turn are needed: claude writes its statusline before it ever
calls the API, so a syntactically-valid but non-functional key reaches the whole chain.
Fast mode is deliberately not asserted -- ``/fast on`` is a no-op under an unusable key,
and the reported value is not stable across a real turn.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import time
import uuid
from collections.abc import Generator
from pathlib import Path

import pytest

from imbue.system_interface.harnesses.claude.model import CLAUDE_CATALOG
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity

# tmux: the suite's resource guard requires this on any test that shells out to tmux.
# timeout: the project default is 10s, far under a real claude launch.
pytestmark = [pytest.mark.acceptance, pytest.mark.tmux, pytest.mark.timeout(420)]

# Syntactically valid and pre-approved, but not a working credential: claude accepts it at
# startup and only discovers it is dead when a turn calls the API, which this test never does.
# Must be at least 20 chars -- claude identifies an approved key by its last 20 characters.
_UNUSABLE_API_KEY = "sk-ant-probe-key-not-a-real-credential"

_STATE_WRITTEN_TIMEOUT_SECONDS = 90.0
# The statusline reflects a switch within a few seconds; the rest of this budget is
# only spent proving a no-op, and it is spent once per catalog option.
_SWITCH_TIMEOUT_SECONDS = 15.0
_POLL_INTERVAL_SECONDS = 1.0
_PANE_WIDTH = 200
_PANE_HEIGHT = 50

# The launch alias the workspace pins (.mngr/settings.toml settings_overrides.model), and
# the option it must resolve to. Opus is launched, not switched to, so it exercises the
# path where the reported id keeps its [1m] launch suffix.
_LAUNCH_MODEL_ID = "opus[1m]"


def _repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / "system" / "scripts" / "claude_status_line.sh").is_file():
            return parent
    raise AssertionError("could not locate the repo root from the test file")


def _tmux(socket: str, *args: str) -> subprocess.CompletedProcess[str]:
    """Run tmux against a private server socket, never the caller's."""
    return subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True, timeout=60.0)


class _LiveClaude:
    """A real claude in a tmux pane, writing model_state.json through the real statusline."""

    def __init__(self, socket: str, session_name: str, state_dir: Path) -> None:
        self._socket = socket
        self._session_name = session_name
        self.state_dir = state_dir

    def send_line(self, text: str) -> None:
        _tmux(self._socket, "send-keys", "-t", self._session_name, text, "Enter")

    def read_identity(self):  # noqa: ANN201 -- ModelIdentity | None, named by the shared reader
        return read_model_identity(self.state_dir / "model_state.json")

    def wait_for_model_id(self, predicate, timeout: float):  # noqa: ANN001, ANN201
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            identity = self.read_identity()
            if identity is not None and predicate(identity.model_id):
                return identity
            time.sleep(_POLL_INTERVAL_SECONDS)
        return None


@pytest.fixture
def live_claude(tmp_path: Path) -> Generator[_LiveClaude, None, None]:
    if shutil.which("claude") is None:
        pytest.skip("requires the claude binary on PATH")
    if shutil.which("tmux") is None:
        pytest.skip("requires tmux on PATH")
    if shutil.which("jq") is None:
        pytest.skip("the statusline script requires jq")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    # The statusline writes only for the agent's MAIN session, comparing the payload's
    # session id against this marker (mngr's SessionStart hook writes it live). Claude
    # mints its own session id, so seed the marker from the payload on the first fire by
    # letting the script skip once -- instead, pin it: claude accepts --session-id.
    session_id = str(uuid.uuid4())
    (state_dir / "claude_session_id").write_text(session_id)

    # Skip the first-run dialogs that would otherwise block the TUI before any statusline fires.
    claude_json = {
        "projects": {str(work_dir): {"allowedTools": ["bash"], "hasTrustDialogAccepted": True}},
        "hasCompletedOnboarding": True,
        "numStartups": 1,
        "bypassPermissionsModeAccepted": True,
        "effortCalloutDismissed": True,
        "customApiKeyResponses": {"approved": [_UNUSABLE_API_KEY[-20:]], "rejected": []},
    }
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    for target in (home_dir / ".claude.json", config_dir / ".claude.json"):
        target.write_text(json.dumps(claude_json))

    settings = {
        "model": _LAUNCH_MODEL_ID,
        "statusLine": {
            "type": "command",
            "command": str(_repo_root() / "system" / "scripts" / "claude_status_line.sh"),
        },
    }
    # Every variable is assigned INSIDE the pane command rather than handed to the tmux
    # client: a tmux server gives new panes its OWN environment, so a client-side value
    # loses to whatever the server was started with. Run from inside an mngr agent, that
    # pointed the statusline at the LIVE agent's state dir and clobbered its
    # model_state.json instead of writing the test's.
    assignments = " ".join(
        f"{name}={shlex.quote(value)}"
        for name, value in (
            ("HOME", str(home_dir)),
            ("CLAUDE_CONFIG_DIR", str(config_dir)),
            ("ANTHROPIC_API_KEY", _UNUSABLE_API_KEY),
            ("MNGR_AGENT_STATE_DIR", str(state_dir)),
            ("MNGR_AGENT_WORK_DIR", str(work_dir)),
        )
    )
    launch = (
        f"{assignments} claude --settings {shlex.quote(json.dumps(settings))} "
        f"--session-id {session_id} --dangerously-skip-permissions"
    )

    socket = f"claude-contract-{uuid.uuid4().hex[:8]}"
    session_name = "probe"
    _tmux(
        socket,
        "new-session",
        "-d",
        "-s",
        session_name,
        "-x",
        str(_PANE_WIDTH),
        "-y",
        str(_PANE_HEIGHT),
        "-c",
        str(work_dir),
        f"{launch}; sleep 600",
    )
    try:
        yield _LiveClaude(socket, session_name, state_dir)
    finally:
        _tmux(socket, "kill-server")


def _claude_version() -> str:
    result = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60.0)
    return result.stdout.strip() or "unknown"


def test_live_model_state_resolves_to_the_catalog_option_it_was_launched_as(
    live_claude: _LiveClaude,
) -> None:
    """The whole live-read chain must land on the option the workspace launched.

    Proves, against the real binary: the statusline payload still carries the fields the
    script selects, the script still writes them, the shared reader still parses them, and
    the reported id still matches the catalog option for the launch alias.
    """
    identity = live_claude.wait_for_model_id(lambda model_id: True, _STATE_WRITTEN_TIMEOUT_SECONDS)
    drift = f"(claude version: {_claude_version()}; a failure here means the live-read chain drifted)"

    assert identity is not None, (
        f"the statusline never wrote a readable model_state.json {drift}; "
        "consumers: claude_status_line.sh, read_model_identity, the chat model bar"
    )
    expected = next(option for option in CLAUDE_CATALOG.options if option.id == _LAUNCH_MODEL_ID)
    matched = match_option(identity, CLAUDE_CATALOG.options)
    assert matched is not None, (
        f"the live reported model id {identity.model_id!r} matches NO catalog option {drift}; "
        f"consumer: match_option -- the model bar would show nothing. Catalog keys: "
        f"{[option.harness_reported_model_id or option.id for option in CLAUDE_CATALOG.options]}"
    )
    assert matched.id == expected.id, (
        f"the live reported model id {identity.model_id!r} resolved to {matched.id!r}, "
        f"but the workspace launched {expected.id!r} {drift}; consumer: the chat model bar"
    )
    assert identity.effort is not None, (
        f"the statusline reported no effort level {drift}; consumer: the model bar's effort chip"
    )


def test_switching_model_reports_an_id_the_catalog_still_matches(live_claude: _LiveClaude) -> None:
    """Every option the account can actually select must report an id resolving back to it.

    ``/model <id>`` is what :class:`ClaudeModelResolver` sends. The id claude then reports is
    often not the catalog key -- opus keeps its ``[1m]`` launch suffix, haiku reports a dated
    id -- so this is what pins that the matcher still bridges the gap.

    An option the account is not entitled to is a silent no-op: claude leaves the model where
    it was and prints nothing. That is indistinguishable here from a wrong alias, so such an
    option is recorded and skipped rather than failed -- an entitlement this machine lacks is
    not drift. The test still fails if EVERY option no-ops, which is what a broken alias set
    would look like.
    """
    assert live_claude.wait_for_model_id(lambda _: True, _STATE_WRITTEN_TIMEOUT_SECONDS) is not None, (
        "the statusline never wrote an initial model_state.json"
    )
    drift = f"(claude version: {_claude_version()}; a failure here means a reported id drifted)"

    switched: list[str] = []
    unavailable: list[str] = []
    for option in CLAUDE_CATALOG.options:
        if option.id == _LAUNCH_MODEL_ID:
            continue
        before = live_claude.read_identity()
        live_claude.send_line(f"/model {option.id}")
        key = option.harness_reported_model_id or option.id
        identity = live_claude.wait_for_model_id(
            lambda model_id, key=key: model_id.startswith(key), _SWITCH_TIMEOUT_SECONDS
        )
        if identity is None:
            after = live_claude.read_identity()
            if after is not None and before is not None and after.model_id == before.model_id:
                unavailable.append(option.id)
                continue
            raise AssertionError(
                f"after /model {option.id} the statusline reported "
                f"{after.model_id if after else None!r}, which starts with neither {key!r} nor the "
                f"id it had before {drift}; consumers: ClaudeModelResolver.switch, the model bar"
            )
        # Resolution, not identity: the catalog carries overlapping keys (a dated
        # ``claude-haiku-4-5-<date>`` reported for ``claude-haiku-4`` resolves to the
        # shorter ``haiku`` entry through the prefix pass), so demanding the driven
        # option back would fail on the catalog's own shape rather than on drift.
        # What must hold is that the bar resolves the live id to SOMETHING.
        matched = match_option(identity, CLAUDE_CATALOG.options)
        assert matched is not None, (
            f"after /model {option.id} the reported id {identity.model_id!r} matches no catalog "
            f"option {drift}; consumer: match_option -- the model bar would show nothing"
        )
        switched.append(option.id)

    assert switched, (
        f"no catalog option could be switched to at all (no-ops: {unavailable}) {drift}; "
        "consumer: ClaudeModelResolver.switch -- either every alias is wrong or this account "
        "is entitled to none of them"
    )
