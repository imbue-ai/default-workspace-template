// @vitest-environment jsdom
/**
 * The chat root's unread marks: a chat that finished a turn while the root showed another chat
 * wears one until the root shows it, the shown chat never does, a new turn or leaving the list
 * clears it, and the marks live in storage so every root of this browser agrees.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";

const STORAGE_KEY = "chat-root-unread";

type ChatUnread = typeof import("./chatUnread");

/** A fresh copy of the module per test: the marks and the last statuses are module-level. */
async function loadChatUnread(): Promise<ChatUnread> {
  vi.resetModules();
  const chatUnread = await import("./chatUnread");
  chatUnread.initChatUnread();
  return chatUnread;
}

function statuses(entries: Record<string, string>): Map<string, string> {
  return new Map(Object.entries(entries));
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("noteStatuses", () => {
  it("marks a chat whose turn finished while the root showed another chat, until it is read", async () => {
    const { noteStatuses, isUnread, markRead } = await loadChatUnread();

    noteStatuses(statuses({ "agent-a": "working", "agent-b": "idle" }), "agent-b");
    expect(isUnread("agent-a")).toBe(false);

    noteStatuses(statuses({ "agent-a": "idle", "agent-b": "idle" }), "agent-b");
    expect(isUnread("agent-a")).toBe(true);
    expect(isUnread("agent-b")).toBe(false);

    markRead("agent-a");
    expect(isUnread("agent-a")).toBe(false);
  });

  it("never marks the chat the root is showing, and marks nothing without a working turn before", async () => {
    const { noteStatuses, isUnread } = await loadChatUnread();

    noteStatuses(statuses({ "agent-a": "idle", "agent-b": "attention" }), null);
    expect(isUnread("agent-a")).toBe(false);
    expect(isUnread("agent-b")).toBe(false);

    noteStatuses(statuses({ "agent-a": "working" }), "agent-a");
    noteStatuses(statuses({ "agent-a": "idle" }), "agent-a");
    expect(isUnread("agent-a")).toBe(false);
  });

  it("marks the shown chat when the root is hidden, and the mark goes when the root shows it", async () => {
    const { noteStatuses, isUnread } = await loadChatUnread();

    noteStatuses(statuses({ "agent-a": "working" }), null);
    noteStatuses(statuses({ "agent-a": "idle" }), null);
    expect(isUnread("agent-a")).toBe(true);

    noteStatuses(statuses({ "agent-a": "idle" }), "agent-a");
    expect(isUnread("agent-a")).toBe(false);
  });

  it("drops the mark when the chat works again or leaves the list", async () => {
    const { noteStatuses, isUnread } = await loadChatUnread();
    noteStatuses(statuses({ "agent-a": "working", "agent-b": "working" }), null);
    noteStatuses(statuses({ "agent-a": "idle", "agent-b": "idle" }), null);
    expect([isUnread("agent-a"), isUnread("agent-b")]).toEqual([true, true]);

    noteStatuses(statuses({ "agent-a": "working" }), null);

    expect(isUnread("agent-a")).toBe(false);
    expect(isUnread("agent-b")).toBe(false);
  });

  it("keeps the marks in storage, so a reload reads them back and another root's write is followed", async () => {
    const { noteStatuses } = await loadChatUnread();
    noteStatuses(statuses({ "agent-a": "working" }), null);
    noteStatuses(statuses({ "agent-a": "idle" }), null);
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "null")).toEqual(["agent-a"]);

    const reloaded = await loadChatUnread();
    expect(reloaded.isUnread("agent-a")).toBe(true);

    window.localStorage.setItem(STORAGE_KEY, JSON.stringify([]));
    window.dispatchEvent(new StorageEvent("storage", { key: STORAGE_KEY }));
    expect(reloaded.isUnread("agent-a")).toBe(false);
  });
});
