// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { detailOf, listingOf, rowOf, runDetailOf, runOf, scheduleFormOf } from "../testing/records";
import { AppModel, IDLE_REFRESH_MS, RUNNING_REFRESH_MS, type AppHost } from "./App";

const requests: string[] = [];
const bodies: unknown[] = [];
let answer: () => unknown = () => listingOf([]);
/** A refusal scripted for one method: the server's status and detail. */
let refusal: { method: string; status: number; detail: string } | null = null;

function host(): AppHost & { locations: [string, string][]; navigated: string[] } {
  const locations: [string, string][] = [];
  const navigated: string[] = [];
  return {
    locations,
    navigated,
    reportLocation: (path, title) => locations.push([path, title]),
    startWithText: () => undefined,
    confirmRemove: () => Promise.resolve(true),
    navigate: (path) => navigated.push(path),
  };
}

/** A routine's page, loaded: the server answers `detail` for every read until a test says otherwise. */
async function loadedRoutinePage(detail = detailOf(), page: AppHost = host()): Promise<AppModel> {
  answer = () => detail;
  const model = new AppModel("/news-sync", page, () => undefined);
  await model.load();
  return model;
}

/** Let every pending fetch and the continuations after it run: an action, the reload it ends in, and
 * the parsing of a refusal's body each take a few ticks. */
async function settle(): Promise<void> {
  for (let i = 0; i < 20; i += 1) await Promise.resolve();
}

