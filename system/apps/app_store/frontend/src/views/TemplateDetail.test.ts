// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { catalogTemplateRecord } from "getting-started-frontend/src/testing/records";
import type { StoreTemplateDetailAttrs } from "./TemplateDetail";
import { StoreTemplateDetail, templateSizeText } from "./TemplateDetail";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

describe("templateSizeText", () => {
  it("says the size, that it is being estimated, or that it is not estimated", () => {
    expect(templateSizeText({ kind: "known", sizeKb: 72_192 })).toBe("about 70.5 MB");
    expect(templateSizeText({ kind: "known", sizeKb: 860 })).toBe("about 860 KB");
    expect(templateSizeText({ kind: "loading" })).toBe("Estimating…");
    expect(templateSizeText({ kind: "unknown" })).toBe("Not estimated");
  });
});

describe("StoreTemplateDetail", () => {
  function mountDetail(overrides: Partial<StoreTemplateDetailAttrs>): StoreTemplateDetailAttrs {
    const attrs: StoreTemplateDetailAttrs = {
      template: catalogTemplateRecord("digest"),
      size: { kind: "unknown" },
      onBack: vi.fn(),
      onStartWithText: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(StoreTemplateDetail, attrs) });
    return attrs;
  }

  it("lists what the template needs and says memory is not known", () => {
    mountDetail({
      template: catalogTemplateRecord("digest", { needs_ai: true, apt_packages: ["jq"] }),
      size: { kind: "known", sizeKb: 2048 },
    });
    expect(Array.from(root.querySelectorAll("[data-needs] li")).map((item) => item.textContent)).toEqual([
      "An AI modelit reasons over what it reads",
      "System packagesjq",
    ]);
    expect(root.querySelector("[data-template-size]")!.textContent).toBe("about 2 MB");
    expect(root.textContent).toContain("Not known until it's set up");
  });

  it("says when there is nothing to connect", () => {
    mountDetail({});
    expect(root.querySelector("[data-needs]")!.textContent).toBe("Nothing to connect or supply.");
    expect(root.querySelector("[data-template-size=unknown]")!.textContent).toBe("Not estimated");
  });

  it("installs through the adopt chat, and goes back", () => {
    const attrs = mountDetail({});
    root.querySelector<HTMLElement>(".app-store-adopt")!.click();
    expect(attrs.onStartWithText).toHaveBeenCalledWith("/use-template https://github.com/someone/digest");
    root.querySelector<HTMLElement>(".app-store-back")!.click();
    expect(attrs.onBack).toHaveBeenCalledTimes(1);
  });
});
