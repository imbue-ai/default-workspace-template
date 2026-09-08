import "../testing/dom";

import { describe, expect, it } from "vitest";

import { appRecord } from "../testing/records";
import {
  actionKey,
  equalTabWidth,
  isTitleTruncated,
  mostRecentAddressOfApp,
  stoppedPlaceholderForApp,
} from "./DockviewWorkspace";

describe("equalTabWidth", () => {
  it("shares what is left of a strip once the '+' is accounted for", () => {
    expect(equalTabWidth([{ width: 644, tabCount: 4 }])).toBe(150);
  });

  it("takes the narrowest strip's ideal so no strip has to scroll", () => {
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 908, tabCount: 6 },
      ]),
    ).toBe(144);
  });

  it("clamps to the floor and the ceiling", () => {
    expect(equalTabWidth([{ width: 400, tabCount: 12 }])).toBe(140);
    expect(equalTabWidth([{ width: 1200, tabCount: 1 }])).toBe(220);
    expect(equalTabWidth([{ width: 20, tabCount: 1 }])).toBe(140);
  });

  it("ignores strips that hold no tabs, and answers with the ceiling for none at all", () => {
    expect(
      equalTabWidth([
        { width: 644, tabCount: 4 },
        { width: 300, tabCount: 0 },
      ]),
    ).toBe(150);
    expect(equalTabWidth([])).toBe(220);
  });
});

describe("isTitleTruncated", () => {
  it("tolerates a sub-pixel overhang so an exact fit stays crisp", () => {
    expect(isTitleTruncated(80, 140)).toBe(false);
    expect(isTitleTruncated(140.4, 140)).toBe(false);
    expect(isTitleTruncated(260, 140)).toBe(true);
  });
});

describe("mostRecentAddressOfApp", () => {
  const candidates = [
    { address: "app:chat?instance=a", appName: "chat", lastActiveMs: 1_000 },
    { address: "app:chat?instance=b", appName: "chat", lastActiveMs: 5_000 },
    { address: "app:terminal?instance=t", appName: "terminal", lastActiveMs: 9_000 },
  ];

  it("finds nothing to focus in a view with no instance of the app", () => {
    expect(mostRecentAddressOfApp(candidates, "browser", {})).toBeNull();
  });

  it("prefers the tab this client focused most recently, then the app's own recency", () => {
    expect(mostRecentAddressOfApp(candidates, "chat", {})).toBe("app:chat?instance=b");
    expect(mostRecentAddressOfApp(candidates, "chat", { "app:chat?instance=a": 7_000 })).toBe("app:chat?instance=a");
    expect(
      mostRecentAddressOfApp(candidates, "chat", { "app:chat?instance=a": 2_000, "app:chat?instance=b": 3_000 }),
    ).toBe("app:chat?instance=b");
  });

  it("keeps an open tab ahead of an instance that was active more recently but is not open", () => {
    // A restored tab may carry a focus stamp of 0 and still be the one the user has up.
    expect(mostRecentAddressOfApp(candidates, "chat", { "app:chat?instance=a": 0 })).toBe("app:chat?instance=a");
  });

  it("ignores other apps' recency", () => {
    expect(mostRecentAddressOfApp(candidates, "chat", { "app:terminal?instance=t": 99_000 })).toBe(
      "app:chat?instance=b",
    );
  });

  it("takes the first listed when nothing has recency", () => {
    const bare = [
      { address: "app:chat?instance=a", appName: "chat", lastActiveMs: null },
      { address: "app:chat?instance=b", appName: "chat", lastActiveMs: null },
    ];
    expect(mostRecentAddressOfApp(bare, "chat", {})).toBe("app:chat?instance=a");
  });
});

describe("actionKey", () => {
  it("keys an in-flight action by app and action", () => {
    expect(actionKey("chat", "new")).toBe("chat:new");
  });
});

describe("stoppedPlaceholderForApp", () => {
  it("shows nothing in place of a running app's page", () => {
    expect(stoppedPlaceholderForApp(appRecord("docs"))).toBeNull();
  });

  it("names a stopped app and offers Start only where the workspace can start it", () => {
    const supervised = stoppedPlaceholderForApp(appRecord("docs", { is_running: false }));
    expect(supervised?.label).toBe("Docs");
    expect(supervised?.detail).toBe("stopped");
    expect(supervised?.onStart).not.toBeNull();

    const unsupervised = stoppedPlaceholderForApp(appRecord("docs", { is_running: false, program: "" }));
    expect(unsupervised?.detail).toBe("not running (managed outside the workspace)");
    expect(unsupervised?.onStart).toBeNull();

    const critical = stoppedPlaceholderForApp(appRecord("docs", { is_running: false, critical: true }));
    expect(critical?.onStart).toBeNull();
  });
});
