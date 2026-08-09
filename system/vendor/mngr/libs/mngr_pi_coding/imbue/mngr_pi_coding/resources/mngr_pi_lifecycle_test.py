"""Behavioural tests for the pi lifecycle extension (resources/mngr_pi_lifecycle.ts).

The extension is the crux of the pi port -- it owns the RUNNING/WAITING marker,
the readiness sentinel, conversation-resume bookkeeping, and transcript emission.
It runs inside pi's Node process, so we exercise it the way mngr_antigravity
exercises its shell-script resources: drive the real file with synthetic
lifecycle events (here via Node instead of bash) and assert on the files it
writes. Skipped automatically when Node (with TypeScript support) is unavailable,
e.g. a CI sandbox without it -- the .ts is a resource, not Python, so it does not
count toward coverage.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr.agents.common_transcript_records import validate_common_transcript_record
from imbue.mngr_pi_coding.plugin import _LIFECYCLE_EXTENSION_NAME
from imbue.mngr_pi_coding.plugin import _load_resource

# Node driver: load the extension, register its handlers via a fake `pi`, then
# replay a JSON list of events. Each event is `{event, payload?, sessionId?,
# sessionFile?}`; the fake ctx returns the given session id/file. Assertions
# live in Python (below) against the files the extension writes.
_DRIVER_MJS = """
import { readFileSync } from "node:fs";
const events = JSON.parse(readFileSync(process.argv[2], "utf8"));
const handlers = {};
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default({ on: (name, handler) => { (handlers[name] ||= []).push(handler); } });
for (const ev of events) {
  const ctx = {
    sessionManager: { getSessionId: () => ev.sessionId, getSessionFile: () => ev.sessionFile },
    model: ev.model,
    thinkingLevel: ev.thinkingLevel,
  };
  for (const handler of (handlers[ev.event] || [])) {
    await handler(ev.payload || {}, ctx);
  }
}
"""


def _node_supports_typescript(node: str, work_dir: Path) -> bool:
    """Whether this Node can import a `.ts` module (strip-types, Node >= ~22.6)."""
    probe_ts = work_dir / "probe.ts"
    probe_ts.write_text("export const value: number = 1;\n")
    probe_mjs = work_dir / "probe.mjs"
    probe_mjs.write_text("const m = await import('./probe.ts'); process.exit(m.value === 1 ? 0 : 1);\n")
    result = subprocess.run([node, str(probe_mjs)], capture_output=True, text=True, timeout=30)
    return result.returncode == 0


def _run_extension(
    tmp_path: Path, events: list[dict[str, Any]], *, emit_common: bool = True, emit_usage: bool = False
) -> Path:
    """Run the extension over ``events`` under a fresh state dir; return the state dir."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")

    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver_path = work_dir / "driver.mjs"
    driver_path.write_text(_DRIVER_MJS)
    events_path = work_dir / "events.json"
    events_path.write_text(json.dumps(events))

    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    if emit_usage:
        # The usage writer is gated on this marker (provisioned by mngr_pi_coding_usage).
        (state_dir / "pi_emit_usage").write_text("1")
    result = subprocess.run(
        [node, str(driver_path), str(events_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MNGR_AGENT_STATE_DIR": str(state_dir),
            "MNGR_PI_EMIT_COMMON_TRANSCRIPT": "1" if emit_common else "0",
        },
    )
    assert result.returncode == 0, f"extension driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


_COMMON_TRANSCRIPT = Path("events") / "pi-coding" / "common_transcript" / "events.jsonl"
_RAW_TRANSCRIPT = Path("logs") / "pi-coding_transcript" / "events.jsonl"


def test_session_start_writes_readiness_sentinel(tmp_path: Path) -> None:
    state = _run_extension(tmp_path, [{"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"}])
    assert (state / "pi_session_started").read_text() == "1"


def test_session_file_recorded_on_start_and_switch(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"},
            {"event": "session_switch", "sessionId": "s2", "sessionFile": "/s/s2.jsonl"},
        ],
    )
    assert (state / "pi_session_file").read_text() == "/s/s2.jsonl"


