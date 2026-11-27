// @vitest-environment jsdom
import "../testing/dom";
import { mountView, unmountViews } from "@imbue/workspace-ui/src/testing/mount";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { initEmbedderRelay, resetEmbedderRelayForTesting } from "../relay";
import { FIRST_RUN_COVER_ID, firstRunCoverView } from "./FirstRunCover";

beforeEach(() => {
  resetEmbedderRelayForTesting();
  initEmbedderRelay();
});

afterEach(() => {
  unmountViews();
  resetEmbedderRelayForTesting();
  Object.defineProperty(window, "parent", { value: window, configurable: true });
});

describe("the first run's cover", () => {
  it("is told what the chrome last said it can do when its frame loads", () => {
    const chrome = { postMessage: vi.fn() };
    Object.defineProperty(window, "parent", { value: chrome, configurable: true });
    const said = { type: "minds:embedder-capabilities", canPopOut: false, declinesProviderRelay: true };
    window.dispatchEvent(
      new MessageEvent("message", {
        data: said,
        origin: "https://chrome.example",
        source: chrome as unknown as Window,
      }),
    );
    mountView(() => firstRunCoverView("about:blank"));
    const frame = document.getElementById(FIRST_RUN_COVER_ID) as HTMLIFrameElement;
    const heard = vi.spyOn(frame.contentWindow as Window, "postMessage");

    frame.dispatchEvent(new Event("load"));

    expect(heard).toHaveBeenCalledExactlyOnceWith(said, "*");
  });
});
