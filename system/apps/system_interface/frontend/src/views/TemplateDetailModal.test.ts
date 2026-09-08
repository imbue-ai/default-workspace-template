// @vitest-environment jsdom
import "../testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { catalogTemplateRecord } from "../testing/records";
import { TemplateDetailModal, templateRequirements } from "./TemplateDetailModal";
import type { TemplateDetailModalAttrs } from "./TemplateDetailModal";

describe("templateRequirements", () => {
  it("lists accounts by service with their permissions joined, then the model, keys, and packages", () => {
    const template = catalogTemplateRecord("digest", {
      required_accounts: [
        { service: "slack-api", permission: "slack-read-all" },
        { service: "gmail-api", permission: "gmail-read" },
        { service: "slack-api", permission: "slack-write" },
        { service: "slack-api", permission: "slack-read-all" },
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

describe("TemplateDetailModal", () => {
  let root: HTMLElement;

  beforeEach(() => {
    root = document.createElement("div");
    document.body.appendChild(root);
  });

  afterEach(() => {
    m.mount(root, null);
    root.remove();
  });

  /** Mount the dialog over a plain template with every action live, ``overrides`` laid over that. */
  function mountDetail(overrides: Partial<TemplateDetailModalAttrs>): TemplateDetailModalAttrs {
    const attrs: TemplateDetailModalAttrs = {
      template: catalogTemplateRecord("plain"),
      isStartDisabled: false,
      startDisabledReason: null,
      onClose: vi.fn(),
      onAdopt: vi.fn(),
      onCreateMachine: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(TemplateDetailModal, attrs) });
    return attrs;
  }

  it("shows the write-up as paragraphs and the Needs list, and links the repository", () => {
    mountDetail({
      template: catalogTemplateRecord("digest", {
        what_it_is: "Reads your inbox\nevery morning.\n\nWrites a digest.",
        required_accounts: [{ service: "slack-api", permission: "slack-read-all" }],
        needs_ai: true,
      }),
    });
    const detail = root.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(Array.from(detail.querySelectorAll("p")).map((paragraph) => paragraph.textContent)).toEqual([
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
    mountDetail({ template: catalogTemplateRecord("plain", { description: "Just a thing." }) });
    const detail = root.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(Array.from(detail.querySelectorAll("p")).map((paragraph) => paragraph.textContent)).toEqual([
      "Just a thing.",
    ]);
    expect(detail.querySelector("h4")).toBeNull();
  });

  it("stands both actions down when told to, so neither callback fires", () => {
    const attrs = mountDetail({
      isStartDisabled: true,
      startDisabledReason: "No app on this machine can start a chat",
    });
    const adopt = root.querySelector<HTMLElement>(".new-tab-template-adopt")!;
    const createMachine = root.querySelector<HTMLElement>(".new-tab-template-create-machine")!;
    expect(adopt.getAttribute("aria-disabled")).toBe("true");
    expect(createMachine.getAttribute("aria-disabled")).toBe("true");
    adopt.click();
    createMachine.click();
    expect(attrs.onAdopt).not.toHaveBeenCalled();
    expect(attrs.onCreateMachine).not.toHaveBeenCalled();
  });
});
