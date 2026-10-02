// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";
import { buttonNamed } from "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import type { ChatActionResult } from "../models/summary";
import { LOADING_HISTORY, item, summary } from "../testing/records";
import { ActivityPage } from "./ActivityPage";

const WALLPAPER = item({ item_id: "chat:c1", name: "Wallpaper", chat_id: "c1", rss_kib: 330 * 1024 });

async function settle(): Promise<void> {
  await new Promise((resolve) => setTimeout(resolve, 0));
  m.redraw.sync();
}

describe("the stop dialog", () => {
  afterEach(unmountViews);

  it("asks again, warning of the interruption, when the chat started working since the page read it", async () => {
    const calls: [string, string, boolean][] = [];
    const answers: ChatActionResult[] = [{ kind: "started_working" }, { kind: "done" }];
    const root = mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: summary("COMFORTABLE", [WALLPAPER], null) },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        history: LOADING_HISTORY,
        onChatAction: async (chatId, action, isInterruptConfirmed) => {
          calls.push([chatId, action, isInterruptConfirmed]);
          return answers.shift() ?? { kind: "failed", message: "unexpected" };
        },
      }),
    );

    buttonNamed(root, "Stop").click();
    m.redraw.sync();
    expect(document.body.textContent).not.toContain("started working since you opened this");
    buttonNamed(document.body, "Stop chat").click();
    await settle();

    expect(calls).toEqual([["c1", "stop", false]]);
    expect(document.body.textContent).toContain("It started working since you opened this");
    buttonNamed(document.body, "Stop chat").click();
    await settle();

    expect(calls).toEqual([
      ["c1", "stop", false],
      ["c1", "stop", true],
    ]);
    expect(document.body.textContent).toContain('Stopped "Wallpaper"');
  });

  it("tells the user the figures are old when refreshing has been failing", () => {
    const root = mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: summary("COMFORTABLE", [WALLPAPER], null) },
        refreshFailure: { since: Date.UTC(2026, 9, 1, 12, 0), message: "The page answered 500." },
        nowMs: Date.now(),
        onAskInChat: () => true,
        history: LOADING_HISTORY,
        onChatAction: async (): Promise<ChatActionResult> => ({ kind: "done" }),
      }),
    );
    expect(root.textContent).toContain("Couldn't refresh since");
    expect(root.textContent).toContain("may be out of date");
  });

  it("tells a visitor the app is the owner's", () => {
    const root = mountView(() =>
      m(ActivityPage, {
        state: { kind: "forbidden" },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        history: LOADING_HISTORY,
        onChatAction: async (): Promise<ChatActionResult> => ({ kind: "done" }),
      }),
    );
    expect(root.textContent).toContain("only available to the workspace's owner");
  });
});

describe("the memory bar", () => {
  afterEach(unmountViews);

  function segmentWidths(items: ReturnType<typeof item>[]): number[] {
    const root = mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: summary("COMFORTABLE", items, null) },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        onChatAction: async (): Promise<ChatActionResult> => ({ kind: "done" }),
        history: LOADING_HISTORY,
      }),
    );
    const segments = Array.from(root.querySelectorAll<HTMLElement>(".activity-memory-bar > div:not(.absolute)"));
    return segments.map((segment) => parseFloat(segment.style.width));
  }

  const GIB_KIB = 1024 * 1024;

  it("draws each section at its own size of the limit, leaving out memory no single process holds", () => {
    const widths = segmentWidths([
      item({ item_id: "chat:a", name: "A", rss_kib: GIB_KIB }),
      item({ item_id: "app:b", name: "B", kind: "APP", chat_id: null, rss_kib: GIB_KIB }),
    ]);
    expect(widths.map((width) => width.toFixed(2))).toEqual(["12.50", "12.50"]);
  });

  it("scales the sections down to what is in use when shared memory makes the processes sum past it", () => {
    const widths = segmentWidths([
      item({ item_id: "chat:a", name: "A", rss_kib: 4 * GIB_KIB }),
      item({ item_id: "app:b", name: "B", kind: "APP", chat_id: null, rss_kib: 4 * GIB_KIB }),
    ]);
    expect(widths.reduce((sum, width) => sum + width, 0)).toBeCloseTo(42.5, 5);
  });
});

describe("acting safely", () => {
  afterEach(unmountViews);

  function mountPage(
    loaded: ReturnType<typeof summary>,
    onChatAction: (
      chatId: string,
      action: "stop" | "start",
      isInterruptConfirmed: boolean,
    ) => Promise<ChatActionResult>,
  ): HTMLElement {
    return mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: loaded },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        onChatAction,
        history: LOADING_HISTORY,
      }),
    );
  }

  it("focuses Keep running, so a second Enter on the row's Stop cannot stop the chat", async () => {
    const root = mountPage(summary("COMFORTABLE", [WALLPAPER], null), async () => ({ kind: "done" }));
    buttonNamed(root, "Stop").click();
    m.redraw.sync();
    await new Promise((resolve) => requestAnimationFrame(resolve));
    expect(document.activeElement?.textContent?.trim()).toBe("Keep running");
  });

  it("frees its buttons and says so when the stop itself throws", async () => {
    const root = mountPage(summary("COMFORTABLE", [WALLPAPER], null), async () => {
      throw new Error("network down");
    });
    buttonNamed(root, "Stop").click();
    m.redraw.sync();
    buttonNamed(document.body, "Stop chat").click();
    await settle();
    expect(document.body.textContent).toContain('Couldn\'t stop "Wallpaper": Error: network down');
    expect(buttonNamed(root, "Stop").disabled).toBe(false);
  });

  it("offers no Stop or Start in a preview, and says why", () => {
    const stopped = item({ item_id: "chat:c2", name: "Logo", chat_id: "c2", state: "stopped" });
    const root = mountPage({ ...summary("TIGHT", [WALLPAPER, stopped], null), is_preview: true }, async () => ({
      kind: "done",
    }));
    const labels = Array.from(root.querySelectorAll("button")).map((button) => button.textContent?.trim());
    expect(labels).not.toContain("Stop");
    expect(labels).not.toContain("Start");
    expect(root.textContent).toContain("can't stop anything in it");
  });

  it("opens the background services when the likely first to close is one of them", () => {
    const backup = item({
      item_id: "service:host-backup",
      name: "Backups",
      kind: "SERVICE",
      chat_id: null,
      rss_kib: 900 * 1024,
    });
    const root = mountPage(summary("CRITICAL", [backup], "service:host-backup"), async () => ({ kind: "done" }));
    const row = root.querySelector('[data-item-id="service:host-backup"]');
    expect(row?.textContent).toContain("Likely closed first");
  });
});
