// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import m from "mithril";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { DiskResponse, StatsResponse } from "../models/api";
import { EVERY_MS, MonitorModel, SCANNING_EVERY_MS, viewOf, type MonitorHost } from "../models/monitor";
import { GB, diskOf, scanOf, statsOf } from "../testing/records";
import { App } from "./App";

const requests: string[] = [];
const bodies: unknown[] = [];
let stats: () => StatsResponse = () => statsOf();
let disk: () => DiskResponse = () => diskOf();
/** Scripted answers by "METHOD path": a status and a body, in place of the defaults. */
let scripted: Record<string, { status: number; body: unknown }> = {};
let isUnreachable = false;

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

function answer(url: string, method: string): Response {
  const path = url.split("?")[0];
  const script = scripted[`${method} ${path}`];
  if (script !== undefined) return json(script.status, script.body);
  if (path === "/api/stats") return json(200, stats());
  if (path === "/api/disk") return json(200, disk());
  if (path === "/api/disk/scan") return json(202, { scan: scanOf({ state: "scanning" }) });
  if (path === "/api/history") {
    return json(200, { span: "1h", step_seconds: 60, samples: [{ t: Date.now() - 60_000, cpu: 0.9, memory: 0.6 }] });
  }
  if (path === "/api/stop") return json(200, { status: "ok" });
  return json(404, {});
}

/** A host whose page is visible until a test hides it. */
function visibleHost(): MonitorHost & { hide: () => void; show: () => void } {
  let isHidden = false;
  let listener: () => void = () => undefined;
  return {
    isHidden: () => isHidden,
    onVisibilityChange: (next) => (listener = next),
    hide: () => {
      isHidden = true;
      listener();
    },
    show: () => {
      isHidden = false;
      listener();
    },
  };
}

