import { describe, expect, it, vi } from "vitest";
import type { CatalogModelOption, HarnessCatalog } from "./HarnessCatalog";
import { contextSizeOptions, formatContextWindow, modelName, oneOptionPerModel } from "./HarnessCatalog";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

// The catalog cache and the single-flight promise are module-level state, so
// each test gets a fresh copy of the module.
async function loadHarnessCatalog(): Promise<typeof import("./HarnessCatalog")> {
  vi.resetModules();
  mockRequest.mockReset();
  return import("./HarnessCatalog");
}

function catalogFixture(popups: HarnessCatalog["popups"]): HarnessCatalog {
  return {
    label: "Codex",
    options: [],
    switch_mode: "on_change",
    picker_mode: "dynamic",
    native_atomic_shoulder_tap_possible: true,
    supports_compaction: true,
    can_interrupt_compaction: true,
    popups,
  };
}

const CATALOGS = {
  claude: catalogFixture([
    { trigger: "composer_command", commands: ["/login", "/logout"], action: "open_auth" },
    { trigger: "composer_command", commands: ["/status", "/exit"], action: "notice" },
    { trigger: "turn_check", commands: [], action: "fast_mode_limit", notice_body: "Billed through the API." },
  ]),
  codex: catalogFixture([{ trigger: "turn_check", commands: [], action: "fast_mode_limit" }]),
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

describe("hasFastModeLimit", () => {
  it("reports the harness's turn_check declaration", async () => {
    const harnessCatalog = await loadHarnessCatalog();
    mockRequest.mockResolvedValue(CATALOGS);
    await harnessCatalog.ensureHarnessCatalogs();
    expect(harnessCatalog.hasFastModeLimit("claude")).toBe(true);
    expect(harnessCatalog.hasFastModeLimit("pi-coding")).toBe(false);
    expect(harnessCatalog.hasFastModeLimit(undefined)).toBe(false);
  });
});

describe("getFastModeLimitNoticeBody", () => {
  it("is the line the turn_check popup declared, and null where it declared none", async () => {
    const harnessCatalog = await loadHarnessCatalog();
    mockRequest.mockResolvedValue(CATALOGS);
    await harnessCatalog.ensureHarnessCatalogs();
    expect(harnessCatalog.getFastModeLimitNoticeBody("claude")).toBe("Billed through the API.");
    expect(harnessCatalog.getFastModeLimitNoticeBody("codex")).toBeNull();
    expect(harnessCatalog.getFastModeLimitNoticeBody("pi-coding")).toBeNull();
    expect(harnessCatalog.getFastModeLimitNoticeBody(undefined)).toBeNull();
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

function option(id: string, family: string | null, contextWindow: number | null): CatalogModelOption {
  return {
    id,
    label: family === null || contextWindow === null ? id : `${family} (${formatContextWindow(contextWindow)})`,
    efforts: [],
    default_effort: null,
    supports_fast: false,
    in_picker: true,
    harness_reported_model_id: null,
    family,
    context_window: contextWindow,
  };
}

describe("a model that runs at several context sizes", () => {
  const QWEN = option("local-model/qwen3:8b-ctx40960", "qwen3:8b", 40960);
  const LLAMA_40K = option("local-model/llama3.3:latest-ctx40960", "llama3.3:latest", 40960);
  const LLAMA_64K = option("local-model/llama3.3:latest-ctx65536", "llama3.3:latest", 65536);
  const LLAMA_128K = option("local-model/llama3.3:latest-ctx131072", "llama3.3:latest", 131072);
  const PLAIN = option("openrouter/qwen/qwen3", null, null);
  // Not in size order, as a server lists them.
  const OPTIONS = [QWEN, LLAMA_64K, PLAIN, LLAMA_128K, LLAMA_40K];

  it("is one row among the models, at the size nearest the one in use from below", () => {
    expect(oneOptionPerModel(OPTIONS, 65536)).toEqual([QWEN, LLAMA_64K, PLAIN]);
    expect(oneOptionPerModel(OPTIONS, 100000)).toEqual([QWEN, LLAMA_64K, PLAIN]);
    expect(oneOptionPerModel(OPTIONS, 131072)).toEqual([QWEN, LLAMA_128K, PLAIN]);
  });

  it("is at its smallest size when the size in use is smaller than any it offers, or there is none", () => {
    expect(oneOptionPerModel(OPTIONS, 8192)).toEqual([QWEN, LLAMA_40K, PLAIN]);
    expect(oneOptionPerModel(OPTIONS, null)).toEqual([QWEN, LLAMA_40K, PLAIN]);
  });

  it("offers its sizes in ascending order, and a model with no size of its own offers none", () => {
    expect(contextSizeOptions(OPTIONS, LLAMA_64K)).toEqual([LLAMA_40K, LLAMA_64K, LLAMA_128K]);
    expect(contextSizeOptions(OPTIONS, QWEN)).toEqual([QWEN]);
    expect(contextSizeOptions(OPTIONS, PLAIN)).toEqual([]);
  });

  it("is named for the model alone where its size is shown beside it", () => {
    expect(modelName(LLAMA_64K)).toBe("llama3.3:latest");
    expect(LLAMA_64K.label).toBe("llama3.3:latest (64K)");
    expect(modelName(PLAIN)).toBe("openrouter/qwen/qwen3");
  });

  it("shows a size in thousands where it is a round count", () => {
    expect(formatContextWindow(40960)).toBe("40K");
    expect(formatContextWindow(131072)).toBe("128K");
    expect(formatContextWindow(50000)).toBe("50,000");
  });
});
