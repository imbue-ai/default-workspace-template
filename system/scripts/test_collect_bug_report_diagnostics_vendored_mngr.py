"""The collector's chat transcripts, fetched through the real vendored ``mngr``.

The unit tests stub mngr, so they can say neither whether the target the collector hands
it is one mngr resolves, nor what mngr makes of a real stream. Inside a workspace
container the host record in the host dir is stamped by the outer provider that built it,
so ``mngr list`` names the host after that record (``workspace-1``) rather than
``localhost``, and a target composed from the listing is only as good as the vendored
mngr's willingness to resolve it.

So this drives the collector's ``list_agents`` -> ``fetch_transcript`` path against the
vendored binary over a host dir shaped like a workspace's: a record naming the host
something other than ``localhost``, and one agent with a real ATIF stream. A change to
mngr's addressing, or to what it makes of the records a harness writes, shows up here
rather than as archives with no ``chats/`` member or with every line's speaker rewritten.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType

import pytest
from imbue.mngr.agents.common_transcript_records import (
    PINNED_ATIF_SCHEMA_VERSION,
    HeaderRecord,
    StepRecord,
    StepSource,
)
from imbue.mngr.cli.testing import (
    create_agent_with_events_dir,
    write_common_transcript_events,
)
from imbue.mngr.interfaces.data_types import CertifiedHostData
from imbue.mngr.primitives import HostId

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COLLECTOR_PATH = Path(__file__).parent / "collect_bug_report_diagnostics.py"


def _load_collector(mngr_binary: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "collect_bug_report_diagnostics_over_mngr", _COLLECTOR_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    namespace = module.__dict__
    assert "MNGR_BINARY" in namespace, (
        "the collector no longer defines MNGR_BINARY; update this test"
    )
    namespace["MNGR_BINARY"] = mngr_binary
    return module


def _atif_stream_records() -> list[dict[str, object]]:
    """A stream as a harness writes one: the header, then who said what.

    Built through mngr's own record models, so a change to the shape the
    emitters must write reaches this fixture rather than leaving it asserting
    against a shape nothing produces any more. The header carries no timestamp,
    and a step's ``source`` is the speaker -- the two properties the collector's
    reader has to survive and preserve.
    """
    header = HeaderRecord(
        type="header",
        event_id="header-" + "0" * 32,
        emitter="claude/common_transcript",
        schema_version=PINNED_ATIF_SCHEMA_VERSION,
    )
    asked = StepRecord(
        type="step",
        event_id="u1-user",
        emitter="claude/common_transcript",
        timestamp="2026-09-14T12:00:00Z",
        source=StepSource.USER,
        message="what did the update change",
    )
    answered = StepRecord(
        type="step",
        event_id="a1-agent",
        emitter="claude/common_transcript",
        timestamp="2026-09-14T12:00:01Z",
        source=StepSource.AGENT,
        message="the launch path",
    )
    return [
        record.model_dump(mode="json", exclude_none=True)
        for record in (header, asked, answered)
    ]


def _workspace_shaped_host_dir(tmp_path: Path) -> Path:
    """A host dir as the outer provider leaves it: its id, and a record naming the host.

    The record is written through mngr's own model, the same shape ``set_certified_data``
    persists, with the name the user gave the workspace rather than ``localhost``.
    """
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    host_id = HostId.generate()
    (host_dir / "host_id").write_text(str(host_id))
    now = datetime.now(timezone.utc)
    record = CertifiedHostData(
        host_id=str(host_id), host_name="workspace-1", created_at=now, updated_at=now
    )
    (host_dir / "data.json").write_text(
        json.dumps(record.model_dump(by_alias=True, mode="json"), indent=2)
    )
    return host_dir


@pytest.mark.timeout(120)
def test_transcripts_are_fetched_through_the_vendored_mngr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_dir = _workspace_shaped_host_dir(tmp_path)
    _agent_id, events_dir = create_agent_with_events_dir(
        host_dir,
        agent_name="chatty",
        events_source="claude/common_transcript",
        agent_type="claude",
    )
    write_common_transcript_events(events_dir, _atif_stream_records())
    mngr_binary = shutil.which("mngr")
    assert mngr_binary is not None and Path(mngr_binary).resolve().is_relative_to(
        _REPO_ROOT
    ), f"the mngr on PATH is not this checkout's vendored one: {mngr_binary}"
    monkeypatch.setenv("MNGR_HOST_DIR", str(host_dir))
    # A temp cwd, so mngr does not read this checkout's own .mngr/settings.toml as project settings.
    monkeypatch.chdir(tmp_path)
    collector = _load_collector(mngr_binary)

    members = collector.collect_transcript_members(60.0)

    assert [name for name, _content, _written_at in members] == [
        "chats/chatty-claude.jsonl"
    ]
    collected = [
        json.loads(line) for line in members[0][1].splitlines() if line.strip()
    ]
    assert [
        record.get("message") for record in collected if record["type"] == "step"
    ] == [
        "what did the update change",
        "the launch path",
    ]
    # Who spoke each line survives collection: it is the field an event-stream
    # read replaces with the path the stream was read from.
    assert [record["source"] for record in collected if record["type"] == "step"] == [
        "user",
        "agent",
    ]