def test_in_memory_session_does_not_clobber_recorded_file(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": "/s/s1.jsonl"},
            # session_switch with no sessionFile models an in-memory session.
            {"event": "session_switch", "sessionId": "mem"},
        ],
    )
    assert (state / "pi_session_file").read_text() == "/s/s1.jsonl"


_MODEL_STATE = Path("minds_model_state.json")


def test_model_state_written_on_session_start(tmp_path: Path) -> None:
    """Pre-turn-1: session_start fires before the first prompt, so the launch model +
    thinking level land on disk immediately for the chat model bar."""
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "session_start",
                "sessionId": "s1",
                "sessionFile": "/s/s1.jsonl",
                "model": {"provider": "anthropic", "id": "claude-opus-4-8"},
                "thinkingLevel": "high",
            }
        ],
    )
    assert json.loads((state / _MODEL_STATE).read_text()) == {
        "model": "anthropic/claude-opus-4-8",
        "effort": "high",
        "fast": False,
    }


def test_model_state_tracks_model_and_thinking_switches(tmp_path: Path) -> None:
    """model_select / thinking_level_select keep the state live. The changed axis comes
    from the event; the untouched one from ctx (which may lag the event)."""
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "session_start",
                "sessionId": "s1",
                "sessionFile": "/s/s1.jsonl",
                "model": {"provider": "anthropic", "id": "claude-opus-4-8"},
                "thinkingLevel": "high",
            },
            # /model switch: event carries the new model, ctx still reports the thinking level.
            {
                "event": "model_select",
                "payload": {"model": {"provider": "openai", "id": "gpt-5.2"}},
                "thinkingLevel": "high",
            },
            # thinking change: event carries the new level, ctx still reports the model.
            {
                "event": "thinking_level_select",
                "payload": {"level": "low"},
                "model": {"provider": "openai", "id": "gpt-5.2"},
            },
        ],
    )
    assert json.loads((state / _MODEL_STATE).read_text()) == {
        "model": "openai/gpt-5.2",
        "effort": "low",
        "fast": False,
    }


def test_marker_set_on_agent_start_and_cleared_on_agent_end(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "agent_end", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()


def test_marker_present_after_agent_start(tmp_path: Path) -> None:
    state = _run_extension(tmp_path, [{"event": "agent_start", "sessionId": "root"}])
    assert (state / "active").exists()


def test_session_shutdown_clears_marker(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "agent_start", "sessionId": "root"},
            {"event": "session_shutdown", "sessionId": "root"},
        ],
    )
    assert not (state / "active").exists()


def test_common_transcript_records_for_each_role(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}},
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                        ],
                        "model": "m",
                        "stopReason": "toolUse",
                        "usage": {"input": 7, "output": 3, "cacheRead": 1, "cacheWrite": 0},
                        "timestamp": 2,
                    }
                },
            },
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "c1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "out"}],
                        "isError": False,
                        "timestamp": 3,
                    }
                },
            },
            # Roles the common schema does not model are skipped.
            {
                "event": "message_end",
                "payload": {"message": {"role": "bashExecution", "command": "x", "timestamp": 4}},
            },
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert [r["type"] for r in records] == ["user_message", "assistant_message", "tool_result"]
    assert records[0]["content"] == "hi"
    assert records[1]["text"] == "ok"
    assert records[1]["tool_calls"] == [
        {"tool_call_id": "c1", "tool_name": "bash", "input_preview": '{"command":"ls"}'}
    ]
    # parts preserve the source order of the text and tool_call blocks.
    assert records[1]["parts"] == [
        {"type": "text", "content": "ok"},
        {"type": "tool_call", "tool_call_id": "c1", "tool_name": "bash", "input_preview": '{"command":"ls"}'},
    ]
    assert records[1]["usage"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_read_tokens": 1,
        "cache_write_tokens": 0,
    }
    assert records[2]["tool_call_id"] == "c1"
    assert records[2]["output"] == "out"
    assert records[2]["is_error"] is False
    assert all(r["source"] == "pi-coding/common_transcript" for r in records)
    assert len({r["event_id"] for r in records}) == 3


