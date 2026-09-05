import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  addressFor,
  appNameFromAddress,
  appStoppedDetail,
  applyApps,
  findInstance,
  instancePageUrl,
  isAddressUnlisted,
  isAppStoppable,
  listInstances,
  parseAddress,
  primaryActionForApp,
  resetInventoryForTesting,
  whenAppsLoaded,
} from "./Inventory";
import type { AppRecord, InstanceRecord } from "./Inventory";
import { appRecord, instanceRecord } from "../testing/records";

function instance(key: string, title: string, url: string = "/"): InstanceRecord {
  return instanceRecord({ key, title, url, lifetime: "explicit", renameable: false });
}

function app(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  return appRecord(name, {
    label: `${name}-1a2b`,
    url: `http://127.0.0.1:9${name.length}00`,
    program: "",
    has_instances: false,
    actions: [],
    instances: [instance("", name)],
    ...overrides,
  });
}

describe("addresses", () => {
  it("round-trips the bare and the keyed form", () => {
    expect(addressFor("files", "")).toBe("app:files");
    expect(addressFor("terminal", "terminal-2")).toBe("app:terminal?instance=terminal-2");
    expect(parseAddress("app:files")).toEqual({ app: "files", key: "" });
    expect(parseAddress("app:terminal?instance=terminal-2")).toEqual({ app: "terminal", key: "terminal-2" });
    expect(appNameFromAddress("app:chat?instance=agent-1.sess")).toBe("chat");
  });

  it("refuses the old spellings and malformed addresses", () => {
    for (const bad of ["chat:agent-1", "service:files", "app:", "app:files?key=1", "app:files?instance=", "files"]) {
      expect(parseAddress(bad), bad).toBeNull();
    }
  });
});

describe("isAddressUnlisted", () => {
  afterEach(() => resetInventoryForTesting());

  it("is decided by the app's own list, never by the seed", () => {
    applyApps([
      app("files"),
      app("terminal", { has_instances: true, is_listed: false, instances: [] }),
      app("browser", { has_instances: true, is_listed: true, instances: [instance("riley", "Riley")] }),
    ]);
    expect(isAddressUnlisted("app:files")).toBe(false);
    expect(isAddressUnlisted("app:terminal?instance=terminal-1")).toBe(false);
    expect(isAddressUnlisted("app:browser?instance=riley")).toBe(false);
    expect(isAddressUnlisted("app:browser?instance=gone")).toBe(true);
    expect(isAddressUnlisted("app:nowhere?instance=x")).toBe(true);
    expect(isAddressUnlisted("not an address")).toBe(true);
  });
});

describe("the inventory", () => {
  beforeEach(() => {
    resetInventoryForTesting();
  });

  afterEach(() => {
    resetInventoryForTesting();
  });

  it("finds instances by address and lists every openable app's instances in order", () => {
    applyApps([
      app("terminal", {
        has_instances: true,
        instances: [instance("terminal-1", "Terminal 1"), instance("terminal-2", "Terminal 2")],
      }),
      app("files"),
      app("owner-exec", { internal: true }),
    ]);
    expect(findInstance("app:terminal?instance=terminal-2")?.instance.title).toBe("Terminal 2");
    expect(findInstance("app:files")?.app.name).toBe("files");
    expect(findInstance("app:terminal")).toBeNull();
    expect(findInstance("app:browser?instance=x")).toBeNull();
    expect(listInstances().map((resolved) => resolved.address)).toEqual([
      "app:terminal?instance=terminal-1",
      "app:terminal?instance=terminal-2",
      "app:files",
    ]);
  });

  it("knows which apps the workspace may stop, and why a stopped one is not answering", () => {
    expect(isAppStoppable(app("files", { program: "files" }))).toBe(true);
    expect(isAppStoppable(app("files"))).toBe(false);
    expect(isAppStoppable(app("chat", { program: "system_interface", critical: true }))).toBe(false);
    // A row running inside a critical app's program cannot be stopped without stopping that app.
    applyApps([app("system_interface", { program: "system_interface", critical: true })]);
    expect(isAppStoppable(app("chat", { program: "system_interface" }))).toBe(false);
    expect(isAppStoppable(app("files", { program: "files" }))).toBe(true);
    expect(appStoppedDetail(app("files", { program: "files" }))).toBe("stopped");
    expect(appStoppedDetail(app("files"))).toContain("managed outside");
  });
});

describe("instancePageUrl", () => {
  it("derives the app's origin from its label on a workspace host and fills the tab in", () => {
    const url = instancePageUrl(
      { name: "terminal", label: "terminal-9c2d", url: "http://127.0.0.1:7681" },
      { url: "/?arg=_&arg=session&arg=terminal-1&arg={tab}" },
      "tab-0123456789abcdef",
      "web-1a2b.host-0f1e2d3c.localhost:8421",
      "https:",
    );
    expect(url).toBe(
      "https://terminal-9c2d.host-0f1e2d3c.localhost:8421/?arg=_&arg=session&arg=terminal-1&arg=tab-0123456789abcdef",
    );
  });

  it("uses the app's registered loopback url on a host with no workspace coordinate", () => {
    const url = instancePageUrl(
      { name: "chat", label: "", url: "http://127.0.0.1:18765" },
      { url: "/agent-1" },
      "tab-0123456789abcdef",
      "127.0.0.1:18765",
      "http:",
    );
    expect(url).toBe("http://127.0.0.1:18765/agent-1");
  });
});

describe("primaryActionForApp", () => {
  function chat(overrides: Partial<AppRecord>): AppRecord {
    return appRecord("chat", {
      url: "http://127.0.0.1:8000",
      program: "",
      critical: true,
      actions: [
        { id: "new", label: "New Chat" },
        { id: "subagent", label: "Open subagent" },
      ],
      ...overrides,
    });
  }

  it("takes the declared default shortcut's action, else the first action, else nothing", () => {
    expect(primaryActionForApp(chat({ default_shortcut: { action: "subagent", mode: "new" } }))?.id).toBe("subagent");
    expect(primaryActionForApp(chat({}))?.id).toBe("new");
    expect(primaryActionForApp(chat({ default_shortcut: { action: "gone", mode: "new" } }))?.id).toBe("new");
    expect(primaryActionForApp(chat({ actions: [] }))).toBeNull();
  });
});

describe("whenAppsLoaded", () => {
  afterEach(() => {
    vi.useRealTimers();
    resetInventoryForTesting();
  });

  it("answers true once a list arrives and false when the timeout passes first", async () => {
    vi.useFakeTimers();
    const pending = whenAppsLoaded(50);
    applyApps([app("files")]);
    await expect(pending).resolves.toBe(true);
    await expect(whenAppsLoaded(50)).resolves.toBe(true);

    resetInventoryForTesting();
    const timedOut = whenAppsLoaded(50);
    vi.advanceTimersByTime(50);
    await expect(timedOut).resolves.toBe(false);
  });
});
