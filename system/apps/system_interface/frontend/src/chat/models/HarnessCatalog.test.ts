import { describe, expect, it, vi } from "vitest";
import type { HarnessCatalog } from "./HarnessCatalog";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../../base-path", () => ({ apiUrl: (path: string) => path }));

// The catalog cache and the single-flight promise are module-level state, so
// each test gets a fresh copy of the module.
async function loadHarnessCatalog(): Promise<typeof import("./HarnessCatalog")> {
  vi.resetModules();
  mockRequest.mockReset();
  return import("./HarnessCatalog");
}

function catalogFixture(popups: HarnessCatalog["popups"]): HarnessCatalog {
  return {
    options: [],
    switch_mode: "on_change",
    picker_mode: "dynamic",
    native_atomic_shoulder_tap_possible: true,
    popups,
  };
}

const CATALOGS = {
  claude: catalogFixture([
    { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
    { trigger: "composer_command", commands: ["/status", "/exit"], action: "notice" },
    { trigger: "turn_check", commands: [], action: "fast_mode_prompt" },
  ]),
  "pi-coding": catalogFixture([{ trigger: "composer_command", commands: ["/login"], action: "open_auth" }]),
};

describe("findComposerPopup", () => {
  async function loaded(): Promise<typeof import("./HarnessCatalog")> {
    const harnessCatalog = await loadHarnessCatalog();
    mockRequest.mockResolvedValue(CATALOGS);
    await harnessCatalog.ensureHarnessCatalogs();
    return harnessCatalog;
  }

  it("matches on the first token, so every argument form matches too", async () => {
    const harnessCatalog = await loaded();
    expect(harnessCatalog.findComposerPopup("claude", "/status")?.command).toBe("/status");
    expect(harnessCatalog.findComposerPopup("claude", "/status extra words")?.command).toBe("/status");
    expect(harnessCatalog.findComposerPopup("claude", "/login please")?.popup.action).toBe("open_auth");
  });

  it("ignores case and surrounding whitespace", async () => {
    const harnessCatalog = await loaded();
    expect(harnessCatalog.findComposerPopup("claude", "  /STATUS  ")?.command).toBe("/status");
  });

  it("leaves a command mentioned mid-sentence alone", async () => {
    const harnessCatalog = await loaded();
    expect(harnessCatalog.findComposerPopup("claude", "please run /status for me")).toBeNull();
    expect(harnessCatalog.findComposerPopup("claude", "hello there")).toBeNull();
  });

  it("matches only what the agent's own harness declared", async () => {
    const harnessCatalog = await loaded();
    expect(harnessCatalog.findComposerPopup("pi-coding", "/status")).toBeNull();
    expect(harnessCatalog.findComposerPopup("pi-coding", "/login")?.popup.action).toBe("open_auth");
    expect(harnessCatalog.findComposerPopup(undefined, "/status")).toBeNull();
  });
});

describe("hasFastModePrompt", () => {
  it("reports the harness's turn_check declaration", async () => {
    const harnessCatalog = await loadHarnessCatalog();
    mockRequest.mockResolvedValue(CATALOGS);
    await harnessCatalog.ensureHarnessCatalogs();
    expect(harnessCatalog.hasFastModePrompt("claude")).toBe(true);
    expect(harnessCatalog.hasFastModePrompt("pi-coding")).toBe(false);
    expect(harnessCatalog.hasFastModePrompt(undefined)).toBe(false);
  });
});

describe("ensureHarnessCatalogs", () => {
  it("is single-flight: concurrent awaiters share one request", async () => {
    const harnessCatalog = await loadHarnessCatalog();
    mockRequest.mockResolvedValue(CATALOGS);
    await Promise.all([harnessCatalog.ensureHarnessCatalogs(), harnessCatalog.ensureHarnessCatalogs()]);
    expect(mockRequest).toHaveBeenCalledTimes(1);
    expect(harnessCatalog.getHarnessCatalog("claude")).not.toBeNull();
  });

  it("retries after a failed load rather than wedging the session", async () => {
    // The composer's slash-command guard awaits this before deciding; a fetch
    // failure must not disable the guard until the next page load.
    const harnessCatalog = await loadHarnessCatalog();
    vi.spyOn(console, "warn").mockImplementation(() => {});
    mockRequest.mockRejectedValueOnce(new Error("offline"));
    await harnessCatalog.ensureHarnessCatalogs();
    expect(harnessCatalog.getHarnessCatalog("claude")).toBeNull();

    mockRequest.mockResolvedValue(CATALOGS);
    await harnessCatalog.ensureHarnessCatalogs();
    expect(harnessCatalog.getHarnessCatalog("claude")).not.toBeNull();
  });
});
