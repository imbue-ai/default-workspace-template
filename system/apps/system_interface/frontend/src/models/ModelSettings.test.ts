import { beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend without a real network
// call and asserts the POST body/order. redraw is a no-op; apiUrl is identity so
// URLs are predictable. getAgentById is mocked to supply the live choice.
const { mockRequest, mockGetAgentById } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockGetAgentById: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./AgentManager", () => ({ getAgentById: mockGetAgentById }));

import { effectiveChoice, getAgentFastMode, isPickInFlight, setFastMode, setModelChoice } from "./ModelSettings";
import type { ModelChoice } from "./ModelSettings";
import type { CatalogModelOption } from "./HarnessCatalog";

const OPUS: CatalogModelOption = {
  id: "opus[1m]",
  label: "Opus 5 (1M)",
  efforts: [
    { level: "medium", in_picker: true },
    { level: "high", in_picker: true },
  ],
  supports_fast: true,
  in_picker: true,
};
const SONNET: CatalogModelOption = {
  id: "sonnet",
  label: "Sonnet 5",
  efforts: [{ level: "medium", in_picker: true }],
  supports_fast: false,
  in_picker: true,
};

function live(modelId: string, effort: string | null, fast: boolean, matched: CatalogModelOption | null): ModelChoice {
  return { identity: { model_id: modelId, effort, fast }, source: "live", matched };
}

interface RequestOptions {
  method: string;
  url: string;
  body?: { model_id?: string; effort?: string | null; fast?: boolean };
}

// Let the single-flight chain's promise callbacks run.
async function flush(): Promise<void> {
  for (let i = 0; i < 4; i++) {
    await new Promise<void>((resolve) => setTimeout(resolve, 0));
  }
}

beforeEach(() => {
  mockRequest.mockReset();
  mockRequest.mockResolvedValue({});
  mockGetAgentById.mockReset();
  mockGetAgentById.mockReturnValue(undefined);
});

describe("effectiveChoice", () => {
  it("returns the live choice when nothing is pending", () => {
    const choice = effectiveChoice("a1", live("opus[1m]", "medium", true, OPUS));
    expect(choice).toEqual({
      identity: { model_id: "opus[1m]", effort: "medium", fast: true },
      matched: OPUS,
      isPending: false,
    });
  });

  it("returns null when there is no live choice yet", () => {
    expect(effectiveChoice("a2", null)).toBeNull();
  });

  it("reflects an optimistic pick immediately and holds it until the matching live arrives", async () => {
    setModelChoice("a3", { model_id: "sonnet", effort: "medium", fast: false }, SONNET);
    expect(isPickInFlight("a3")).toBe(true);

    // While pending, the bar shows the pick even though the live value is still opus.
    const pending = effectiveChoice("a3", live("opus[1m]", "medium", true, OPUS));
    expect(pending).toEqual({
      identity: { model_id: "sonnet", effort: "medium", fast: false },
      matched: SONNET,
      isPending: true,
    });

    // The matching live choice clears the overlay.
    const settled = effectiveChoice("a3", live("sonnet", "medium", false, SONNET));
    expect(settled?.isPending).toBe(false);
    expect(isPickInFlight("a3")).toBe(false);
    await flush();
  });

  it("posts the pick to the model endpoint", async () => {
    setModelChoice("a4", { model_id: "opus[1m]", effort: "high", fast: true }, OPUS);
    await flush();
    const call = mockRequest.mock.calls.find((args) => (args[0] as RequestOptions).method === "POST");
    expect(call).toBeDefined();
    const options = call![0] as RequestOptions;
    expect(options.url).toBe("/api/agents/:agentId/model");
    expect(options.body).toEqual({ model_id: "opus[1m]", effort: "high", fast: true });
  });

  it("applies rapid picks in click order", async () => {
    setModelChoice("a5", { model_id: "sonnet", effort: "medium", fast: false }, SONNET);
    setModelChoice("a5", { model_id: "opus[1m]", effort: "medium", fast: false }, OPUS);
    await flush();
    const models = mockRequest.mock.calls
      .map((args) => args[0] as RequestOptions)
      .filter((options) => options.method === "POST")
      .map((options) => options.body?.model_id);
    expect(models).toEqual(["sonnet", "opus[1m]"]);
  });
});

describe("fast mode helpers", () => {
  it("reads the agent's fast state from the live choice", () => {
    mockGetAgentById.mockReturnValue({ model_choice: live("opus[1m]", "medium", true, OPUS) });
    expect(getAgentFastMode("a6")).toBe(true);
  });

  it("setFastMode applies fast to the current model, keeping the effort", async () => {
    mockGetAgentById.mockReturnValue({ model_choice: live("opus[1m]", "high", false, OPUS) });
    setFastMode("a7", true);
    await flush();
    const call = mockRequest.mock.calls.find((args) => (args[0] as RequestOptions).method === "POST");
    expect((call![0] as RequestOptions).body).toEqual({ model_id: "opus[1m]", effort: "high", fast: true });
  });

  it("setFastMode is a no-op for a model that does not support fast", async () => {
    mockGetAgentById.mockReturnValue({ model_choice: live("sonnet", "medium", false, SONNET) });
    setFastMode("a8", true);
    await flush();
    expect(mockRequest).not.toHaveBeenCalled();
  });
});
