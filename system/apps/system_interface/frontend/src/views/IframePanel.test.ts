import { describe, expect, it } from "vitest";

import { IframePanel } from "./IframePanel";

type VnodeLike = {
  tag?: unknown;
  attrs?: Record<string, unknown>;
};

// Render the panel and return the iframe vnode's attrs. The component renders a
// single `m("iframe", attrs)`, so the top-level vnode is the iframe itself.
function renderIframeAttrs(): Record<string, unknown> {
  const vnode = { attrs: { url: "https://service.example/app", title: "Files" } };
  const rendered = IframePanel.view(vnode as unknown as Parameters<typeof IframePanel.view>[0]) as VnodeLike;
  expect(rendered.tag).toBe("iframe");
  expect(rendered.attrs).toBeDefined();
  return rendered.attrs!;
}

describe("IframePanel", () => {
  it("grants the app-tab iframe the download capability", () => {
    // Chromium blocks any download initiated from a sandboxed iframe unless the
    // sandbox lists `allow-downloads`; without it, app-tab download buttons do
    // nothing. This asserts the token is present so a future edit can't silently
    // drop it and re-break downloads.
    const sandbox = renderIframeAttrs().sandbox;
    expect(typeof sandbox).toBe("string");
    expect((sandbox as string).split(/\s+/)).toContain("allow-downloads");
  });

  it("preserves the pre-existing sandbox tokens alongside downloads", () => {
    // The download capability is additive: the tokens the framed app already
    // relies on (scripts, its own same-origin context, forms, popups) must all
    // remain, or enabling downloads would regress unrelated app behavior.
    const tokens = (renderIframeAttrs().sandbox as string).split(/\s+/);
    for (const expected of ["allow-scripts", "allow-same-origin", "allow-forms", "allow-popups"]) {
      expect(tokens).toContain(expected);
    }
  });
});
