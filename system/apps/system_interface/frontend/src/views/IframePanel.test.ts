import type m from "mithril";
import { describe, expect, it } from "vitest";

import { parseSelfReferentialServices } from "../base-path";
import { IFRAME_PANEL_SERVICE_NAME_ATTR, IframePanel } from "./IframePanel";

/** Render the component the way dockview's reactive renderer does. */
function render(attrs: {
  url: string;
  title: string;
  serviceName?: string;
  panelId?: string;
  isSelfReferential?: boolean;
}): m.Vnode {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  return (IframePanel.view as any)({ attrs }) as m.Vnode;
}

/** Flatten a vnode tree to its text, so a copy assertion doesn't depend on markup shape. */
function textOf(vnode: m.Vnode | string | number | boolean | null | undefined): string {
  if (vnode === null || vnode === undefined || typeof vnode === "boolean") return "";
  if (typeof vnode === "string" || typeof vnode === "number") return String(vnode);
  const children = vnode.children;
  if (typeof children === "string") return children;
  if (Array.isArray(children)) {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    return children.map((child: any) => textOf(child)).join("");
  }
  return "";
}

describe("IframePanel", () => {
  it("frames the service normally when it is not self-referential", () => {
    const vnode = render({ url: "http://web-x1.host-abc.localhost:8421/", title: "web", serviceName: "web" });
    expect(vnode.tag).toBe("iframe");
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const attrs = vnode.attrs as any;
    expect(attrs.src).toBe("http://web-x1.host-abc.localhost:8421/");
    expect(attrs[IFRAME_PANEL_SERVICE_NAME_ATTR]).toBe("web");
  });

  it("explains itself instead of framing a service that resolves back to this shell", () => {
    // The url is a real, reachable origin: getting the notice rather than an
    // iframe proves the refusal beat the derivation, not that the service
    // failed to resolve.
    const vnode = render({
      url: "http://si-preview-x1.host-abc.localhost:8421/",
      title: "preview",
      serviceName: "si-preview",
      isSelfReferential: true,
    });
    expect(vnode.tag).not.toBe("iframe");
    const text = textOf(vnode);
    expect(text).toContain("This tab is the preview you are already looking at.");
    expect(text).toContain("si-preview");
  });

  it("still frames a self-referential service that carries no service name", () => {
    // ``isSelfReferential`` is derived from ``serviceName``, so the pair cannot
    // disagree in practice; the panel falls back to framing rather than
    // rendering a notice that cannot name what it is refusing.
    const vnode = render({ url: "http://example.test/", title: "ad-hoc", isSelfReferential: true });
    expect(vnode.tag).toBe("iframe");
  });
});

describe("parseSelfReferentialServices", () => {
  it("splits the meta tag's list and ignores whitespace and empty entries", () => {
    expect(parseSelfReferentialServices(" si-preview , si-preview-app ,, ")).toEqual(
      new Set(["si-preview", "si-preview-app"]),
    );
  });

  it("yields an empty set for an absent or blank value", () => {
    expect(parseSelfReferentialServices("")).toEqual(new Set());
    expect(parseSelfReferentialServices("   ")).toEqual(new Set());
  });
});
