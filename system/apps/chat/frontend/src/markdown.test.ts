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

  it("works for an on-disk path of any file type", () => {
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

  it("opens every web link in a new browsing context, with no opener or referrer, should nothing route it", () => {
    const anchors = render(
      "[Docs](https://example.com/docs), [mail](mailto:a@example.com), [call](tel:+15551234), [app](http://localhost:3000/) and [api](http://127.0.0.1:8080/x)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "https://example.com/docs",
      "mailto:a@example.com",
      "tel:+15551234",
      "http://localhost:3000/",
      "http://127.0.0.1:8080/x",
    ]);
    expect(Array.from(anchors, (a) => [a.getAttribute("target"), a.getAttribute("rel")])).toEqual(
      Array(5).fill(["_blank", "noopener noreferrer"]),
    );
  });

  it("names a file by its file URL, whether written as an absolute path or as a file URL", () => {
    const anchors = render(
      "[Q4 report](/home/user/workspace/data/my%20docs/q4.pdf), [folder](/home/user/workspace/data/), [plan](/home/user/my%20notes/plan%23.md?requested_at=2026-09-29#top), [root](/) and [notes](file:///home/user/workspace/notes.md)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "file:///home/user/workspace/data/my%20docs/q4.pdf",
      "file:///home/user/workspace/data",
      "file:///home/user/my%20notes/plan%23.md",
      "file:///",
      "file:///home/user/workspace/notes.md",
    ]);
    expect(Array.from(anchors, (a) => [a.hasAttribute("target"), a.hasAttribute("download")])).toEqual(
      Array(5).fill([false, false]),
    );
  });

  it.each([
    "[the skill](.agents/skills/assist/SKILL.md)",
    "[guide](docs/guide.md:12)",
    "[report](q4.md:12)",
    "[section](#usage)",
    "[remote file](file://server.example/share/notes.md)",
    "[text me](sms:+15551234)",
    "[cdn](//cdn.example.com/lib.js)",
    "[broken](/home/user/%zz)",
  ])("renders %s as its label text with no link", (source) => {
    const container = render(source);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent!.trim()).toBe(source.slice(1, source.indexOf("]")));
  });

  it.each([
    '<a href="//evil.example/x">other host</a>',
    '<a href="/\\evil.example/x">other host</a>',
    '<a href="//[bad/x">other host</a>',
    '<a href="/\\a%20b/x">other host</a>',
  ])("renders the raw link %s, which a browser takes to another host, as its text", (source) => {
    const container = render(source);
    expect(container.querySelector("a")).toBeNull();
    expect(container.textContent!.trim()).toBe("other host");
  });

  it("keeps the markup and image inside an unwrapped link", () => {
    const container = render("[**bold** ![Chart](/home/user/workspace/data/images/chart.png)](data/images/chart.png)");
    expect(container.querySelector("a")).toBeNull();
    expect(container.querySelector("strong")!.textContent).toBe("bold");
    expect(container.querySelector("img")!.getAttribute("src")).toBe("/home/user/workspace/data/images/chart.png");
  });
});
