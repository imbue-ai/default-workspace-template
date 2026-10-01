// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

// lightbox touches the DOM imperatively; markdown.ts only needs its export to exist.
vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

import { renderMarkdown, requestedAtUrl } from "./markdown";

describe("requestedAtUrl", () => {
  it("appends the per-message post time to an absolute on-disk path", () => {
    expect(requestedAtUrl("/home/user/workspace/data/images/chart.png", "2026-07-24T00:00:00Z")).toBe(
      "/home/user/workspace/data/images/chart.png?requested_at=2026-07-24T00%3A00%3A00Z",
    );
  });

  it("works for download-link paths too", () => {
    expect(requestedAtUrl("/home/user/workspace/data/documents/report.pdf", "ts-1")).toBe(
      "/home/user/workspace/data/documents/report.pdf?requested_at=ts-1",
    );
  });

  it("leaves external URLs untouched", () => {
    expect(requestedAtUrl("https://example.com/pic.png", "ts-1")).toBeNull();
    expect(requestedAtUrl("//cdn.example.com/pic.png", "ts-1")).toBeNull();
  });

  it("leaves app routes untouched", () => {
    expect(requestedAtUrl("/api/uploads/x", "ts-1")).toBeNull();
  });

  it("does not double-append when a query string is already present", () => {
    expect(requestedAtUrl("/x/chart.png?v=2", "ts-1")).toBeNull();
  });
});

describe("renderMarkdown links", () => {
  function render(source: string): HTMLElement {
    const container = document.createElement("div");
    container.innerHTML = renderMarkdown(source);
    return container;
  }

  it("opens only external links in a new browsing context, with no opener or referrer", () => {
    const anchors = render(
      "[Docs](https://example.com/docs), [mail](mailto:a@example.com), [call](tel:+15551234) and [x](HTTPS://EXAMPLE.COM)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "https://example.com/docs",
      "mailto:a@example.com",
      "tel:+15551234",
      "HTTPS://EXAMPLE.COM",
    ]);
    expect(Array.from(anchors, (a) => [a.getAttribute("target"), a.getAttribute("rel")])).toEqual(
      Array(4).fill(["_blank", "noopener noreferrer"]),
    );
  });

  it("keeps a file or local link's real target, with nothing that would open it in the browser", () => {
    const anchors = render(
      "[Q4 report](/home/user/workspace/data/documents/q4.pdf), [app](http://localhost:3000/) and [api](http://127.0.0.1:8080/x)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "/home/user/workspace/data/documents/q4.pdf",
      "http://localhost:3000/",
      "http://127.0.0.1:8080/x",
    ]);
    expect(Array.from(anchors, (a) => [a.hasAttribute("target"), a.hasAttribute("download")])).toEqual(
      Array(3).fill([false, false]),
    );
  });

  it.each([
    "[the skill](.agents/skills/assist/SKILL.md)",
    "[guide](docs/guide.md:12)",
    "[report](q4.md:12)",
    "[section](#usage)",
    "[local](file:///home/user/workspace/notes.md)",
    "[text me](sms:+15551234)",
    "[cdn](//cdn.example.com/lib.js)",
  ])("renders %s as its label text with no link", (source) => {
    const container = render(source);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent!.trim()).toBe(source.slice(1, source.indexOf("]")));
  });

  it("keeps the markup and image inside an unwrapped link", () => {
    const container = render("[**bold** ![Chart](/home/user/workspace/data/images/chart.png)](data/images/chart.png)");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("strong")!.textContent).toBe("bold");
    expect(container.querySelector("img")!.getAttribute("src")).toBe("/home/user/workspace/data/images/chart.png");
  });
});
