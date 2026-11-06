// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, describe, expect, it, vi } from "vitest";

import { snapshotRecord, storeAppRecord, workspaceAppRecord } from "../testing/records";
import {
  addApp,
  availableApps,
  checkAddedApp,
  fetchTemplateSizeKb,
  getStoreState,
  iconDataUrl,
  installLogUrl,
  isInCategory,
  isRemovable,
  isBusy,
  pollOnce,
  removeAddedApp,
  requestInstall,
  requestUninstall,
  resetStoreStateForTests,
  splitCommand,
  storeAppFor,
  titleOf,
  uninstallingApps,
} from "./Store";

function answer(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

afterEach(() => {
  vi.unstubAllGlobals();
  resetStoreStateForTests();
});

describe("the pure helpers", () => {
  const installed = storeAppRecord("datasette", { status: "INSTALLED" });
  const leaving = storeAppRecord("marimo", { status: "UNINSTALLING" });
  const failed = storeAppRecord("radicale", { status: "FAILED" });
  const fresh = storeAppRecord("nodered");
  const snapshot = snapshotRecord({
    apps: [installed, leaving, failed, fresh],
    workspace_apps: [workspaceAppRecord("chat"), workspaceAppRecord("datasette", { origin: "STORE" })],
  });

  it("offers every app that is not installed or on its way out", () => {
    expect(availableApps(snapshot).map((app) => app.slug)).toEqual(["radicale", "nodered"]);
  });

  it("finds the store's entry behind an app it installed, and none for a built-in", () => {
    expect(storeAppFor(snapshot, snapshot.workspace_apps[1])?.slug).toBe("datasette");
    expect(storeAppFor(snapshot, snapshot.workspace_apps[0])).toBeNull();
  });

  it("keeps showing an app being uninstalled once its window row is gone", () => {
    expect(uninstallingApps(snapshot).map((app) => app.slug)).toEqual(["marimo"]);
  });

  it("names the app a job runs for, and reads busy while one runs", () => {
    expect(titleOf(snapshot, "nodered")).toBe("Nodered");
    expect(titleOf(snapshot, null)).toBeNull();
    expect(isBusy(snapshotRecord({ apps: [fresh] }))).toBe(false);
    expect(isBusy(snapshotRecord({ apps: [storeAppRecord("x", { status: "DOWNLOADING" })] }))).toBe(true);
    expect(isBusy(snapshotRecord({ busy_with: "x" }))).toBe(true);
  });

  it("puts every app in All apps, and an app in each category it is tagged with", () => {
    expect(isInCategory(fresh, null)).toBe(true);
    expect(isInCategory(fresh, "data")).toBe(true);
    expect(isInCategory(fresh, "automation")).toBe(false);
  });

  it("turns registered SVG markup into an image URL and names an app's log", () => {
    expect(iconDataUrl("<svg/>")).toBe("data:image/svg+xml;charset=utf-8,%3Csvg%2F%3E");
    expect(installLogUrl("datasette")).toBe("/api/apps/datasette/log");
  });
});

describe("splitCommand", () => {
  it("splits on spaces, groups quoted words, honours backslashes, and refuses an open quote", () => {
    expect(splitCommand(`run "my app.py" 'a b' c\\ d --x="1 2"`)).toEqual([
      "run",
      "my app.py",
      "a b",
      "c d",
      "--x=1 2",
    ]);
    expect(splitCommand("  ")).toEqual([]);
    expect(splitCommand('say "it\\"s"')).toEqual(["say", 'it"s']);
    expect(splitCommand("'' x")).toEqual(["", "x"]);
    expect(splitCommand('run "open')).toBeNull();
  });
});

describe("the added-app requests", () => {
  it("checks, adds and removes, answering the store's reason when it refuses", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(200, { slug: "demo" })));
    await expect(
      checkAddedApp({
        source: "pypi",
        package: "demo",
        version: "",
        display_name: "",
        command: "x",
        python: "3.12",
        setup_files: [],
      }),
    ).resolves.toEqual({
      kind: "checked",
      result: { slug: "demo" },
    });
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(async () => answer(422, { detail: "No such package." })),
    );
    await expect(
      checkAddedApp({
        source: "pypi",
        package: "x",
        version: "",
        display_name: "",
        command: "x",
        python: "3.12",
        setup_files: [],
      }),
    ).resolves.toEqual({
      kind: "refused",
      reason: "No such package.",
    });
    await expect(
      addApp({
        source: "pypi",
        package: "x",
        version: "",
        display_name: "",
        command: "x",
        python: "3.12",
        setup_files: [],
      }),
    ).resolves.toBe("No such package.");
    await expect(removeAddedApp("x")).resolves.toBe("No such package.");
    const fetchMock = vi.fn().mockImplementation(async () => answer(200, snapshotRecord()));
    vi.stubGlobal("fetch", fetchMock);
    await expect(removeAddedApp("demo")).resolves.toBeNull();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/added-apps/demo");
    expect(fetchMock.mock.calls[0][1].method).toBe("DELETE");
    await expect(
      addApp({
        source: "npm",
        package: "x",
        version: "",
        display_name: "",
        command: "x",
        python: "3.12",
        setup_files: [],
      }),
    ).resolves.toBeNull();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(
      checkAddedApp({
        source: "npm",
        package: "x",
        version: "",
        display_name: "",
        command: "x",
        python: "3.12",
        setup_files: [],
      }),
    ).resolves.toEqual({
      kind: "refused",
      reason: "The App Store could not be reached. Try again in a moment.",
    });
  });

  it("allows removing an added app only once it is not installed", () => {
    expect(isRemovable(storeAppRecord("demo", { is_added: true }))).toBe(true);
    expect(isRemovable(storeAppRecord("demo", { is_added: true, status: "FAILED" }))).toBe(true);
    expect(isRemovable(storeAppRecord("demo", { is_added: true, status: "INSTALLED" }))).toBe(false);
    expect(isRemovable(storeAppRecord("demo", { is_added: false }))).toBe(false);
  });
});