beforeEach(() => {
  vi.useFakeTimers();
  requests.length = 0;
  bodies.length = 0;
  answer = () => listingOf([]);
  refusal = null;
  vi.stubGlobal("fetch", (url: string, init?: RequestInit) => {
    const method = init?.method ?? "GET";
    requests.push(`${method} ${url}`);
    bodies.push(typeof init?.body === "string" ? JSON.parse(init.body) : undefined);
    if (refusal !== null && refusal.method === method) {
      return Promise.resolve(
        new Response(JSON.stringify({ detail: refusal.detail }), {
          status: refusal.status,
          headers: { "Content-Type": "application/json" },
        }),
      );
    }
    const body = answer();
    return Promise.resolve(
      new Response(body === undefined ? null : JSON.stringify(body), {
        status: body === undefined ? 204 : 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("AppModel", () => {
  it("reports the page's location at once and refines the title once the routine is known", async () => {
    answer = () => detailOf({ row: rowOf({ title: "News sync" }) });
    const page = host();
    const model = new AppModel("/news-sync", page, () => undefined);
    expect(page.locations).toEqual([["/news-sync", "news-sync"]]);
    await model.load();
    expect(page.locations[page.locations.length - 1]).toEqual(["/news-sync", "News sync"]);
    expect(model.loaded.kind).toBe("routine");
    model.dispose();
  });

  it("re-reads the list every 30 seconds, every 5 while a run is going, and never under the editor", async () => {
    const model = new AppModel("/", host(), () => undefined);
    await model.load();
    expect(requests).toEqual(["GET /api/routines"]);

    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS - 1);
    expect(requests).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(requests).toHaveLength(2);

    answer = () => listingOf([rowOf({ is_running: true })]);
    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS);
    await settle();
    expect(requests).toHaveLength(3);
    await vi.advanceTimersByTimeAsync(RUNNING_REFRESH_MS);
    await settle();
    expect(requests).toHaveLength(4);

    // An open editor holds the polling; closing it lets the next tick read again.
    model.scheduleForm = scheduleFormOf({ unit: "h", at: "", days: [] });
    await vi.advanceTimersByTimeAsync(RUNNING_REFRESH_MS * 3);
    await settle();
    expect(requests).toHaveLength(4);
    model.scheduleForm = null;
    await vi.advanceTimersByTimeAsync(RUNNING_REFRESH_MS);
    await settle();
    expect(requests).toHaveLength(5);

    model.dispose();
    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS * 2);
    await settle();
    expect(requests).toHaveLength(5);
  });

  it("re-reads a run page every 5 seconds while its run is still going", async () => {
    // The run page reads the run and its routine; the run is answered as still going.
    vi.stubGlobal("fetch", (url: string) => {
      requests.push(`GET ${url}`);
      const body = url.includes("/runs/")
        ? runDetailOf({ run: runOf({ outcome: null, finished_at: null }) })
        : detailOf();
      return Promise.resolve(
        new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    });
    const model = new AppModel("/news-sync/runs/run-1", host(), () => undefined);
    await model.load();
    expect(model.loaded.kind).toBe("run");
    const reads = requests.length;

    await vi.advanceTimersByTimeAsync(RUNNING_REFRESH_MS);
    await settle();
    expect(requests.length).toBeGreaterThan(reads);
    model.dispose();
  });

  it("reads a log page's run, routine and whole log, and re-reads them while the run is still going", async () => {
    const run = runOf({ outcome: null, finished_at: null });
    vi.stubGlobal("fetch", (url: string) => {
      requests.push(`GET ${url}`);
      if (url.endsWith("/log")) return Promise.resolve(new Response("pulling...\n", { status: 200 }));
      const body = url.includes("/runs/") ? runDetailOf({ run }) : detailOf();
      return Promise.resolve(
        new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }),
      );
    });
    const page = host();
    const model = new AppModel("/news-sync/runs/run-1/log", page, () => undefined);
    await model.load();
    expect(model.loaded).toEqual({ kind: "log", run, log: "pulling...\n", routineTitle: "News sync" });
    expect(page.locations[page.locations.length - 1]).toEqual(["/news-sync/runs/run-1/log", "News sync: a run's log"]);
    expect(requests).toEqual([
      "GET /api/routines/news-sync/runs/run-1",
      "GET /api/routines/news-sync",
      "GET /api/routines/news-sync/runs/run-1/log",
    ]);

    await vi.advanceTimersByTimeAsync(RUNNING_REFRESH_MS);
    await settle();
    expect(requests).toHaveLength(6);
    model.dispose();
  });

  it("keeps a failed read on screen with its reason", async () => {
    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ detail: "the ledger is unreadable" }), { status: 500 })),
    );
    const model = new AppModel("/", host(), () => undefined);
    await model.load();
    expect(model.loaded).toEqual({ kind: "error", message: "the ledger is unreadable" });
    model.dispose();
  });

  it("keeps the last good data when a later re-read fails, and says so in a toast", async () => {
    answer = () => listingOf([rowOf()]);
    const model = new AppModel("/", host(), () => undefined);
    await model.load();
    expect(model.loaded.kind).toBe("list");

    vi.stubGlobal("fetch", () =>
      Promise.resolve(new Response(JSON.stringify({ detail: "the ledger is unreadable" }), { status: 500 })),
    );
    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS);
    await settle();

    expect(model.loaded.kind).toBe("list");
    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      "Couldn't refresh: the ledger is unreadable",
    ]);
    model.dispose();
  });

  it("holds the polling while the remove confirmation is up", async () => {
    let answerRemoval: (isConfirmed: boolean) => void = () => undefined;
    const page = {
      ...host(),
      confirmRemove: () =>
        new Promise<boolean>((resolve) => {
          answerRemoval = resolve;
        }),
    };
    const model = await loadedRoutinePage(detailOf(), page);

    model.remove();
    await settle();
    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS * 2);
    await settle();
    expect(requests).toEqual(["GET /api/routines/news-sync"]);

    answerRemoval(false);
    await settle();
    await vi.advanceTimersByTimeAsync(IDLE_REFRESH_MS);
    await settle();
    expect(requests).toHaveLength(2);
    model.dispose();
  });

  it("removes the routine and leaves for the list without reading the removed page", async () => {
    const page = host();
    const model = await loadedRoutinePage(detailOf(), page);
    answer = () => undefined;

    model.remove();
    await settle();

    expect(requests).toEqual(["GET /api/routines/news-sync", "DELETE /api/routines/news-sync"]);
    expect(page.navigated).toEqual(["/"]);
    expect(model.loaded.kind).toBe("routine");
  });

  it("starts a run on request, busy until the page has been read again", async () => {
    const model = await loadedRoutinePage();

    model.runNow();
    expect(model.isBusy).toBe(true);
    await settle();

    expect(requests).toEqual([
      "GET /api/routines/news-sync",
      "POST /api/routines/news-sync/run",
      "GET /api/routines/news-sync",
    ]);
    expect(model.isBusy).toBe(false);
    expect(model.toasts.current()).toEqual([]);
    model.dispose();
  });

  it("switches the routine to the opposite of what the page shows", async () => {
    const model = await loadedRoutinePage(detailOf({ row: rowOf({ is_enabled: true }) }));

    model.toggleEnabled();
    await settle();

    expect(requests[1]).toBe("PUT /api/routines/news-sync/enabled");
    expect(bodies[1]).toEqual({ is_enabled: false });
    model.dispose();
  });

  it("saves the schedule and closes the editor, or keeps the form up with the refusal's reason", async () => {
    const model = await loadedRoutinePage();
    model.scheduleForm = scheduleFormOf({ count: "2", unit: "h", at: "", days: [] });

    model.saveSchedule();
    await settle();

    expect(requests[1]).toBe("PUT /api/routines/news-sync/schedule");
    expect(bodies[1]).toEqual({ every: "2h", at: null, days: [], day_of_month: null, once: null });
    expect(model.scheduleForm).toBeNull();

    refusal = { method: "PUT", status: 400, detail: "a time of day needs a cadence of a day or longer" };
    const form = scheduleFormOf({ count: "15", unit: "m", at: "07:30", days: [] });
    model.scheduleForm = form;
    model.saveSchedule();
    await settle();

    expect(model.scheduleForm).toBe(form);
    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      "Couldn't change the schedule: a time of day needs a cadence of a day or longer",
    ]);
    model.dispose();
  });

  it("opens a fix chat and says so", async () => {
    const model = await loadedRoutinePage();

    model.fix();
    await settle();

    expect(requests[1]).toBe("POST /api/routines/news-sync/fix");
    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      "A chat is opening with everything the agent needs to start.",
    ]);
    model.dispose();
  });

  it("reports a refused action with the server's reason and reads the page again", async () => {
    const model = await loadedRoutinePage();
    refusal = { method: "POST", status: 409, detail: "A run of news-sync is already going." };

    model.runNow();
    await settle();

    expect(model.toasts.current().map((toast) => toast.message)).toEqual([
      "Couldn't start it: A run of news-sync is already going.",
    ]);
    expect(requests[2]).toBe("GET /api/routines/news-sync");
    expect(model.isBusy).toBe(false);
    model.dispose();
  });
});
