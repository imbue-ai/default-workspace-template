import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TranscriptEvent, UserMessageEvent } from "../models/Response";
import { chatSnapshotFixture } from "../models/chatSnapshotFixture";
import {
  countUserTurns,
  getFastModeNoticeChatId,
  isFastModeLimitReached,
  maybeApplyFastModeLimit,
  resetFastModeLimitForTests,
  wasFastModeLimitApplied,
} from "./fast-mode-limit";
import { getChatFastMode, setFastMode } from "../models/ModelSettings";
import { hasFastModeLimit } from "../models/HarnessCatalog";
import { ensureChatSettings, getChatSettings, updateChatSettings } from "../models/ChatSettings";

// The switch memory is kept in localStorage, which the node test env lacks.
vi.hoisted(() => {
  const store = new Map<string, string>();
  globalThis.localStorage ??= {
    getItem: (key: string) => store.get(key) ?? null,
    setItem: (key: string, value: string) => void store.set(key, value),
    removeItem: (key: string) => void store.delete(key),
    clear: () => store.clear(),
    key: () => null,
    length: 0,
  } as Storage;
});

vi.mock("mithril", () => ({ default: { redraw: vi.fn() } }));
vi.mock("../models/ModelSettings", () => ({ getChatFastMode: vi.fn(), setFastMode: vi.fn() }));
vi.mock("../models/HarnessCatalog", () => ({ hasFastModeLimit: vi.fn() }));
vi.mock("../models/ChatSettings", () => ({
  getChatSettings: vi.fn(),
  ensureChatSettings: vi.fn(),
  updateChatSettings: vi.fn(),
}));

const getChatFastModeMock = vi.mocked(getChatFastMode);
const setFastModeMock = vi.mocked(setFastMode);
const hasFastModeLimitMock = vi.mocked(hasFastModeLimit);
const getChatSettingsMock = vi.mocked(getChatSettings);
const ensureChatSettingsMock = vi.mocked(ensureChatSettings);
const updateChatSettingsMock = vi.mocked(updateChatSettings);

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

  it("is never reached with a limit of zero, fast mode off, or a harness without the limit", () => {
    const chat = chatSnapshotFixture("agent-1");
    expect(isFastModeLimitReached(chat, conversation(9), true, 0)).toBe(false);
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
    ensureChatSettingsMock.mockResolvedValue({ fast_mode_turn_limit: 5, is_fast_mode_notice_shown: false });
    updateChatSettingsMock.mockResolvedValue({ fast_mode_turn_limit: 5, is_fast_mode_notice_shown: true });
  });

  it("loads the settings first and switches nothing until they are known", () => {
    getChatSettingsMock.mockReturnValue(null);
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-1"), conversation(5), true);
    expect(ensureChatSettingsMock).toHaveBeenCalledTimes(1);
    expect(setFastModeMock).not.toHaveBeenCalled();
  });

  it("switches the chat off fast mode once, raises the notice the first time, and records it", () => {
    getChatSettingsMock.mockReturnValue({ fast_mode_turn_limit: 3, is_fast_mode_notice_shown: false });
    const chat = chatSnapshotFixture("agent-1");
    maybeApplyFastModeLimit(chat, conversation(3), true);
    expect(setFastModeMock).toHaveBeenCalledWith("agent-1", false);
    expect(getFastModeNoticeChatId()).toBe("agent-1");
    expect(updateChatSettingsMock).toHaveBeenCalledWith({ fast_mode_turn_limit: 3, is_fast_mode_notice_shown: true });
    expect(wasFastModeLimitApplied("agent-1")).toBe(true);
    // A later render, the user having turned fast mode back on, leaves it alone.
    maybeApplyFastModeLimit(chat, conversation(9), true);
    expect(setFastModeMock).toHaveBeenCalledTimes(1);
  });

  it("remembers the switch across a reload, so a chat turned back on stays fast", () => {
    getChatSettingsMock.mockReturnValue({ fast_mode_turn_limit: 3, is_fast_mode_notice_shown: true });
    const chat = chatSnapshotFixture("agent-3");
    maybeApplyFastModeLimit(chat, conversation(3), true);
    expect(setFastModeMock).toHaveBeenCalledTimes(1);
    // A reload forgets the page's memory but not the browser's.
    expect(localStorage.getItem("chat.fastModeLimitApplied.agent-3")).toBe("1");
    resetFastModeLimitForTests();
    localStorage.setItem("chat.fastModeLimitApplied.agent-3", "1");
    expect(wasFastModeLimitApplied("agent-3")).toBe(true);
    maybeApplyFastModeLimit(chat, conversation(9), true);
    expect(setFastModeMock).toHaveBeenCalledTimes(1);
    localStorage.removeItem("chat.fastModeLimitApplied.agent-3");
  });

  it("raises no notice once the workspace has seen it", () => {
    getChatSettingsMock.mockReturnValue({ fast_mode_turn_limit: 3, is_fast_mode_notice_shown: true });
    maybeApplyFastModeLimit(chatSnapshotFixture("agent-2"), conversation(3), true);
    expect(setFastModeMock).toHaveBeenCalledWith("agent-2", false);
    expect(getFastModeNoticeChatId()).toBeNull();
    expect(updateChatSettingsMock).not.toHaveBeenCalled();
  });
});