def test_emitted_common_records_conform_to_canonical_schema(tmp_path: Path) -> None:
    """Every record the extension emits must validate against the shared envelope schema.

    Guards against the pi emitter (resources/mngr_pi_lifecycle.ts) and the canonical
    schema (imbue.mngr.agents.common_transcript_records) drifting apart -- a divergence
    no other plugin's tests would catch. Drives all three record types from real pi
    message_end payloads and asserts each emitted record conforms.
    """
    state = _run_extension(
        tmp_path,
        [
            {"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}},
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "ok"},
                            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                        ],
                        "model": "m",
                        "stopReason": "toolUse",
                        "usage": {"input": 7, "output": 3, "cacheRead": 1, "cacheWrite": 0},
                        "timestamp": 2,
                    }
                },
            },
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "c1",
                        "toolName": "bash",
                        "content": [{"type": "text", "text": "out"}],
                        "isError": False,
                        "timestamp": 3,
                    }
                },
            },
        ],
    )
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert {r["type"] for r in records} == {"user_message", "assistant_message", "tool_result"}
    for record in records:
        assert validate_common_transcript_record(record) is None, record


def test_raw_transcript_captures_every_message(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [
            {"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}},
            {
                "event": "message_end",
                "payload": {"message": {"role": "bashExecution", "command": "x", "timestamp": 2}},
            },
        ],
    )
    raw = _read_jsonl(state / _RAW_TRANSCRIPT)
    assert len(raw) == 2
    assert raw[0]["message"]["role"] == "user"
    assert raw[1]["message"]["role"] == "bashExecution"


def test_common_transcript_event_ids_stay_unique_across_restart(tmp_path: Path) -> None:
    """A second process (resume) must not reuse event_ids written by the first.

    event_id is seeded from the existing line count, so ids keep climbing across
    a stop/start even though the resumed session reuses its id and only new
    messages fire message_end.
    """
    events = [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}]
    # First run writes one record.
    state = _run_extension(tmp_path, events)
    # Second run against the SAME state dir (simulating a resumed restart).
    # The first run would have skipped the whole test if node were unavailable.
    node = shutil.which("node")
    assert node is not None
    work_dir = tmp_path / "work"
    (work_dir / "events.json").write_text(
        json.dumps(
            [{"event": "message_end", "payload": {"message": {"role": "user", "content": "again", "timestamp": 2}}}]
        )
    )
    result = subprocess.run(
        [node, str(work_dir / "driver.mjs"), str(work_dir / "events.json")],
        capture_output=True,
        text=True,
        timeout=60,
        env={
            "PATH": os.environ.get("PATH", ""),
            "MNGR_AGENT_STATE_DIR": str(state),
            "MNGR_PI_EMIT_COMMON_TRANSCRIPT": "1",
        },
    )
    assert result.returncode == 0, result.stderr
    records = _read_jsonl(state / _COMMON_TRANSCRIPT)
    assert len(records) == 2
    assert len({r["event_id"] for r in records}) == 2


def test_no_common_transcript_when_disabled(tmp_path: Path) -> None:
    state = _run_extension(
        tmp_path,
        [{"event": "message_end", "payload": {"message": {"role": "user", "content": "hi", "timestamp": 1}}}],
        emit_common=False,
    )
    assert not (state / _COMMON_TRANSCRIPT).exists()
    # Raw is still captured (it is not gated).
    assert (state / _RAW_TRANSCRIPT).exists()


