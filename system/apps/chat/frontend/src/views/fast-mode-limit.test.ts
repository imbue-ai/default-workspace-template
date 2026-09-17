import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TranscriptEvent, UserMessageEvent } from "../models/Response";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import {
  chooseFastMode,
  countUserTurns,
  getFastModeNoticeChatId,
  isFastModeLimitReached,
  maybeApplyFastModeLimit,
  parseFastModeCommand,
  resetFastModeLimitForTests,
} from "./fast-mode-limit";
import { getChatFastMode, setFastMode } from "../models/ModelSettings";
import { hasFastModeLimit } from "../models/HarnessCatalog";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";
import { ensureFastModeState, getFastModeState, updateFastModeState } from "../models/FastMode";

vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
vi.mock("../models/ModelSettings", () => ({ getChatFastMode: vi.fn(), setFastMode: vi.fn() }));
vi.mock("../models/HarnessCatalog", () => ({ hasFastModeLimit: vi.fn() }));
vi.mock("../models/ChatSettings", () => ({
  DEFAULT_CHAT_SETTINGS: { fast_mode_default: "auto", fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false },
  getChatSettings: vi.fn(),
  ensureChatSettings: vi.fn(),
  updateChatSettings: vi.fn(),
}));
vi.mock("../models/FastMode", () => ({
  getFastModeState: vi.fn(),
  ensureFastModeState: vi.fn(),
  updateFastModeState: vi.fn(),
}));

const getChatFastModeMock = vi.mocked(getChatFastMode);
const setFastModeMock = vi.mocked(setFastMode);
const hasFastModeLimitMock = vi.mocked(hasFastModeLimit);
const getChatSettingsMock = vi.mocked(getChatSettings);
const ensureChatSettingsMock = vi.mocked(ensureChatSettings);
const updateChatSettingsMock = vi.mocked(updateChatSettings);
const getFastModeStateMock = vi.mocked(getFastModeState);
const ensureFastModeStateMock = vi.mocked(ensureFastModeState);
const updateFastModeStateMock = vi.mocked(updateFastModeState);

const SETTINGS = { fast_mode_default: "auto" as const, fast_mode_turn_limit: 3, is_fast_mode_notice_shown: false };

function userMsg(content: string, id: string, extra: Partial<UserMessageEvent> = {}): UserMessageEvent {
  return {
    timestamp: id,
    type: "user_message",
    event_id: id,
    source: "test",
    role: "user",
    content,
    ...extra,
  } as UserMessageEvent;
}

function assistantMsg(id: string): TranscriptEvent {
  return {
    timestamp: id,
    type: "assistant_message",
    event_id: id,
    source: "test",
    model: "m",
    text: "ok",
    tool_calls: [],
    stop_reason: null,
    usage: null,
    is_auth_error: false,
    is_api_error: false,
    api_error_kind: null,
    is_provider_fault: false,
  } as TranscriptEvent;
}

/** A conversation of `count` complete exchanges. */
function conversation(count: number): TranscriptEvent[] {
  const events: TranscriptEvent[] = [];
  for (let i = 0; i < count; i++) {
    events.push(userMsg(`question ${i}`, `u-${i}`));
    events.push(assistantMsg(`a-${i}`));
  }
  return events;
}

describe("countUserTurns", () => {
  it("counts the user's own turns and not the seed's, the hidden lines, or the verdicts", () => {
    const events = [
      userMsg("Wait.. what is honest software?", "seed-0", { source: "seed" }),
      assistantMsg("seed-1"),
      ...conversation(2),
      userMsg("/fast off", "hidden", { display: "hidden" }),
      userMsg("Granted (resolution: granted, request_id: r1)", "verdict", {
        display: "permission_resolution",
        resolution: "granted",
        request_id: "r1",
      }),
    ];
    expect(countUserTurns(events)).toBe(2);
  });
});

describe("isFastModeLimitReached", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    hasFastModeLimitMock.mockReturnValue(true);
    getChatFastModeMock.mockReturnValue(true);
  });

  it("is reached once the user has taken the limit's turns with the agent idle and fast on", () => {
    const chat = chatSnapshotFixture("agent-1");
    expect(isFastModeLimitReached(chat, conversation(5), true, 5)).toBe(true);
    expect(isFastModeLimitReached(chat, conversation(4), true, 5)).toBe(false);
    expect(isFastModeLimitReached(chat, conversation(5), false, 5)).toBe(false);
  });

  it("is never reached with fast mode off or a harness without the limit", () => {
    const chat = chatSnapshotFixture("agent-1");
    getChatFastModeMock.mockReturnValue(false);
    expect(isFastModeLimitReached(chat, conversation(9), true, 5)).toBe(false);
    getChatFastModeMock.mockReturnValue(true);
    hasFastModeLimitMock.mockReturnValue(false);
    expect(isFastModeLimitReached(chat, conversation(9), true, 5)).toBe(false);
  });
});

