// @vitest-environment jsdom
//
// The banner keys entirely off a meta tag the backend injects into the app
// shell, so these tests need a real document to plant that tag in.
import { afterEach, describe, expect, it } from "vitest";

import m from "mithril";

import { STALENESS_MESSAGES, UpdateStalenessBanner, getUpdateStalenessVariant } from "./UpdateStalenessBanner";

const META_TAG_NAME = "system-interface-update-staleness";

function plantMetaTag(content: string): void {
  const tag = document.createElement("meta");
  tag.setAttribute("name", META_TAG_NAME);
  tag.setAttribute("content", content);
  document.head.appendChild(tag);
}

/** Mounts a fresh banner into a detached root, driven with plain `m.render`
 *  (not `m.mount`'s RAF-scheduled auto-redraw) so the dismiss click's state
 *  change lands synchronously on the next explicit redraw. The closure factory
 *  is handed to mithril rather than called here, so the per-instance `hidden`
 *  state is held the same way `App.ts` holds it. */
function mountBanner(): { root: HTMLElement; redraw: () => void } {
  const root = document.createElement("div");
  document.body.appendChild(root);
  const redraw = (): void => {
    m.render(root, m(UpdateStalenessBanner));
  };
  redraw();
  return { root, redraw };
}

function bannerIn(root: HTMLElement): Element | null {
  return root.querySelector(".update-staleness-banner");
}

afterEach(() => {
  document.querySelectorAll(`meta[name="${META_TAG_NAME}"]`).forEach((tag) => tag.remove());
  document.body.innerHTML = "";
});

describe("getUpdateStalenessVariant", () => {
  it("reads the meta tag's content, and reads absence as empty", () => {
    expect(getUpdateStalenessVariant()).toBe("");
    plantMetaTag("update-interrupted");
    expect(getUpdateStalenessVariant()).toBe("update-interrupted");
  });
});

describe("UpdateStalenessBanner", () => {
  it("renders nothing when the shell carries no tag", () => {
    const { root } = mountBanner();
    expect(bannerIn(root)).toBeNull();
  });

  // "toString" is the interesting one: an object-literal lookup would find it
  // on Object.prototype and render "function toString() { [native code] }".
  it.each(["some-future-variant", "toString", "constructor", "hasOwnProperty"])(
    "renders nothing for the unknown variant %s",
    (variant) => {
      // A newer backend's variant must degrade to no banner, not a blank one.
      plantMetaTag(variant);
      const { root } = mountBanner();
      expect(bannerIn(root)).toBeNull();
    },
  );

  it.each([...STALENESS_MESSAGES.keys()])("renders the %s message", (variant) => {
    plantMetaTag(variant);
    const { root } = mountBanner();
    expect(bannerIn(root)?.textContent).toContain(STALENESS_MESSAGES.get(variant));
  });

  it("hides for the rest of the page load once dismissed", () => {
    plantMetaTag("updated-not-activated");
    const { root, redraw } = mountBanner();
    const button = root.querySelector(".update-staleness-banner-btn");
    if (button === null) throw new Error("no dismiss button rendered");
    button.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
    redraw();
    expect(bannerIn(root)).toBeNull();
  });
});