describe("the requests", () => {
  it("queues an install and answers null, or the store's reason when it refuses", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(answer(202, { record: {} }))
      .mockResolvedValue(answer(200, snapshotRecord()));
    vi.stubGlobal("fetch", fetchMock);
    await expect(requestInstall("datasette")).resolves.toBeNull();
    expect(fetchMock.mock.calls[0][0]).toBe("/api/apps/datasette/install");
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");

    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(409, { detail: "It won't fit." })));
    await expect(requestInstall("datasette")).resolves.toBe("It won't fit.");
  });

  it("sends whether to delete the app's files with an uninstall", async () => {
    const fetchMock = vi.fn().mockResolvedValue(answer(202, { record: {} }));
    vi.stubGlobal("fetch", fetchMock);
    await requestUninstall("datasette", true);
    expect(fetchMock.mock.calls[0][0]).toBe("/api/apps/datasette/uninstall");
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ is_deleting_data: true });
  });

  it("answers a plain sentence when the store cannot be reached", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await expect(requestInstall("datasette")).resolves.toBe(
      "The App Store could not be reached. Try again in a moment.",
    );
  });

  it("reads a template's size, or null when it is not estimated", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(200, { size_kb: 2048 })));
    await expect(fetchTemplateSizeKb("digest")).resolves.toBe(2048);
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(404, { detail: "no" })));
    await expect(fetchTemplateSizeKb("digest")).resolves.toBeNull();
  });

  it("holds the polled state, keeping the last good one when a poll fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(500, {})));
    await pollOnce();
    expect(getStoreState()).toEqual({ kind: "failed" });
    const snapshot = snapshotRecord({ busy_with: "x" });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(answer(200, snapshot)));
    await pollOnce();
    expect(getStoreState()).toEqual({ kind: "loaded", snapshot });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("offline")));
    await pollOnce();
    expect(getStoreState()).toEqual({ kind: "loaded", snapshot });
  });
});
