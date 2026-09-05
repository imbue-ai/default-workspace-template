import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.hoisted(() => {
  globalThis.requestAnimationFrame ??= ((cb: FrameRequestCallback): number =>
    setTimeout(() => cb(0), 0) as unknown as number) as typeof globalThis.requestAnimationFrame;
});

import {
  addressFor,
  appNameFromAddress,
  appStoppedDetail,
  applyApps,
  findInstance,
  instancePageUrl,
  isAppStoppable,
  listInstances,
  parseAddress,
  resetInventoryForTesting,
} from "./Inventory";
import type { AppRecord, InstanceRecord } from "./Inventory";

function instance(key: string, title: string, url: string = "/"): InstanceRecord {
  return { key, url, title, status: "idle", lifetime: "explicit", last_active: null, renameable: false };
}

function app(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  return {
    name,
    display_name: name.charAt(0).toUpperCase() + name.slice(1),
    icon: "",
    label: `${name}-1a2b`,
    url: `http://127.0.0.1:9${name.length}00`,
    internal: false,
    program: "",
    critical: false,
    instances_url: "",
    has_instances: false,
    actions: [],
    default_shortcut: null,
    is_running: true,
    instances: [instance("", name)],
    ...overrides,
  };
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
