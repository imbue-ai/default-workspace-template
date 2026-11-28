// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { appListing, templateListing } from "../models/TemplateCatalog";
import { catalogAppRecord, catalogTemplateRecord } from "../testing/records";
import {
  TemplateDetail,
  adoptTemplateMessage,
  createMachineFromTemplateMessage,
  createWorkspaceWithAppMessage,
  installAppMessage,
  listingActions,
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

  it("lists what an app asks for the same way", () => {
    const app = catalogAppRecord("weather-desk", {
      required_accounts: [{ scope: "slack-api", permission: "slack-read-all" }],
      needs_ai: true,
      required_secrets: ["OPENWEATHER_API_KEY"],
      apt_packages: ["ffmpeg"],
    });
    expect(templateRequirements(app)).toEqual([
      { label: "Connect slack-api", detail: "slack-read-all" },
      { label: "An AI model", detail: "it reasons over what it reads" },
      { label: "OPENWEATHER_API_KEY", detail: "a key you supply" },
      { label: "System packages", detail: "ffmpeg" },
    ]);
    expect(templateRequirements(catalogAppRecord("plain"))).toEqual([]);
  });
});

describe("the seeded messages", () => {
  it("adopt with the skill's command, and create a machine with a plain request naming the repository", () => {
    const template = catalogTemplateRecord("digest");
    expect(adoptTemplateMessage(template)).toBe("/use-template https://github.com/someone/digest");
    expect(createMachineFromTemplateMessage(template)).toContain("https://github.com/someone/digest");
  });

  it("install an app with the install skill's command, and create a workspace with a request naming the app's repository", () => {
    const app = catalogAppRecord("weather-desk");
    expect(installAppMessage(app)).toBe("/install-app https://github.com/someone/weather-desk");
    expect(createWorkspaceWithAppMessage(app)).toBe(
      "Please create a new Imbue Studio workspace for me with the app at https://github.com/someone/weather-desk " +
        "installed in it (the imbue-studio skill can create one). Walk me through anything it needs from me, like " +
        "permissions or accounts, and tell me when it is ready.",
    );
    // The request is the template one with the app in the template's place: it never calls the app a template.
    expect(createWorkspaceWithAppMessage(app)).not.toContain("template");
    expect(createMachineFromTemplateMessage(catalogTemplateRecord("digest"))).toBe(
      "Please create a new Imbue Studio workspace for me from the template at https://github.com/someone/digest " +
        "(the imbue-studio skill can create one). Walk me through anything it needs from me, like " +
        "permissions or accounts, and tell me when it is ready.",
    );
  });
});

describe("listingActions", () => {
  it("offers a template's two actions and an app's two actions, the one for this workspace first", () => {
    expect(listingActions(templateListing(catalogTemplateRecord("digest"))).map((action) => action.label)).toEqual([
      "Make it mine",
      "Create a new workspace from this",
    ]);
    const app = catalogAppRecord("weather-desk");
    expect(listingActions(appListing(app))).toEqual([
      { label: "Add to this workspace", marker: "new-tab-app-install", message: installAppMessage(app) },
      {
        label: "Create a new workspace with this",
        marker: "new-tab-app-create-workspace",
        message: createWorkspaceWithAppMessage(app),
      },
    ]);
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
      listing: templateListing(catalogTemplateRecord("plain")),
      onBack: vi.fn(),
      onStartWithText: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(TemplateDetail, attrs) });
    return attrs;
  }

  it("shows the write-up as paragraphs and the Needs list, and links the repository", () => {
    mountDetail({
      listing: templateListing(
        catalogTemplateRecord("digest", {
          what_it_is: "Reads your inbox\nevery morning.\n\nWrites a digest.",
          required_accounts: [{ scope: "slack-api", permission: "slack-read-all" }],
          needs_ai: true,
        }),
      ),
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
    mountDetail({
      listing: templateListing(catalogTemplateRecord("plain", { description: "Just a thing.", author: "" })),
    });
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

  it("shows an app's icon, description, Needs list and repository, marked with the app's name", () => {
    mountDetail({
      listing: appListing(
        catalogAppRecord("weather-desk", {
          title: "Weather Desk",
          description: "Forecasts at a glance.",
          required_secrets: ["OPENWEATHER_API_KEY"],
        }),
      ),
    });
    const detail = root.querySelector<HTMLElement>(".new-tab-template-detail")!;
    expect(detail.getAttribute("data-app")).toBe("weather-desk");
    expect(detail.hasAttribute("data-template")).toBe(false);
    expect(detail.querySelector("h2")!.textContent).toBe("Weather Desk");
    expect(detail.querySelector("img")!.getAttribute("src")).toBe("https://example.test/app-icons/weather-desk.svg");
    expect(Array.from(detail.querySelectorAll("p")).map((paragraph) => paragraph.textContent)).toEqual([
      "by someone",
      "Forecasts at a glance.",
    ]);
    expect(Array.from(detail.querySelectorAll("li")).map((item) => item.textContent)).toEqual([
      "OPENWEATHER_API_KEYa key you supply",
    ]);
    expect(detail.querySelector("a")!.getAttribute("href")).toBe("https://github.com/someone/weather-desk");
  });

  it("an app's two actions start the install chat and the create-a-workspace chat, never a template's", () => {
    const app = catalogAppRecord("weather-desk");
    const attrs = mountDetail({ listing: appListing(app) });
    expect(root.querySelector(".new-tab-template-adopt")).toBeNull();
    expect(root.querySelector(".new-tab-template-create-machine")).toBeNull();

    const install = root.querySelector<HTMLElement>(".new-tab-app-install")!;
    expect(install.textContent).toBe("Add to this workspace");
    install.click();
    expect(attrs.onStartWithText).toHaveBeenLastCalledWith("/install-app https://github.com/someone/weather-desk");

    const create = root.querySelector<HTMLElement>(".new-tab-app-create-workspace")!;
    expect(create.textContent).toBe("Create a new workspace with this");
    create.click();
    expect(attrs.onStartWithText).toHaveBeenLastCalledWith(createWorkspaceWithAppMessage(app));
    expect(attrs.onStartWithText).toHaveBeenCalledTimes(2);
  });

  it("shows no write-up for an app with no description", () => {
    mountDetail({ listing: appListing(catalogAppRecord("bare", { description: "", author: "" })) });
    expect(root.querySelector(".new-tab-template-detail")!.querySelectorAll("p")).toHaveLength(0);
  });
});
