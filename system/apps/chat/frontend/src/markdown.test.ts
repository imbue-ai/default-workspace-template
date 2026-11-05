// @vitest-environment jsdom
import m from "mithril";
import { afterEach, describe, expect, it, vi } from "vitest";

// lightbox touches the DOM imperatively; markdown.ts only needs its export to exist.
vi.mock("./lightbox", () => ({ openImageLightbox: vi.fn() }));

import { MarkdownContent, renderMarkdown, requestedAtUrl } from "./markdown";
import { forgetLocalImage, localImageUrl, rememberLocalImage } from "./models/attachments";

function mountMarkdown(content: string, requestedAt?: string): HTMLImageElement {
  const root = document.createElement("div");
  m.render(root, m(MarkdownContent, { content, requestedAt }));
  return root.querySelector("img")!;
}

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

describe("renderMarkdown embeds", () => {
  it("keeps a cross-origin image, video and audio embed, which the page policy admits from anywhere", () => {
    // The chat page's Content-Security-Policy opens img-src and media-src to every origin because the
    // sanitizer renders these three tags; a tag added here needs the policy's matching directive opened too
    // (server.py's attach_document_policy), and a tag the policy does not admit is better stripped here.
    const html = renderMarkdown(
      '<img src="https://cdn.example.com/a.png">\n' +
        '<video src="https://cdn.example.com/a.mp4" controls></video>\n' +
        '<audio src="https://cdn.example.com/a.mp3" controls></audio>',
    );
    expect(html).toContain('<img src="https://cdn.example.com/a.png">');
    expect(html).toContain('<video src="https://cdn.example.com/a.mp4" controls="">');
    expect(html).toContain('<audio src="https://cdn.example.com/a.mp3" controls="">');
  });

  it("strips the embeds the policy would block: frames, plugins and scripts", () => {
    const html = renderMarkdown(
      '<iframe src="https://evil.example.com/"></iframe>' +
        '<object data="https://evil.example.com/x.swf"></object>' +
        '<embed src="https://evil.example.com/x.swf">' +
        '<script src="https://evil.example.com/x.js"></script>',
    );
    expect(html).not.toMatch(/<(iframe|object|embed|script)/);
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
      "[Docs](https://example.com/docs), [mail](mailto:a@example.com), [call](tel:+15551234), [local call](tel:5551234), [app](http://localhost:3000/) and [api](http://127.0.0.1:8080/x)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "https://example.com/docs",
      "mailto:a@example.com",
      "tel:+15551234",
      "tel:5551234",
      "http://localhost:3000/",
      "http://127.0.0.1:8080/x",
    ]);
    expect(Array.from(anchors, (a) => [a.getAttribute("target"), a.getAttribute("rel")])).toEqual(
      Array(6).fill(["_blank", "noopener noreferrer"]),
    );
  });

  it("keeps a custom-scheme link, which an app of the workspace or of the user's system opens", () => {
    const anchors = render(
      "[channel](slack://channel?team=T1&id=C1), [text me](sms:+15551234) and [settings](x-apple.systempreferences:com.apple.preference.security)",
    ).querySelectorAll("a");
    expect(Array.from(anchors, (a) => a.getAttribute("href"))).toEqual([
      "slack://channel?team=T1&id=C1",
      "sms:+15551234",
      "x-apple.systempreferences:com.apple.preference.security",
    ]);
    expect(Array.from(anchors, (a) => a.getAttribute("target"))).toEqual(Array(3).fill("_blank"));
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
    "[build](Makefile:3:1)",
    "[code](app.ts:12-20)",
    "[app](localhost:3000/dashboard)",
    "[api](example.com:8080/x?q=1)",
    "[run](javascript:alert(1))",
    "[page](data:text/html,<p>hi</p>)",
    "[section](#usage)",
    "[remote file](file://server.example/share/notes.md)",
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
    '<a href="/&#9;/[bad/x">other host</a>',
    '<a href="/&#10;/evil.example/x">other host</a>',
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

describe("MarkdownContent images", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("holds a placeholder for an image until it loads, then lets its own size take over", () => {
    const image = mountMarkdown("![chart](/home/user/workspace/data/chart.png)", "ts-1");

    expect(image.getAttribute("src")).toBe("/home/user/workspace/data/chart.png?requested_at=ts-1");
    expect(image.classList.contains("is-loading")).toBe(true);

    image.dispatchEvent(new Event("load"));

    expect(image.classList.contains("is-loading")).toBe(false);
  });

  it("fades a loaded image in over the box, then lets the box go", () => {
    const image = mountMarkdown("![chart](/home/user/workspace/data/chart.png)", "ts-1");
    // jsdom applies no stylesheet, so stand in for the fade the chat's style gives an arriving image.
    image.style.animationName = "image-arrive";

    image.dispatchEvent(new Event("load"));
    expect(image.classList.contains("is-arriving")).toBe(true);

    image.dispatchEvent(new Event("animationend"));
    expect(image.classList.contains("is-arriving")).toBe(false);
  });

  it("does not wait on a fade that reduced motion turned off", () => {
    const image = mountMarkdown("![chart](/home/user/workspace/data/chart.png)", "ts-1");
    image.style.animationName = "none";

    image.dispatchEvent(new Event("load"));

    expect(image.classList.contains("is-arriving")).toBe(false);
  });

  it("gives a loaded image with no size of its own the size the browser decoded, so a viewBox-only SVG still shows", () => {
    const image = mountMarkdown("![board](/home/user/workspace/data/board.svg)", "ts-1");
    Object.defineProperty(image, "naturalWidth", { value: 225 });
    Object.defineProperty(image, "naturalHeight", { value: 150 });

    image.dispatchEvent(new Event("load"));

    expect(image.getAttribute("width")).toBe("225");
    expect(image.getAttribute("height")).toBe("150");
  });

  it("keeps the size an upload's path records over the decoded one", () => {
    const path = `/code/uploads/${"d".repeat(32)}-300x200/board.svg`;
    const image = mountMarkdown(`![${path}](${path})`, "ts-1");
    Object.defineProperty(image, "naturalWidth", { value: 225 });
    Object.defineProperty(image, "naturalHeight", { value: 150 });

    image.dispatchEvent(new Event("load"));

    expect(image.getAttribute("width")).toBe("300");
    expect(image.getAttribute("height")).toBe("200");
  });

  it("drops the placeholder when the image fails to load as well", () => {
    const image = mountMarkdown("![chart](/home/user/workspace/data/missing.png)");

    image.dispatchEvent(new Event("error"));

    expect(image.classList.contains("is-loading")).toBe(false);
  });

  it("holds an upload's box at the size its path records, so it does not jump when it loads", () => {
    const path = `/code/uploads/${"a".repeat(32)}-1600x900/photo.png`;

    const image = mountMarkdown(`![${path}](${path})`, "ts-1");

    expect(image.getAttribute("width")).toBe("1600");
    expect(image.getAttribute("height")).toBe("900");
    expect(image.classList.contains("is-loading")).toBe(true);
  });

  it("leaves an upload whose path records no size to the fixed-shape box", () => {
    const path = `/code/uploads/${"b".repeat(32)}/photo.png`;

    const image = mountMarkdown(`![${path}](${path})`, "ts-1");

    expect(image.hasAttribute("width")).toBe(false);
    expect(image.classList.contains("is-loading")).toBe(true);
  });

  it("draws an image this document uploaded from its local copy, at the size its path records", () => {
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", Object.assign(class extends URL {}, { revokeObjectURL }));
    const path = `/code/uploads/${"c".repeat(32)}-800x600/photo.png`;
    rememberLocalImage(path, "blob:local-photo", 1024);

    const image = mountMarkdown(`![${path}](${path})`, "ts-1");

    expect(image.getAttribute("src")).toBe("blob:local-photo");
    expect(image.getAttribute("width")).toBe("800");
    expect(image.getAttribute("height")).toBe("600");
    forgetLocalImage(path);
    expect(localImageUrl(path)).toBeUndefined();
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:local-photo");
  });
});
