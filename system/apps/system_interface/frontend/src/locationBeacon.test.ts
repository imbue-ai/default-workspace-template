// @vitest-environment jsdom
//
// The beacon boundary is about what a real browser event carries -- an origin,
// a source window, a payload -- so these tests dispatch real MessageEvents at
// a real (jsdom) window with real iframes for the panes, and only the two
// module-state stores the handler consults are stood in for.

import { beforeEach, describe, expect, it, vi } from "vitest";

// The machine as the handler sees it: one registered app (the origin
// derivation reads the registry through getApps) and a spy where a surviving
// beacon lands. Everything else on both modules stays real -- in particular
// labelForService, so the trusted origin below is derived exactly as the
// handler derives it.
const machine = vi.hoisted(() => ({
  apps: [{ name: "docs-viewer", url: "http://127.0.0.1:9000", label: "" }],
  recordMemberLocation: vi.fn(),
}));

vi.mock("./models/AgentManager", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./models/AgentManager")>()),
  getApps: () => machine.apps,
}));

vi.mock("./models/MemberLocations", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./models/MemberLocations")>()),
  recordMemberLocation: machine.recordMemberLocation,
}));

import { initializeLocationBeaconListener } from "./locationBeacon";
import { labelForService } from "./models/AgentManager";
import { deriveServiceOrigin } from "./origin";
import { IFRAME_PANEL_LIVE_KEY_ATTR } from "./views/IframePanel";

const INSTANCE_REF = "service:docs-viewer?instance=docs-viewer-1";

/** The one origin the shell trusts here, derived the way the handler derives
 *  it (labelForService falls back to the bare name for this registry entry,
 *  which is fine: both sides fall back identically). */
const TRUSTED_ORIGIN = deriveServiceOrigin(labelForService("docs-viewer")).replace(/\/$/, "");

// Registered once, as the dock registers it once for the page's lifetime.
initializeLocationBeaconListener();

/** Mount a pane's iframe the way IframePanel renders one: carrying the live
 *  key of the object it shows. Returns its window, the beacon's source. */
function mountPane(liveKey: string): Window {
  const iframe = document.createElement("iframe");
  iframe.setAttribute(IFRAME_PANEL_LIVE_KEY_ATTR, liveKey);
  document.body.appendChild(iframe);
  const paneWindow = iframe.contentWindow;
  if (paneWindow === null) throw new Error("jsdom gave the pane iframe no contentWindow");
  return paneWindow;
}

function postBeacon(data: unknown, origin: string, source: Window | null): void {
  window.dispatchEvent(new MessageEvent("message", { data, origin, source }));
}

beforeEach(() => {
  document.body.innerHTML = "";
  machine.recordMemberLocation.mockClear();
});

describe("the location-beacon listener", () => {
  it("records a framed instance's beacon under the pane's ref", () => {
    const paneWindow = mountPane(INSTANCE_REF);
    postBeacon({ type: "minds-location", path: "/docs/guide" }, TRUSTED_ORIGIN, paneWindow);
    expect(machine.recordMemberLocation).toHaveBeenCalledExactlyOnceWith(INSTANCE_REF, "/docs/guide");
  });

  it("drops a beacon from an origin that is not one of the workspace's own services", () => {
    const paneWindow = mountPane(INSTANCE_REF);
    postBeacon({ type: "minds-location", path: "/docs/guide" }, "https://evil.example", paneWindow);
    expect(machine.recordMemberLocation).not.toHaveBeenCalled();
  });

  it("drops a beacon whose window belongs to no pane this dock renders", () => {
    mountPane(INSTANCE_REF);
    postBeacon({ type: "minds-location", path: "/docs/guide" }, TRUSTED_ORIGIN, null);
    expect(machine.recordMemberLocation).not.toHaveBeenCalled();
  });

  it("drops a beacon from a pane that shows no app instance", () => {
    const paneWindow = mountPane("service:docs-viewer");
    postBeacon({ type: "minds-location", path: "/docs/guide" }, TRUSTED_ORIGIN, paneWindow);
    expect(machine.recordMemberLocation).not.toHaveBeenCalled();
  });

  it("drops payloads that are not a minds-location with a non-empty path", () => {
    const paneWindow = mountPane(INSTANCE_REF);
    postBeacon({ type: "something-else", path: "/docs/guide" }, TRUSTED_ORIGIN, paneWindow);
    postBeacon({ type: "minds-location", path: "" }, TRUSTED_ORIGIN, paneWindow);
    postBeacon({ type: "minds-location", path: 42 }, TRUSTED_ORIGIN, paneWindow);
    postBeacon(null, TRUSTED_ORIGIN, paneWindow);
    expect(machine.recordMemberLocation).not.toHaveBeenCalled();
  });
});