describe("maybeApplyFastModeLimit", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetFastModeLimitForTests();
    hasFastModeLimitMock.mockReturnValue(true);
    getChatFastModeMock.mockReturnValue(true);
    getChatSettingsMock.mockReturnValue(SETTINGS);
    getFastModeStateMock.mockReturnValue({ mode: "auto", is_switched: false });
    ensureChatSettingsMock.mockResolvedValue(SETTINGS);
    ensureFastModeStateMock.mockResolvedValue({ mode: "auto", is_switched: false });
    updateChatSettingsMock.mockResolvedValue({ ...SETTINGS, is_fast_mode_notice_shown: true });
    updateFastModeStateMock.mockResolvedValue({ mode: "auto", is_switched: true });
  });

  it("loads the settings and the chat's mode first and switches nothing until both are known", () => {
    getChatSettingsMock.mockReturnValue(null);
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-1"), conversation(5), true);
    expect(ensureChatSettingsMock).toHaveBeenCalledTimes(1);
    expect(setFastModeMock).not.toHaveBeenCalled();

    getChatSettingsMock.mockReturnValue(SETTINGS);
    getFastModeStateMock.mockReturnValue(null);
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-1"), conversation(5), true);
    expect(ensureFastModeStateMock).toHaveBeenCalledWith("agent-1");
    expect(setFastModeMock).not.toHaveBeenCalled();
  });

  it("switches an auto chat off fast mode once its turns have run, records it, and raises the notice the first time", () => {
    const chat = chatSnapshotFixture("agent-1");
    maybeApplyFastModeLimit(chat, conversation(3), true);
    expect(setFastModeMock).toHaveBeenCalledWith("agent-1", false);
    expect(updateFastModeStateMock).toHaveBeenCalledWith("agent-1", { mode: "auto", is_switched: true });
    expect(getFastModeNoticeChatId()).toBe("agent-1");
    expect(updateChatSettingsMock).toHaveBeenCalledWith({ ...SETTINGS, is_fast_mode_notice_shown: true });
    // The chat remembers the switch, so a later render with fast mode turned back on leaves it alone.
    getFastModeStateMock.mockReturnValue({ mode: "auto", is_switched: true });
    maybeApplyFastModeLimit(chat, conversation(9), true);
    expect(setFastModeMock).toHaveBeenCalledTimes(1);
  });

  it("leaves a chat that is on or off alone however many turns it has taken", () => {
    getFastModeStateMock.mockReturnValue({ mode: "on", is_switched: false });
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-1"), conversation(9), true);
    getFastModeStateMock.mockReturnValue({ mode: "off", is_switched: false });
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-1"), conversation(9), true);
    expect(setFastModeMock).not.toHaveBeenCalled();
    expect(updateFastModeStateMock).not.toHaveBeenCalled();
  });

  it("raises no notice once the workspace has seen it", () => {
    getChatSettingsMock.mockReturnValue({ ...SETTINGS, is_fast_mode_notice_shown: true });
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-2"), conversation(3), true);
    expect(setFastModeMock).toHaveBeenCalledWith("agent-2", false);
    expect(getFastModeNoticeChatId()).toBeNull();
    expect(updateChatSettingsMock).not.toHaveBeenCalled();
  });
});

describe("chooseFastMode", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    getChatSettingsMock.mockReturnValue(SETTINGS);
    updateFastModeStateMock.mockResolvedValue(null);
  });

  it("records the mode on the chat and sets the agent's speed to match", () => {
    chooseFastMode("agent-1", "on", conversation(9));
    expect(updateFastModeStateMock).toHaveBeenLastCalledWith("agent-1", { mode: "on", is_switched: false });
    expect(setFastModeMock).toHaveBeenLastCalledWith("agent-1", true);
    chooseFastMode("agent-1", "off", conversation(0));
    expect(updateFastModeStateMock).toHaveBeenLastCalledWith("agent-1", { mode: "off", is_switched: false });
    expect(setFastModeMock).toHaveBeenLastCalledWith("agent-1", false);
  });

  it("puts a chat on auto fast while its turns are left and already switched once they have run", () => {
    chooseFastMode("agent-1", "auto", conversation(1));
    expect(updateFastModeStateMock).toHaveBeenLastCalledWith("agent-1", { mode: "auto", is_switched: false });
    expect(setFastModeMock).toHaveBeenLastCalledWith("agent-1", true);
    chooseFastMode("agent-1", "auto", conversation(3));
    expect(updateFastModeStateMock).toHaveBeenLastCalledWith("agent-1", { mode: "auto", is_switched: true });
    expect(setFastModeMock).toHaveBeenLastCalledWith("agent-1", false);
  });
});

describe("parseFastModeCommand", () => {
  it("reads /fast on and /fast off, in any case and spacing, and nothing else", () => {
    expect(parseFastModeCommand("/fast on")).toBe("on");
    expect(parseFastModeCommand("  /FAST   Off ")).toBe("off");
    expect(parseFastModeCommand("/fast")).toBeNull();
    expect(parseFastModeCommand("/fast auto")).toBeNull();
    expect(parseFastModeCommand("/fast on please")).toBeNull();
    expect(parseFastModeCommand("fast on")).toBeNull();
  });
});
