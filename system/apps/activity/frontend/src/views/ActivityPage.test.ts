// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";
import { buttonNamed } from "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";

import type { AppStopResult, ChatActionResult } from "../models/summary";
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
        onAppStop: async (): Promise<AppStopResult> => ({ kind: "done" }),
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
        onAppStop: async (): Promise<AppStopResult> => ({ kind: "done" }),
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
        onAppStop: async (): Promise<AppStopResult> => ({ kind: "done" }),
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
        onAppStop: async (): Promise<AppStopResult> => ({ kind: "done" }),
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

describe("stopping an app", () => {
  afterEach(unmountViews);

  const FILES = item({
    item_id: "app:files",
    name: "File Viewer",
    kind: "APP",
    chat_id: null,
    state: "RUNNING",
    app_name: "files",
    is_stoppable: true,
    is_restarted_on_open: true,
    rss_kib: 120 * 1024,
  });
  const BROWSER = item({
    item_id: "app:browser",
    name: "Browser",
    kind: "APP",
    chat_id: null,
    state: "RUNNING",
    app_name: "browser",
    always_on_reason: "Running because agents use it",
    is_restarted_on_open: true,
    rss_kib: 900 * 1024,
  });

  function mountApps(
    items: ReturnType<typeof item>[],
    onAppStop: (appName: string) => Promise<AppStopResult>,
    isPreview: boolean,
  ): HTMLElement {
    const loaded = { ...summary("COMFORTABLE", items, null), is_preview: isPreview };
    return mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: loaded },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        onAppStop,
        onChatAction: async (): Promise<ChatActionResult> => ({ kind: "done" }),
        history: LOADING_HISTORY,
      }),
    );
  }

  function rowButtons(root: ParentNode, itemId: string): string[] {
    const row = root.querySelector(`[data-item-id="${itemId}"]`);
    if (row === null) throw new Error(`no row ${itemId}`);
    return Array.from(row.querySelectorAll("button")).map((button) => button.textContent?.trim() ?? "");
  }

  it("asks first, saying what closes and that it comes back, then quits it through the desktop", async () => {
    const stopped: string[] = [];
    const root = mountApps(
      [FILES, BROWSER],
      async (appName) => {
        stopped.push(appName);
        return { kind: "done" };
      },
      false,
    );
    expect(rowButtons(root, "app:browser")).not.toContain("Stop");
    expect(root.querySelector('[data-item-id="app:browser"]')?.textContent).toContain("Always on");

    expect(rowButtons(root, "app:files")).toContain("Stop");
    const filesRow = root.querySelector('[data-item-id="app:files"]') as HTMLElement;
    buttonNamed(filesRow, "Stop").click();
    m.redraw.sync();
    expect(document.body.textContent).toContain("including those of anyone you've shared it with");
    expect(document.body.textContent).toContain("It starts again the next time it's opened.");
    expect(document.querySelector('[role="dialog"]')?.getAttribute("aria-label")).toBe('Stop "File Viewer"?');
    buttonNamed(document.body, "Stop app").click();
    await settle();

    expect(stopped).toEqual(["files"]);
    expect(document.body.textContent).toContain('Stopped "File Viewer". Freed about 120 MB.');
  });

  it("stops nothing when the user keeps it running, and says why when the desktop refuses", async () => {
    const stopped: string[] = [];
    const root = mountApps(
      [FILES],
      async (appName) => {
        stopped.push(appName);
        return { kind: "failed", message: "the desktop refused to stop the app: busy" };
      },
      false,
    );
    const filesRow = root.querySelector('[data-item-id="app:files"]') as HTMLElement;
    buttonNamed(filesRow, "Stop").click();
    m.redraw.sync();
    buttonNamed(document.body, "Keep running").click();
    m.redraw.sync();
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(stopped).toEqual([]);

    buttonNamed(filesRow, "Stop").click();
    m.redraw.sync();
    buttonNamed(document.body, "Stop app").click();
    await settle();
    expect(stopped).toEqual(["files"]);
    expect(document.body.textContent).toContain(
      'Couldn\'t stop "File Viewer": the desktop refused to stop the app: busy',
    );
  });

  it("does not stop an app that a refresh found already stopped while the dialog was open", async () => {
    const stopped: string[] = [];
    let files = FILES;
    const root = mountView(() =>
      m(ActivityPage, {
        state: { kind: "loaded", summary: summary("COMFORTABLE", [files], null) },
        refreshFailure: null,
        nowMs: Date.now(),
        onAskInChat: () => true,
        onAppStop: async (appName): Promise<AppStopResult> => {
          stopped.push(appName);
          return { kind: "done" };
        },
        onChatAction: async (): Promise<ChatActionResult> => ({ kind: "done" }),
        history: LOADING_HISTORY,
      }),
    );
    buttonNamed(root.querySelector('[data-item-id="app:files"]') as HTMLElement, "Stop").click();
    m.redraw.sync();
    files = { ...FILES, state: "STOPPED" };
    m.redraw.sync();
    buttonNamed(document.body, "Stop app").click();
    await settle();
    expect(stopped).toEqual([]);
    expect(document.body.textContent).toContain('"File Viewer" has already stopped.');
  });

  it("offers no Stop for an app that is not running, nor for anything in a preview", () => {
    const stoppedFiles = { ...FILES, state: "STOPPED" };
    const root = mountApps([stoppedFiles], async () => ({ kind: "done" }), false);
    expect(rowButtons(root, "app:files")).not.toContain("Stop");
    unmountViews();

    const preview = mountApps(
      [FILES, BROWSER, item({ item_id: "chat:c1", name: "Wallpaper", chat_id: "c1" })],
      async () => ({ kind: "done" }),
      true,
    );
    expect(rowButtons(preview, "app:files")).not.toContain("Stop");
    expect(rowButtons(preview, "chat:c1")).not.toContain("Stop");
    expect(preview.textContent).toContain("can't stop anything in it");
    expect(preview.querySelector('[data-item-id="app:browser"]')?.textContent).toContain("Always on");
  });
});