def test_unknown_content_and_roles_degrade_gracefully(tmp_path: Path) -> None:
    """Unknown content blocks/roles and malformed messages must not crash the extension.

    The common (lossy) envelope surfaces only what it models; the raw stream
    preserves everything verbatim, so unknown shapes are never lost.
    """
    state = _run_extension(
        tmp_path,
        [
            {
                "event": "message_end",
                "payload": {
                    "message": {
                        "role": "assistant",
                        "model": "m",
                        "stopReason": "toolUse",
                        "timestamp": 1,
                        "content": [
                            {"type": "thinking", "thinking": "secret reasoning"},
                            {"type": "text", "text": "hello"},
                            {"type": "image", "data": "BASE64", "mimeType": "image/png"},
                            {"type": "futureBlockType", "blob": {"nested": True}},
                            {"type": "toolCall", "id": "c1", "name": "bash", "arguments": {"command": "ls"}},
                        ],
                    }
                },
            },
            # Roles the common schema does not model -> skipped from common (kept in raw).
            {
                "event": "message_end",
                "payload": {"message": {"role": "branchSummary", "summary": "x", "timestamp": 2}},
            },
            {
                "event": "message_end",
                "payload": {"message": {"role": "someFutureRole", "whatever": 1, "timestamp": 3}},
            },
            # content that is neither a string nor an array -> coerced to "" (no crash).
            {"event": "message_end", "payload": {"message": {"role": "user", "content": 12345, "timestamp": 4}}},
            # Malformed messages -> skipped entirely.
            {"event": "message_end", "payload": {"message": {"timestamp": 5}}},
            {"event": "message_end", "payload": {"message": None}},
        ],
    )

    common = _read_jsonl(state / _COMMON_TRANSCRIPT)
    # Unknown roles are skipped; only modelled roles surface.
    assert [r["type"] for r in common] == ["assistant_message", "user_message"]
    assistant = next(r for r in common if r["type"] == "assistant_message")
    # Unknown content blocks (thinking/image/future) are dropped; text + tool call survive.
    assert assistant["text"] == "hello"
    assert [c["tool_name"] for c in assistant["tool_calls"]] == ["bash"]
    # Thinking content is not surfaced in the lossy common envelope.
    assert "secret reasoning" not in json.dumps(common)
    user = next(r for r in common if r["type"] == "user_message")
    # Non-string/array content coerces to empty rather than crashing.
    assert user["content"] == ""

    # Raw preserves every well-formed message verbatim -- unknown roles AND unknown blocks.
    raw = _read_jsonl(state / _RAW_TRANSCRIPT)
    assert [r["message"]["role"] for r in raw] == ["assistant", "branchSummary", "someFutureRole", "user"]
    raw_text = json.dumps(raw)
    assert "futureBlockType" in raw_text
    assert "BASE64" in raw_text


# Drives the inbox watcher: a fake pi captures sendUserMessage calls; the inbox is
# pre-seeded with one already-delivered line BEFORE load (archived to
# pi_inbox_history and truncated at load, so it is never re-injected), then
# new/malformed lines are appended and we wait for the poll. The inbox content
# right after load is captured so the truncation itself is observable.
_INBOX_DRIVER_MJS = """
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const injected = [];
const pi = { on: () => {}, sendUserMessage: (c) => injected.push(c) };
writeFileSync(inbox, JSON.stringify("OLD: already delivered") + "\\n");
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default(pi);
const inboxAtLoad = readFileSync(inbox, "utf-8");
appendFileSync(inbox, JSON.stringify("first\\nmultiline") + "\\n");
appendFileSync(inbox, "{not json}\\n");
appendFileSync(inbox, JSON.stringify("second") + "\\n");
await new Promise((r) => setTimeout(r, 600));
writeFileSync(STATE + "/injected.json", JSON.stringify({ injected, inboxAtLoad }));
"""


