import { describe, expect, it, vi } from "vitest";

const { mockRequest } = vi.hoisted(() => ({ mockRequest: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("@imbue/workspace-ui/src/base-path", () => ({ apiUrl: (path: string) => path }));

import { harnessCatalogFixture } from "../models/harnessCatalogFixture";

// The catalog cache is module-level state, and the label module reads it through the
// binding it imported, so both modules have to come from the same fresh registry.
async function freshModules(): Promise<{
  catalogs: typeof import("../models/HarnessCatalog");
  labels: typeof import("./harness-labels");
}> {
  vi.resetModules();
  mockRequest.mockReset();
  return { catalogs: await import("../models/HarnessCatalog"), labels: await import("./harness-labels") };
}

describe("harnessLabel", () => {
  it("names a harness the way the backend's catalog does, and falls back to the raw name", async () => {
    const { catalogs, labels } = await freshModules();
    // Before the catalogs land there is nothing to read; the raw name shows rather than nothing.
    expect(labels.harnessLabel("claude")).toBe("claude");

    mockRequest.mockResolvedValue({
      claude: harnessCatalogFixture("claude"),
      codex: harnessCatalogFixture("codex"),
    });
    await catalogs.ensureHarnessCatalogs();

    expect(labels.harnessLabel("claude")).toBe("Claude Code");
    expect(labels.harnessLabel("codex")).toBe("Codex");
    // A harness the backend did not list keeps its raw name.
    expect(labels.harnessLabel("antigravity")).toBe("antigravity");
  });
});
