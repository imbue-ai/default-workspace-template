// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { catalogTemplateRecord } from "../testing/records";
import {
  TemplateDetail,
  adoptTemplateMessage,
  createMachineFromTemplateMessage,
  templateRequirements,
} from "./TemplateDetail";
import type { TemplateDetailAttrs } from "./TemplateDetail";

describe("templateRequirements", () => {
  it("lists accounts by scope with their permissions joined, then the model, keys, and packages", () => {
    const template = catalogTemplateRecord("digest", {
      required_accounts: [
        { scope: "slack-api", permission: "slack-read-all" },
        { scope: "gmail-api", permission: "gmail-read" },
        { scope: "slack-api", permission: "slack-write" },
        { scope: "slack-api", permission: "slack-read-all" },
      ],
      needs_ai: true,
      required_secrets: ["OPENWEATHER_API_KEY"],
      apt_packages: ["poppler-utils", "ffmpeg"],
    });
    expect(templateRequirements(template)).toEqual([
      { label: "Connect slack-api", detail: "slack-read-all, slack-write" },
      { label: "Connect gmail-api", detail: "gmail-read" },
      { label: "An AI model", detail: "it reasons over what it reads" },
      { label: "OPENWEATHER_API_KEY", detail: "a key you supply" },
      { label: "System packages", detail: "poppler-utils, ffmpeg" },
    ]);
  });

  it("is empty for a template that needs nothing", () => {
    expect(templateRequirements(catalogTemplateRecord("plain"))).toEqual([]);
  });
});

describe("the seeded messages", () => {
  it("adopt with the skill's command, and create a machine with a plain request naming the repository", () => {
    const template = catalogTemplateRecord("digest");
    expect(adoptTemplateMessage(template)).toBe("/use-template https://github.com/someone/digest");
    expect(createMachineFromTemplateMessage(template)).toContain("https://github.com/someone/digest");
  });
});

describe("TemplateDetail", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  function mountDetail(overrides: Partial<TemplateDetailAttrs>): TemplateDetailAttrs {
    const attrs: TemplateDetailAttrs = {
      template: catalogTemplateRecord("plain"),
      onBack: vi.fn(),
      onStartWithText: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(TemplateDetail, attrs) });
    return attrs;
  }

  it("shows the write-up as paragraphs and the Needs list, and links the repository", () => {
    mountDetail({
      template: catalogTemplateRecord("digest", {
        what_it_is: "Reads your inbox\nevery morning.\n\nWrites a digest.",
        required_accounts: [{ scope: "slack-api", permission: "slack-read-all" }],
        needs_ai: true,
      }),
    });
    const detail = root.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(detail.getAttribute("data-template")).toBe("digest");
    expect(Array.from(detail.querySelectorAll("p")).map((paragraph) => paragraph.textContent)).toEqual([
      "by someone",
      "Reads your inbox every morning.",
      "Writes a digest.",
    ]);
    expect(Array.from(detail.querySelectorAll("li")).map((item) => item.textContent)).toEqual([
      "Connect slack-apislack-read-all",
      "An AI modelit reasons over what it reads",
    ]);
    expect(detail.querySelector("a")!.getAttribute("href")).toBe("https://github.com/someone/digest");
  });

  it("falls back to the description and omits Needs when the template has neither write-up nor requirements", () => {
    mountDetail({ template: catalogTemplateRecord("plain", { description: "Just a thing.", author: "" }) });
    const detail = root.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(Array.from(detail.querySelectorAll("p")).map((paragraph) => paragraph.textContent)).toEqual([
      "Just a thing.",
    ]);
    expect(detail.querySelector("h4")).toBeNull();
  });

  it("starts a chat with the adopt or the create-machine message, and goes back on the back control", () => {
    const attrs = mountDetail({});
    root.querySelector<HTMLElement>(".new-tab-template-adopt")!.click();
    expect(attrs.onStartWithText).toHaveBeenLastCalledWith("/use-template https://github.com/someone/plain");
    root.querySelector<HTMLElement>(".new-tab-template-create-machine")!.click();
    expect(attrs.onStartWithText).toHaveBeenLastCalledWith(
      createMachineFromTemplateMessage(catalogTemplateRecord("plain")),
    );
    root.querySelector<HTMLElement>(".new-tab-template-back")!.click();
    expect(attrs.onBack).toHaveBeenCalledTimes(1);
  });
});