def test_inbox_watcher_injects_only_new_lines(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "inbox_driver.mjs"
    driver.write_text(_INBOX_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"inbox driver failed:\n{result.stdout}\n{result.stderr}"
    outcome = json.loads((state_dir / "injected.json").read_text())
    # Pre-existing line not re-injected; new lines injected in order with the
    # embedded newline preserved; the malformed line is skipped.
    assert outcome["injected"] == ["first\nmultiline", "second"]
    # The prior generation's line was archived verbatim to pi_inbox_history and the
    # inbox truncated in place at load, so the durable inbox holds only
    # current-generation lines (the offset seed reads 0 from the empty file).
    assert outcome["inboxAtLoad"] == ""
    assert (state_dir / "pi_inbox_history").read_text() == json.dumps("OLD: already delivered") + "\n"


# pi's real sendUserMessage returns a Promise; this fake rejects it. The watcher
# must not let that become an unhandled rejection (which would crash the Node
# process and take pi down with it). The driver fails its top-level `await` only
# if the rejection escapes -- so a returncode of 0 *is* the assertion that the
# rejection was handled. A second, good line proves the poll loop kept running.
_INBOX_REJECTION_DRIVER_MJS = """
import { appendFileSync, writeFileSync } from "node:fs";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const injected = [];
const pi = {
  on: () => {},
  sendUserMessage: (c) => {
    injected.push(c);
    return c === "boom" ? Promise.reject(new Error("rejected inject")) : Promise.resolve();
  },
};
const mod = await import("./mngr_pi_lifecycle.ts");
mod.default(pi);
appendFileSync(inbox, JSON.stringify("boom") + "\\n");
appendFileSync(inbox, JSON.stringify("after") + "\\n");
await new Promise((r) => setTimeout(r, 600));
writeFileSync(STATE + "/injected.json", JSON.stringify(injected));
"""


def test_inbox_watcher_swallows_rejected_inject(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "inbox_rejection_driver.mjs"
    driver.write_text(_INBOX_REJECTION_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, "--unhandled-rejections=throw", str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    # returncode 0 under --unhandled-rejections=throw means the rejection was handled.
    assert result.returncode == 0, f"rejection escaped:\n{result.stdout}\n{result.stderr}"
    injected = json.loads((state_dir / "injected.json").read_text())
    # The rejecting line still advanced the offset (no retry), and the watcher kept
    # running to inject the following line.
    assert injected == ["boom", "after"]


# Drives the interrupt/retract sentinels. A fake ctx exposes isIdle/abort/ui: abort
# APPENDS the parked steers into the editor (as pi does in interactive mode) and records
# an ordered "abort" marker; sendUserMessage records an ordered "inject:<text>" marker.
# session_start is fired so the extension captures the ctx (latestCtx). The scenario JSON
# drives the sequence: an optional pre-load inbox seed, initial idle/draft/parked-steer
# values, then a list of timed actions (append a JSON value / raw text, flip idle, sleep).
# The ordered `log`, the abort count, and the final editor text are written out for assertions.
_SENTINEL_DRIVER_MJS = """
import { appendFileSync, readFileSync, writeFileSync } from "node:fs";
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const inbox = STATE + "/pi_inbox";
const scenario = JSON.parse(readFileSync(process.argv[2], "utf8"));
const log = [];
let editorText = scenario.draft || "";
let idle = scenario.idle === true;
let aborted = 0;
const handlers = {};
const ctx = {
  isIdle: () => idle,
  abort: () => { aborted++; log.push("abort"); editorText += (scenario.parkedSteers || ""); },
  ui: { getEditorText: () => editorText, setEditorText: (t) => { editorText = t; } },
};
const pi = {
  on: (evt, h) => { (handlers[evt] ||= []).push(h); },
  sendUserMessage: (c) => { log.push("inject:" + c); return Promise.resolve(); },
};
if (scenario.preSeed !== undefined) writeFileSync(inbox, JSON.stringify(scenario.preSeed) + "\\n");
mngrPiLifecycle(pi);
for (const h of (handlers["session_start"] || [])) h({}, ctx);
// Drive the timed action list with a recursive setTimeout (a non-unref'd timer keeps the
// process alive so the extension's own inbox poll fires); the driver stays synchronous.
const actions = scenario.actions || [];
let i = 0;
const step = () => {
  if (i >= actions.length) {
    writeFileSync(STATE + "/outcome.json", JSON.stringify({ log, aborted, editorText }));
    return;
  }
  const action = actions[i++];
  if (action.append !== undefined) appendFileSync(inbox, JSON.stringify(action.append) + "\\n");
  if (action.appendRaw !== undefined) appendFileSync(inbox, action.appendRaw);
  if (action.setIdle !== undefined) idle = action.setIdle;
  setTimeout(step, action.sleep || 0);
};
step();
"""

_RETRACT_KEY = {"minds_interrupt_retract": True}
_FLUSH_KEY = {"minds_interrupt": True}


def _run_sentinel_scenario(tmp_path: Path, scenario: dict[str, Any]) -> Path:
    """Run the sentinel driver over ``scenario`` under a fresh state dir; return the state dir."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "sentinel_driver.mjs"
    driver.write_text(_SENTINEL_DRIVER_MJS)
    scenario_path = work_dir / "scenario.json"
    scenario_path.write_text(json.dumps(scenario))
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver), str(scenario_path)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"sentinel driver failed:\n{result.stdout}\n{result.stderr}"
    return state_dir


def test_retract_during_a_turn_aborts_discards_and_restores_draft(tmp_path: Path) -> None:
    """A retract sentinel while a turn runs interrupts it, restores the pre-existing draft, and
    resubmits nothing -- the parked steers are discarded (Minds hands them back to the composer)."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "my draft",
            "parkedSteers": "PARKED STEER",
            "actions": [{"append": _RETRACT_KEY}, {"sleep": 600}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 1
    # Nothing was resubmitted: the only recorded op is the abort.
    assert outcome["log"] == ["abort"]
    # The draft is restored after the abort drained the steers into (and out of) the editor.
    assert outcome["editorText"] == "my draft"


def test_retract_while_idle_no_ops_and_later_strings_still_inject(tmp_path: Path) -> None:
    """A retract with no live turn is a no-op (no abort), and the drain keeps running so a later
    message still injects."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": True,
            "actions": [
                {"append": _RETRACT_KEY},
                {"sleep": 400},
                {"append": "later message"},
                {"sleep": 400},
            ],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 0
    assert outcome["log"] == ["inject:later message"]


def test_pre_existing_retract_line_is_never_processed(tmp_path: Path) -> None:
    """A retract sentinel already on disk at load is archived with the prior generation and never
    interrupts the fresh turn (the generation-reset truncates the inbox at load)."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "d",
            "parkedSteers": "PARKED",
            "preSeed": _RETRACT_KEY,
            "actions": [{"sleep": 600}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 0
    # The pre-existing sentinel was archived verbatim, not processed (driver writes it with
    # JS's compact JSON, so compare against the same separators).
    archived = (state / "pi_inbox_history").read_text()
    assert json.loads(archived) == _RETRACT_KEY
    assert archived == json.dumps(_RETRACT_KEY, separators=(",", ":")) + "\n"


def test_string_then_sentinel_in_one_write_defers_the_sentinel(tmp_path: Path) -> None:
    """A string line and a retract sentinel appended together are handled across two ticks: the
    steer injects first, and only on a later tick (once it has parked) does the abort fire."""
    both = json.dumps("steer msg") + "\n" + json.dumps(_RETRACT_KEY) + "\n"
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "parkedSteers": "PARKED",
            "actions": [{"appendRaw": both}, {"sleep": 800}],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    # The steer was injected before the abort ran, and the sentinel was still processed.
    assert outcome["log"] == ["inject:steer msg", "abort"]
    assert outcome["aborted"] == 1


def test_flush_sentinel_still_resubmits_the_parked_steers(tmp_path: Path) -> None:
    """The flush sibling is unchanged by the shared-core refactor: it interrupts, then resubmits
    the parked steers as one merged turn once the aborted turn settles."""
    state = _run_sentinel_scenario(
        tmp_path,
        {
            "idle": False,
            "draft": "d",
            "parkedSteers": "PARKED",
            "actions": [
                {"append": _FLUSH_KEY},
                {"sleep": 400},
                {"setIdle": True},
                {"sleep": 400},
            ],
        },
    )
    outcome = json.loads((state / "outcome.json").read_text())
    assert outcome["aborted"] == 1
    # Abort first, then the captured steers resubmitted once idle; the draft is restored.
    assert outcome["log"] == ["abort", "inject:PARKED"]
    assert outcome["editorText"] == "d"


_USAGE_EVENTS = Path("events") / "pi-coding" / "usage" / "events.jsonl"


def _assistant_message_end(session_file: str, usage: dict[str, Any]) -> dict[str, Any]:
    return {
        "event": "message_end",
        "sessionId": "s1",
        "sessionFile": session_file,
        "payload": {
            "message": {
                "role": "assistant",
                "content": [],
                "model": "claude-opus-4-8",
                "provider": "anthropic",
                "usage": usage,
            }
        },
    }


def test_usage_writer_emits_cost_snapshot_when_gated(tmp_path: Path) -> None:
    session_file = "/sessions/2056-01-01T00-00-00Z_abc-uuid.jsonl"
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": session_file},
            _assistant_message_end(
                session_file,
                {"input": 2, "output": 7, "cacheRead": 9133, "cacheWrite": 21, "cost": {"total": 0.00488275}},
            ),
        ],
        emit_usage=True,
    )
    records = _read_jsonl(state / _USAGE_EVENTS)
    assert len(records) == 1
    record = records[0]
    assert record["source"] == "pi-coding/usage"
    assert record["type"] == "cost_snapshot"
    # session_id is the session file's basename (timestamp + uuid), stripped of .jsonl.
    assert record["session_id"] == "2056-01-01T00-00-00Z_abc-uuid"
    assert record["cost"] == {"total_cost_usd": 0.00488275}
    assert record["tokens"] == {"input": 2, "output": 7, "cache_read": 9133, "cache_creation": 21}
    assert record["model"] == "anthropic/claude-opus-4-8"
    assert record["cost_mode"] == "API_KEY"


def test_usage_writer_is_inert_without_the_gate_marker(tmp_path: Path) -> None:
    session_file = "/sessions/x.jsonl"
    state = _run_extension(
        tmp_path,
        [
            {"event": "session_start", "sessionId": "s1", "sessionFile": session_file},
            _assistant_message_end(session_file, {"input": 1, "cost": {"total": 0.5}}),
        ],
        emit_usage=False,
    )
    assert not (state / _USAGE_EVENTS).exists()


# Drives the switch-mailbox consumer: a fake pi captures setModel/setThinkingLevel and
# stores handlers so the driver can fire session_start (which the consumer needs to capture a
# ctx for ctx.modelRegistry). The intent is written BEFORE session_start -- modeling a switch
# parked while the agent was stopped -- and must be applied exactly once and the mailbox
# consumed (deleted) afterwards.
_CONTROL_DRIVER_MJS = """
import { existsSync, writeFileSync } from "node:fs";
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const STATE = process.env.MNGR_AGENT_STATE_DIR;
const control = STATE + "/pi_control.json";
const calls = { setModel: [], setThinkingLevel: [] };
const handlers = {};
const targetModel = { provider: "anthropic", id: "claude-opus-4-8", label: "opus" };
const ctx = {
  model: { provider: "anthropic", id: "claude-sonnet-4-5" },
  thinkingLevel: "medium",
  modelRegistry: {
    find: (p, id) => (p === "anthropic" && id === "claude-opus-4-8") ? targetModel : undefined,
    hasConfiguredAuth: () => true,
  },
};
const pi = {
  on: (evt, h) => { handlers[evt] = h; },
  setModel: (m) => { calls.setModel.push({ provider: m.provider, id: m.id }); return Promise.resolve(true); },
  setThinkingLevel: (l) => { calls.setThinkingLevel.push(l); },
};
// Parked intent: written before the extension loads (stopped-agent case). A stale
// pick it replaced is gone by construction -- the mailbox holds one intent.
writeFileSync(control, JSON.stringify({ model_id: "anthropic/claude-opus-4-8", thinking_level: "high" }));
mngrPiLifecycle(pi);
handlers["session_start"]({}, ctx);
setTimeout(() => {
  writeFileSync(STATE + "/switch_calls.json", JSON.stringify({ ...calls, mailboxConsumed: !existsSync(control) }));
}, 600);
"""


def test_control_watcher_applies_model_and_effort_switch(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver = work_dir / "control_driver.mjs"
    driver.write_text(_CONTROL_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    result = subprocess.run(
        [node, str(driver)],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)},
    )
    assert result.returncode == 0, f"control driver failed:\n{result.stdout}\n{result.stderr}"
    calls = json.loads((state_dir / "switch_calls.json").read_text())
    # The parked (pre-session_start) intent resolved via the registry and applied exactly once;
    # the effort passed through; the mailbox was consumed so it cannot re-apply.
    assert calls["setModel"] == [{"provider": "anthropic", "id": "claude-opus-4-8"}]
    assert calls["setThinkingLevel"] == ["high"]
    assert calls["mailboxConsumed"] is True


# Driver for the policy-guard tool_call handler: fire one bash tool_call and report
# the handler's return value plus the (possibly mutated) command, as JSON on stdout.
_GUARD_DRIVER_MJS = """
import mngrPiLifecycle from "./mngr_pi_lifecycle.ts";
const command = process.argv[2];
const handlers = {};
mngrPiLifecycle({ on: (name, handler) => { (handlers[name] ||= []).push(handler); } });
const event = { toolName: "bash", input: { command } };
let result;
for (const handler of (handlers["tool_call"] || [])) { result = handler(event, {}); }
process.stdout.write(JSON.stringify({ result: result ?? null, command: event.input.command }));
"""


def _run_tool_call(tmp_path: Path, command: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Fire one bash ``tool_call`` through the extension; return ``{result, command}``."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available")
    work_dir = tmp_path / "work"
    work_dir.mkdir(exist_ok=True)
    if not _node_supports_typescript(node, work_dir):
        pytest.skip("node does not support importing TypeScript modules")
    (work_dir / _LIFECYCLE_EXTENSION_NAME).write_text(_load_resource(_LIFECYCLE_EXTENSION_NAME))
    driver_path = work_dir / "guard_driver.mjs"
    driver_path.write_text(_GUARD_DRIVER_MJS)
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    full_env = {"PATH": os.environ.get("PATH", ""), "MNGR_AGENT_STATE_DIR": str(state_dir)}
    full_env.update(env or {})
    result = subprocess.run(
        [node, str(driver_path), command], capture_output=True, text=True, timeout=60, env=full_env
    )
    assert result.returncode == 0, f"guard driver failed:\n{result.stdout}\n{result.stderr}"
    return json.loads(result.stdout)


def test_guard_blocks_pipe_to_pager(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "cat f.txt | " + "tail -5")
    assert out["result"] is not None and out["result"]["block"] is True
    assert "tail or head" in out["result"]["reason"]


def test_guard_blocks_git_commit_amend(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "git commit --amend -m x")
    assert out["result"] is not None and out["result"]["block"] is True


def test_guard_blocks_git_rebase(tmp_path: Path) -> None:
    out = _run_tool_call(tmp_path, "git rebase -i HEAD~2")
    assert out["result"] is not None and out["result"]["block"] is True


def test_guard_allows_and_rewrites_a_normal_command(tmp_path: Path) -> None:
    # An allowed command is not blocked, and is rewritten with the oom self-tag.
    out = _run_tool_call(tmp_path, "ls -la")
    assert out["result"] is None
    assert "oom_score_adj" in out["command"]
    assert out["command"].endswith("; ls -la")


def test_guard_rewrites_with_git_identity_when_resolvable(tmp_path: Path) -> None:
    host_dir = tmp_path / "host"
    host_dir.mkdir()
    (host_dir / "data.json").write_text(json.dumps({"host_id": "host-9"}))
    state_dir = tmp_path / "state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "data.json").write_text(json.dumps({"name": "test-agent"}))
    out = _run_tool_call(
        tmp_path,
        "git commit -m hi",
        env={"MNGR_AGENT_ID": "agent-x", "MNGR_HOST_DIR": str(host_dir)},
    )
    assert out["result"] is None
    assert "GIT_AUTHOR_NAME='test-agent'" in out["command"]
    assert "GIT_AUTHOR_EMAIL='agent-x@host-9'" in out["command"]
