// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import * as embedContract from "@minds/embed-contract";
import { resetEmbedEndpointForTesting } from "@imbue/workspace-ui/src/embed";
import { FakeDesktopApi, FakeDesktopSocket } from "../testing/fakeShell";
import { themeMetricsRecord } from "../testing/records";
import { DesktopStore } from "./DesktopStore";
import { connectEmbedderToStore, immersiveBridge } from "./embedderBridge";

/** What the fake embedder says: a message from this page's parent, which at the top level is the page itself. */
function embedderSays(data: Record<string, unknown>): void {
  window.dispatchEvent(new MessageEvent("message", { data, source: window }));
}

/** Everything the page posted to its embedder (its parent, itself here) until the queue drains. */
async function postedToEmbedder(): Promise<unknown[]> {
  await vi.waitFor(() => expect(posted.length).toBeGreaterThan(0));
  return posted;
}

let posted: unknown[];
let store: DesktopStore;
const record = (event: MessageEvent): void => {
  if (event.source !== window) posted.push(event.data);
};

beforeEach(() => {
  resetEmbedEndpointForTesting();
  posted = [];
  store = new DesktopStore({
    clientId: "client-1",
    api: new FakeDesktopApi(),
    socket: new FakeDesktopSocket(),
    metrics: themeMetricsRecord(),
    modes: { isPhone: false, isTouch: false },
    redraw: () => undefined,
    reloadInterface: () => undefined,
    immersive: immersiveBridge,
  });
  connectEmbedderToStore(store);
  window.addEventListener("message", record);
});

afterEach(() => {
  window.removeEventListener("message", record);
  resetEmbedEndpointForTesting();
});

describe("connectEmbedderToStore", () => {
  it("takes the chrome's capabilities, immersive included, and ignores a chrome that names none", () => {
    embedderSays({ type: "minds:embedder-capabilities", canPopOut: true, immersive: true });
    expect(store.getCanPopOut()).toBe(true);
    expect(store.getCanImmerse()).toBe(true);
    embedderSays({ type: "minds:embedder-capabilities", canPopOut: false });
    expect(store.getCanPopOut()).toBe(false);
    expect(store.getCanImmerse()).toBe(false);
  });

  // The contract validates every message before a handler runs, and one that predates the immersive pair (the pinned
  // mngr's, until it carries the Screen integration) drops the change as an unknown type.
  it.skipIf(!("IMMERSIVE_CHANGED" in embedContract))(
    "lands the chrome's immersive changes in the store, an error as a notice, and drops a malformed one",
    () => {
      embedderSays({ type: "minds:immersive-changed", on: true, keyboardLocked: true });
      expect(store.getImmersive()).toEqual({ windowId: null, isKeyboardLocked: true });
      embedderSays({ type: "minds:immersive-changed", on: false, keyboardLocked: false, error: "fullscreen refused" });
      expect(store.getImmersive()).toBeNull();
      expect(store.toasts.current().map((toast) => toast.message)).toEqual([
        "Immersive mode could not start: fullscreen refused",
      ]);
      // The contract's validator drops a change with no lock word before any handler runs.
      embedderSays({ type: "minds:immersive-changed", on: true });
      expect(store.getImmersive()).toBeNull();
    },
  );

  it("the bridge posts the immersive request as the contract spells it", async () => {
    immersiveBridge.requestImmersive("win-0123456789abcdef", true);
    expect(await postedToEmbedder()).toEqual([
      { type: "minds:immersive-request", windowId: "win-0123456789abcdef", on: true },
    ]);
  });
});
