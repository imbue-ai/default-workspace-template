// @vitest-environment jsdom
import { afterEach, describe, expect, it } from "vitest";

import { closeImageLightbox, openImageLightbox } from "./lightbox";

function downloadButton(): HTMLAnchorElement {
  return document.querySelector('[aria-label="Download image"]') as HTMLAnchorElement;
}

describe("the lightbox's Download", () => {
  afterEach(() => {
    closeImageLightbox();
    document.querySelectorAll("iframe").forEach((frame) => frame.remove());
  });

  it("loads the workspace's own image as a download in a hidden frame and says when it fails", () => {
    openImageLightbox("/home/user/workspace/data/images/chart.png?requested_at=ts-1", "A chart");
    expect(downloadButton().dispatchEvent(new MouseEvent("click", { cancelable: true }))).toBe(false);

    const frame = document.querySelector("iframe[hidden]") as HTMLIFrameElement;
    expect(new URL(frame.src).pathname + new URL(frame.src).search).toBe(
      "/home/user/workspace/data/images/chart.png?requested_at=ts-1&download=",
    );
    // jsdom does not navigate frames, so the frame is handed the page a refusal would have loaded.
    Object.defineProperty(frame, "contentDocument", {
      value: { URL: frame.src, contentType: "text/plain", body: { textContent: "Not authenticated" } },
    });
    frame.dispatchEvent(new Event("load"));
    expect(document.querySelector(".image-lightbox-title")?.textContent).toBe(
      "Couldn't download chart.png: Not authenticated",
    );
    expect(document.querySelector("iframe")).toBeNull();

    downloadButton().click();
    expect(document.querySelector(".image-lightbox-title")?.textContent).toBe("A chart");
  });

  it("opens another site's image in a new tab", () => {
    openImageLightbox("https://example.com/pic.png", "A picture");
    expect(downloadButton().dispatchEvent(new MouseEvent("click", { cancelable: true }))).toBe(true);
    expect(downloadButton().target).toBe("_blank");
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("downloads a data: image through the link, which the browser would not open in a tab", () => {
    openImageLightbox("data:image/png;base64,iVBORw0KGgo=", "Inline");
    expect(downloadButton().dispatchEvent(new MouseEvent("click", { cancelable: true }))).toBe(true);
    expect(downloadButton().hasAttribute("download")).toBe(true);
    expect(document.querySelector("iframe")).toBeNull();
  });

  it("still opens for an image whose address does not parse, offering the link", () => {
    openImageLightbox("http://[broken/pic.png", "Broken");
    expect(downloadButton().hasAttribute("download")).toBe(true);
    expect(document.querySelector("iframe")).toBeNull();
  });
});
