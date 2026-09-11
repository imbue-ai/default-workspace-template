"""Release test: the API-error text claude actually writes, against a stubbed API.

``session_parser`` classifies a model API failure by reading the TEXT of a synthetic
assistant record (``model == "<synthetic>"``) through :func:`classify_api_error`, whose
patterns key on the literal form ``API Error: <status>``. The frontend styles the message
from that, and adds a "not Minds' fault" note when :func:`is_provider_fault` agrees.

Every other test of those patterns feeds them a hand-written string. This one makes the REAL
binary produce the text, by pointing it at a local stub that answers every request with a
chosen status. Nothing about it needs credentials or costs money -- the stub *is* the API --
but claude retries a failing request for around three minutes before giving up and writing
the record, which is why this is release-marked rather than per-PR.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import uuid
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from pathlib import Path

import pytest

from imbue.mngr.utils.testing import find_free_port
from imbue.system_interface.harnesses.claude.error_patterns import classify_api_error
from imbue.system_interface.harnesses.claude.error_patterns import is_provider_fault

# 529 is the case that actually changes the UI: it is the one status the frontend turns into
# a provider-fault note, so it is the one worth spending the retry window on.
_STUB_STATUS = 529
_EXPECTED_KIND = "overloaded"

# claude retries a failing request with backoff before it writes the synthetic record;
# measured at ~181s against this stub, so the budget is that with room to spare.
_TURN_TIMEOUT_SECONDS = 420.0

_UNUSABLE_API_KEY = "sk-ant-probe-key-not-a-real-credential"


class _StubHandler(BaseHTTPRequestHandler):
    """Answers every request with ``_STUB_STATUS`` and an Anthropic-shaped error body."""

    def do_POST(self) -> None:  # noqa: N802 -- BaseHTTPRequestHandler's naming
        body = json.dumps({"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}).encode()
        self.send_response(_STUB_STATUS)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log."""


@pytest.fixture
def stub_anthropic_api() -> Generator[str, None, None]:
    server = HTTPServer(("127.0.0.1", find_free_port()), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.release
@pytest.mark.timeout(_TURN_TIMEOUT_SECONDS)
def test_a_real_api_failure_is_classified_by_the_error_patterns(stub_anthropic_api: str, tmp_path: Path) -> None:
    """The synthetic record the binary writes on an API failure must still classify.

    Drives the real binary against a stub that only ever fails, then reads the record it
    leaves in its session JSONL and puts that text through the real classifier -- so a
    change to how claude phrases an API error fails here instead of silently downgrading
    every outage to an unstyled assistant message.
    """
    if shutil.which("claude") is None:
        pytest.skip("requires the claude binary on PATH")

    work_dir = tmp_path / "work"
    work_dir.mkdir()
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    # No real credentials: the stub answers in the API's place, so a syntactically valid
    # pre-approved key is enough to get the binary to make (and fail) a request.
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "projects": {str(work_dir): {"hasTrustDialogAccepted": True}},
                "hasCompletedOnboarding": True,
                "numStartups": 1,
                "bypassPermissionsModeAccepted": True,
                "effortCalloutDismissed": True,
                "customApiKeyResponses": {"approved": [_UNUSABLE_API_KEY[-20:]], "rejected": []},
            }
        )
    )

    subprocess.run(
        ["claude", "-p", f"say {uuid.uuid4().hex[:8]}", "--model", "haiku", "--dangerously-skip-permissions"],
        capture_output=True,
        text=True,
        timeout=_TURN_TIMEOUT_SECONDS,
        cwd=str(work_dir),
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:" + str(Path.home() / ".local" / "bin"),
            "HOME": str(tmp_path),
            "CLAUDE_CONFIG_DIR": str(config_dir),
            "ANTHROPIC_API_KEY": _UNUSABLE_API_KEY,
            "ANTHROPIC_BASE_URL": stub_anthropic_api,
        },
        check=False,
    )

    version = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=60.0).stdout.strip()
    drift = f"(claude version: {version}; a failure here means the API-error text drifted)"

    synthetic_texts: list[str] = []
    for session in (config_dir / "projects").glob("*/*.jsonl"):
        for line in session.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("type") != "assistant":
                continue
            message = record.get("message")
            if not isinstance(message, dict) or message.get("model") != "<synthetic>":
                continue
            content = message.get("content")
            if isinstance(content, list):
                synthetic_texts.extend(
                    str(block.get("text", ""))
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )

    assert synthetic_texts, (
        f"a failing API produced no synthetic assistant record {drift}; "
        "consumer: session_parser's api_error_kind, which is gated on model == '<synthetic>'"
    )
    kinds = [classify_api_error(text) for text in synthetic_texts]
    assert _EXPECTED_KIND in kinds, (
        f"the binary's API-error text no longer classifies as {_EXPECTED_KIND!r} {drift}; "
        f"consumer: classify_api_error. Saw {synthetic_texts!r} -> {kinds!r}"
    )
    assert is_provider_fault(_EXPECTED_KIND), (
        f"a {_STUB_STATUS} no longer counts as a provider fault {drift}; "
        "consumer: the frontend's 'not Minds' fault' note"
    )
