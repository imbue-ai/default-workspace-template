"""End-to-end conformance: the claude statusline writer plus the shared reader.

This is the one test that exercises the real ``system/scripts/claude_status_line.sh`` --
the writer that lives in the dwt tree (there is no test project for ``system/scripts/``) --
against a REAL captured 2.1.207 statusline payload. It runs the script via subprocess with
the payload on stdin, then feeds the file it produced through the shared reader and matcher,
proving the writer's output and the reader agree on the uniform ``model_state.json``
contract. The fixture beside this file is a byte copy of
``docs/system/blueprint/live-model-state/statusline-payload-v2.1.207.json``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from imbue.system_interface.harnesses.claude.model import CLAUDE_CATALOG
from imbue.system_interface.harnesses.model import ModelIdentity
from imbue.system_interface.harnesses.model import match_option
from imbue.system_interface.harnesses.model import read_model_identity

_PAYLOAD_FIXTURE = Path(__file__).parent / "statusline_payload_v2_1_207.json"


def _find_statusline_script() -> Path:
    """The repo's ``system/scripts/claude_status_line.sh``, found by walking up from here."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "system" / "scripts" / "claude_status_line.sh"
        if candidate.is_file():
            return candidate
    raise AssertionError("could not locate system/scripts/claude_status_line.sh from the test file")


def test_statusline_writes_state_the_shared_reader_matches(tmp_path: Path) -> None:
    if shutil.which("jq") is None:
        pytest.skip("jq is required by the statusline script")
    payload = _PAYLOAD_FIXTURE.read_text()
    session_id = json.loads(payload)["session_id"]

    # The script writes only for the agent's MAIN session: its recorded session id must match
    # the payload's, so seed claude_session_id (mngr's SessionStart hook writes it live).
    state_dir = tmp_path
    (state_dir / "claude_session_id").write_text(session_id)

    result = subprocess.run(
        ["bash", str(_find_statusline_script())],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MNGR_AGENT_STATE_DIR": str(state_dir)},
        check=True,
    )
    # The script still prints its status line to stdout.
    assert result.stdout != ""

    written = state_dir / "model_state.json"
    assert written.is_file(), "the statusline script did not write the model-state file"

    identity = read_model_identity(written)
    assert identity is not None
    # The captured payload: model claude-fable-5, effort high, fast off.
    assert identity == ModelIdentity(model_id="claude-fable-5", effort="high", fast=False)

    # The conformance this test proves is that the writer and reader agree on the file.
    # It now also lands on a real catalog option: Fable is offered, and this captured
    # payload is the evidence the statusline reports the SUFFIX-FREE id (claude-fable-5,
    # not claude-fable-5[1m]) even though the entry switches with fable[1m] -- which is
    # exactly why harness_reported_model_id is the suffix-free key.
    matched = match_option(identity, CLAUDE_CATALOG.options)
    assert matched is not None
    assert matched.label == "Fable 5"


def test_statusline_skips_a_nested_session(tmp_path: Path) -> None:
    # A payload whose session id does NOT match the agent's recorded main-session id (a nested
    # interactive claude in the same pane) must not write the file -- it would oscillate it.
    if shutil.which("jq") is None:
        pytest.skip("jq is required by the statusline script")
    payload = _PAYLOAD_FIXTURE.read_text()
    state_dir = tmp_path
    (state_dir / "claude_session_id").write_text("some-other-session-id")

    subprocess.run(
        ["bash", str(_find_statusline_script())],
        input=payload,
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin", "MNGR_AGENT_STATE_DIR": str(state_dir)},
        check=True,
    )
    assert not (state_dir / "model_state.json").exists()