/** Let every pending fetch and the continuations after it run. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
  await vi.advanceTimersByTimeAsync(0);
  m.redraw.sync();
}

async function openPage(
  path: string,
  host: MonitorHost = visibleHost(),
): Promise<{ root: HTMLElement; model: MonitorModel }> {
  const model = new MonitorModel(path, () => m.redraw(), host);
  const root = mountView(() => m(App, { model }));
  model.start();
  await settle();
  return { root, model };
}

function text(root: HTMLElement, selector: string): string {
  return root.querySelector(selector)?.textContent ?? "";
}

function rowTexts(root: HTMLElement): string[] {
  return [...root.querySelectorAll("tbody tr")].map((row) => row.textContent ?? "");
}

beforeEach(() => {
  vi.useFakeTimers();
  requests.length = 0;
  bodies.length = 0;
  stats = () => statsOf();
  disk = () => diskOf();
  scripted = {};
  isUnreachable = false;
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    requests.push(`${method} ${url}`);
    bodies.push(typeof init?.body === "string" ? JSON.parse(init.body) : undefined);
    if (isUnreachable) return Promise.reject(new TypeError("Failed to fetch"));
    return Promise.resolve(answer(url, method));
  });
});

afterEach(() => {
  unmountViews();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("the views a path names", () => {
  it("reads the view from the path, with or without a trailing slash, and the overview for anything else", () => {
    expect(viewOf("/")).toBe("overview");
    expect(viewOf("/cpu/")).toBe("cpu");
    expect(viewOf("/memory")).toBe("memory");
    expect(viewOf("/disk")).toBe("disk");
    expect(viewOf("/elsewhere")).toBe("overview");
  });
});

describe("the overview", () => {
  it("shows a tile per resource and the breakdown with both figures, leaving out empty rows", async () => {
    const { root } = await openPage("/");

    expect(requests).toEqual(["GET /api/stats"]);
    expect(text(root, "h1")).toBe("System Monitor");
    expect(text(root, '[data-tile-value="CPU"]')).toBe("37%");
    expect(text(root, '[data-tile-value="Memory"]')).toBe("50%");
    expect(text(root, '[data-tile-value="Disk"]')).toBe("25%");
    expect([...root.querySelectorAll("a.tile")].map((tile) => tile.getAttribute("href"))).toEqual([
      "/cpu",
      "/memory",
      "/disk",
    ]);
    expect([...root.querySelectorAll("thead th")].map((cell) => cell.textContent)).toEqual(["", "CPU", "Memory", ""]);
    expect(rowTexts(root)).toEqual([
      "Chats 2 running5.0%2.0 GB",
      "Inbox1.0%1.5 GB",
      "Report4.0%512 MB",
      "Workspace20%1.0 GB",
      "Chat app20%1.0 GB",
      "Temporary files of 1.0 GB limit–200 MB",
    ]);
    // Only what can be stopped from here has a Stop button.
    expect([...root.querySelectorAll("button.stop-button")].map((b) => b.getAttribute("aria-label"))).toEqual([
      "Stop Inbox",
      "Stop Report",
    ]);
    expect(root.querySelector('[aria-label="What are temporary files?"]')?.getAttribute("data-hover-tooltip")).toMatch(
      /^Temporary files are created by programs/,
    );
    expect(root.textContent).not.toContain("Memory use is approximate.");
    expect(text(root, "ul.events")).toBe("Nothing in the last 24 hours.");
  });

  it("links each event to its view and says the memory figures are approximate when they are scaled", async () => {
    stats = () =>
      statsOf({
        usage_mode: "scaled",
        events: [
          { at: new Date().toISOString(), kind: "stop", what: "Stopped a chat's agent to free memory", top: [] },
          {
            at: new Date().toISOString(),
            kind: "busy",
            what: "CPU above 90% for 8 minutes",
            top: [{ name: "Inbox", share: 0.7 }],
          },
        ],
      });
    const { root } = await openPage("/");

    expect(root.textContent).toContain("Memory use is approximate.");
    const links = [...root.querySelectorAll("ul.events a")].map((link) => [
      link.textContent,
      link.getAttribute("href"),
    ]);
    expect(links).toEqual([
      ["Details", "/memory"],
      ["Details", "/cpu"],
    ]);
    expect(text(root, "ul.events li:nth-child(2) small")).toBe("Inbox 70%");
  });

  it("says when the workspace stops answering, keeps what it showed, and recovers on the next reading", async () => {
    const { root } = await openPage("/");
    expect(root.querySelector(".offline")).toBeNull();

    isUnreachable = true;
    await vi.advanceTimersByTimeAsync(EVERY_MS);
    await settle();
    expect(text(root, ".offline")).toBe("Can't reach the workspace, retrying");
    expect(text(root, '[data-tile-value="CPU"]')).toBe("37%");

    isUnreachable = false;
    await vi.advanceTimersByTimeAsync(EVERY_MS);
    await settle();
    expect(root.querySelector(".offline")).toBeNull();
  });

  it("asks for nothing while the window is hidden and catches up when it is shown", async () => {
    const host = visibleHost();
    await openPage("/", host);
    host.hide();
    await vi.advanceTimersByTimeAsync(EVERY_MS * 3);
    expect(requests).toEqual(["GET /api/stats"]);

    host.show();
    await settle();
    expect(requests).toEqual(["GET /api/stats", "GET /api/stats"]);
  });
});

describe("stopping something", () => {
  it("asks before it stops, posts the stop once confirmed, and reads again", async () => {
    const { root } = await openPage("/");
    (root.querySelector('button[aria-label="Stop Report"]') as HTMLButtonElement).click();
    m.redraw.sync();

    expect(text(root, ".modal-title")).toBe("Stop Report?");
    expect(text(root, ".modal-message")).toBe("Its next message starts it again.");
    expect(requests).toEqual(["GET /api/stats"]);

    (root.querySelector(".modal-actions .btn--ghost-destructive") as HTMLButtonElement).click();
    await settle();

    expect(requests).toEqual(["GET /api/stats", "POST /api/stop", "GET /api/stats"]);
    expect(bodies[1]).toEqual({ kind: "chat", id: "chat-2" });
    expect(root.querySelector(".modal-card")).toBeNull();
  });

  it("stops nothing when the question is cancelled", async () => {
    const { root } = await openPage("/");
    (root.querySelector('button[aria-label="Stop Inbox"]') as HTMLButtonElement).click();
    m.redraw.sync();
    (root.querySelector(".notice-dismiss") as HTMLButtonElement).click();
    m.redraw.sync();

    expect(root.querySelector(".modal-card")).toBeNull();
    expect(requests).toEqual(["GET /api/stats"]);
  });

  it("shows the refusal's reason until it is dismissed", async () => {
    scripted = {
      "POST /api/stop": { status: 403, body: { detail: "Only the apps you built can be quit from here" } },
    };
    stats = () =>
      statsOf({
        usage: [
          {
            id: "apps",
            label: "Your apps",
            hint: "",
            bytes: GB,
            cpu: 0.1,
            items: [{ name: "News", bytes: GB, cpu: 0.1, stop: { kind: "app", id: "news" } }],
          },
        ],
      });
    const { root } = await openPage("/");
    (root.querySelector('button[aria-label="Quit News"]') as HTMLButtonElement).click();
    m.redraw.sync();
    expect(text(root, ".modal-title")).toBe("Quit News?");

    (root.querySelector(".modal-actions .btn--ghost-destructive") as HTMLButtonElement).click();
    await settle();

    expect(bodies[1]).toEqual({ kind: "app", id: "news" });
    expect(text(root, ".modal-title")).toBe("Couldn't quit News");
    expect(text(root, ".modal-message")).toBe("Only the apps you built can be quit from here");
    (root.querySelector(".notice-dismiss") as HTMLButtonElement).click();
    m.redraw.sync();
    expect(root.querySelector(".modal-card")).toBeNull();
  });
});

describe("the CPU view", () => {
  it("shows the share of the cores, the breakdown by share, and only the busy stretches", async () => {
    stats = () =>
      statsOf({
        events: [
          { at: new Date().toISOString(), kind: "stop", what: "Stopped a chat's agent", top: [] },
          { at: new Date().toISOString(), kind: "busy", what: "CPU above 90% for 3 minutes", top: [] },
        ],
      });
    const { root } = await openPage("/cpu");

    expect(text(root, "h1")).toBe("CPU");
    expect(root.querySelector('a[aria-label="Back to the overview"]')?.getAttribute("href")).toBe("/");
    expect(text(root, ".big-figure")).toBe("37% of 4 cores busy");
    expect(root.textContent).toContain("150 processes");
    expect(rowTexts(root)).toEqual(["Workspace20%", "Chat app20%", "Chats 2 running5.0%", "Report4.0%", "Inbox1.0%"]);
    expect(text(root, "ul.events")).toBe(
      `${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}CPU above 90% for 3 minutes`,
    );
    expect(root.querySelector("ul.events a")).toBeNull();
  });

  it("switches the graph to a longer span read from the history route, and back", async () => {
    const { root, model } = await openPage("/cpu");
    const line = (): string => root.querySelector(".spark-line")?.getAttribute("d") ?? "";
    const tenMinutes = line();
    expect(tenMinutes).not.toBe("");
    expect(root.querySelector('[role="radio"][aria-checked="true"]')?.textContent).toBe("10 min");

    (root.querySelector('[data-segment="1h"]') as HTMLButtonElement).click();
    await settle();

    expect(requests).toContain("GET /api/history?span=1h");
    expect(model.spans.cpu).toBe("1h");
    expect(root.querySelector('[role="radio"][aria-checked="true"]')?.textContent).toBe("1 h");
    expect(line()).not.toBe(tenMinutes);

    (root.querySelector('[data-segment="10m"]') as HTMLButtonElement).click();
    await settle();
    expect(line()).toBe(tenMinutes);
    expect(requests.filter((request) => request.startsWith("GET /api/history"))).toHaveLength(1);
  });

  it("says when there are no readings, and dashes without a CPU reading", async () => {
    stats = () => statsOf({ history: [], cpu: { usage: 0, cores: 1, processes: 1, sampled_at: null } });
    const { root } = await openPage("/cpu");

    expect(text(root, ".no-readings")).toBe("No readings yet");
    expect(text(root, ".big-figure")).toBe("-- no reading yet");
    expect(root.textContent).toContain("the sampler has not reported");
    expect(root.textContent).toContain("1 process");
  });
});

describe("the memory view", () => {
  it("warns from 80% with what the user can free, and draws the guard's line", async () => {
    stats = () => statsOf({ memory: { total: 8 * GB, available: GB, used: 7 * GB } });
    const { root } = await openPage("/memory");

    expect(text(root, ".big-figure")).toBe("7.0 GB of 8.0 GB used");
    expect(text(root, ".memory-note")).toBe(
      "Memory is 88% full. Above 90%, the workspace stops programs to free memory. Chats are using the most memory. " +
        "Stopping chats you aren't using frees memory.",
    );
    expect(root.querySelector(".guard-meter [data-meter-fill]")?.className).toContain("bg-warn-surface");
    expect(root.querySelector(".guard-line")).not.toBeNull();
  });

  it("lists what the guard stopped, with each command behind Details that a refresh keeps open", async () => {
    const at = new Date().toISOString();
    stats = () =>
      statsOf({
        stops: [
          { at, what: "a code checker of Inbox", command: "node tsserver.js" },
          { at: new Date(Date.now() - 60_000).toISOString(), what: "a chat's agent", command: null },
        ],
      });
    const { root } = await openPage("/memory");
    const toggle = (): HTMLButtonElement => root.querySelector("ul.stops button") as HTMLButtonElement;

    expect([...root.querySelectorAll("ul.stops li > span")].map((what) => what.textContent)).toEqual([
      "A code checker of Inbox",
      "A chat's agent",
    ]);
    expect(root.querySelectorAll("ul.stops button")).toHaveLength(1);
    expect((root.querySelector(".stop-details") as HTMLElement).hidden).toBe(true);

    toggle().click();
    m.redraw.sync();
    expect(toggle().textContent).toBe("Hide details");
    expect(toggle().getAttribute("aria-expanded")).toBe("true");
    expect((root.querySelector(".stop-details") as HTMLElement).hidden).toBe(false);
    expect(text(root, ".stop-details pre")).toBe("node tsserver.js");

    await vi.advanceTimersByTimeAsync(EVERY_MS);
    await settle();
    expect((root.querySelector(".stop-details") as HTMLElement).hidden).toBe(false);
  });
});

describe("the disk view", () => {
  it("polls its own route and shows the figures and the backups", async () => {
    disk = () => diskOf({ backups: { ...diskOf().backups, failures_last_day: 1 } });
    const { root } = await openPage("/disk");

    expect(requests).toEqual(["GET /api/disk"]);
    expect(text(root, ".big-figure")).toBe("16 GB of 64 GB used");
    expect(text(root, ".backup")).toContain("Backed up 30 min ago. Up to date.");
    expect(text(root, ".backup")).toContain(
      "The whole workspace is backed up every 6 hours, encrypted. 1 backup failed in the last 24 hours.",
    );
    expect(root.querySelector(".backup a")?.getAttribute("href")).toBe(
      "file:///home/user/workspace/system/services/host_backup/README.md",
    );
    expect(root.querySelector(".disk-table")).toBeNull();
  });

  it("starts a walk when asked, reads faster while it runs, and shows its rows when it is done", async () => {
    const { root } = await openPage("/disk");
    const button = (): HTMLButtonElement => root.querySelector(".scan-button") as HTMLButtonElement;
    expect(button().textContent).toBe("See what's using the disk");

    disk = () => diskOf({ scan: scanOf({ state: "scanning" }) });
    button().click();
    await settle();
    expect(requests).toEqual(["GET /api/disk", "POST /api/disk/scan", "GET /api/disk"]);
    expect(button().textContent).toBe("Measuring…");
    expect(button().disabled).toBe(true);
    expect(text(root, ".scan-note")).toBe("Measuring what is on the disk…");

    disk = () =>
      diskOf({
        scan: scanOf({
          state: "done",
          scanned_at: new Date().toISOString(),
          rows: [
            {
              id: "workspace",
              label: "Workspace files",
              hint: "",
              bytes: 6 * GB,
              items: [{ name: "data", hint: "your files", bytes: 4 * GB }],
            },
            { id: "other", label: "Everything else", hint: "", bytes: 10 * GB, items: [] },
          ],
          missing: ["caches"],
        }),
      });
    await vi.advanceTimersByTimeAsync(SCANNING_EVERY_MS);
    await settle();

    expect(requests).toEqual(["GET /api/disk", "POST /api/disk/scan", "GET /api/disk", "GET /api/disk"]);
    expect(button().textContent).toBe("Measure again");
    expect(button().disabled).toBe(false);
    expect(rowTexts(root)).toEqual(["Workspace files6.0 GB", "data your files4.0 GB", "Everything else10 GB"]);
    expect(text(root, ".scan-missing")).toBe("Could not measure: caches.");
  });
});
