import { beforeEach, describe, expect, it, vi } from "vitest";

// Capture mithril's request so the test drives the backend without a real network
// call and asserts the POST body/order. redraw is a no-op; apiUrl is identity so
// URLs are predictable. getAgentById is mocked to supply the live choice.
const { mockRequest, mockGetAgentById } = vi.hoisted(() => ({ mockRequest: vi.fn(), mockGetAgentById: vi.fn() }));
vi.mock("mithril", () => ({ default: { request: mockRequest, redraw: vi.fn() } }));
vi.mock("../base-path", () => ({ apiUrl: (path: string) => path }));
vi.mock("./AgentManager", () => ({ getAgentById: mockGetAgentById }));

import { changedAxes, effectiveChoice, getAgentFastMode, setFastMode, setModelChoice } from "./ModelSettings";
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
  harness_reported_model_id: "claude-opus-4-8",
};
const SONNET: CatalogModelOption = {
  id: "sonnet",
  label: "Sonnet 5",
  efforts: [{ level: "medium", in_picker: true }],
  supports_fast: false,
  in_picker: true,
  harness_reported_model_id: "claude-sonnet-5",
};

// A pushed live choice: the identity carries the raw REPORTED id (as the backend sends
// it), plus the option the backend matched it to. `reportedId` defaults to the matched
// option's reported id, so a settle test uses a realistic raw id.
function live(
  reportedId: string,
  effort: string | null,
  fast: boolean,
  matched: CatalogModelOption | null,
): ModelChoice {
  return { identity: { model_id: reportedId, effort, fast }, matched };
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

describe("changedAxes", () => {
  it("reports only the effort axis when just effort changed", () => {
    expect(
      changedAxes(
        { model_id: "opus[1m]", effort: "xhigh", fast: false },
        { model_id: "opus[1m]", effort: "medium", fast: false },
      ),
    ).toEqual(["effort"]);
  });

  it("counts a value going back to where it started as a change (the medium->xhigh->medium bug)", () => {
    // The baseline is what the user was looking at (xhigh), not disk -- so picking
    // medium again is a real change and gets sent.
    expect(
      changedAxes(
        { model_id: "opus[1m]", effort: "xhigh", fast: false },
        { model_id: "opus[1m]", effort: "medium", fast: false },
      ),
    ).toContain("effort");
  });

  it("counts no model change when the catalog id is unchanged", () => {
    // Callers pass the matched option's catalog id as `prev` (never a raw reported id),
    // so an effort/fast pick keeps the same id and does not re-send /model.
    expect(
      changedAxes(
        { model_id: "opus[1m]", effort: "high", fast: false },
        { model_id: "opus[1m]", effort: "high", fast: true },
      ),
    ).toEqual(["fast"]);
  });

  it("reports model and fast together when a model switch drops fast", () => {
    expect(
      changedAxes(
        { model_id: "opus[1m]", effort: "high", fast: true },
        { model_id: "sonnet", effort: "high", fast: false },
      ),
    ).toEqual(["model", "fast"]);
  });
});

describe("effectiveChoice", () => {
  it("returns the live choice when nothing is pending", () => {
    const choice = effectiveChoice("a1", live("claude-opus-4-8", "medium", true, OPUS));
    expect(choice).toEqual({
      identity: { model_id: "claude-opus-4-8", effort: "medium", fast: true },
      matched: OPUS,
      isPending: false,
    });
  });

  it("returns null when there is no live choice yet", () => {
    expect(effectiveChoice("a2", null)).toBeNull();
  });

  it("reflects an optimistic pick immediately and holds it until the matching live arrives", async () => {
    setModelChoice("a3", { model_id: "sonnet", effort: "medium", fast: false }, SONNET, ["model"]);

    // While pending, the bar shows the pick even though the live value is still opus.
    const pending = effectiveChoice("a3", live("claude-opus-4-8", "medium", true, OPUS));
    expect(pending).toEqual({
      identity: { model_id: "sonnet", effort: "medium", fast: false },
      matched: SONNET,
      isPending: true,
    });

    // The live choice settles the overlay via the MATCHED option (its raw reported id
    // differs from the catalog id), plus effort/fast agreement.
    const settled = effectiveChoice("a3", live("claude-sonnet-5", "medium", false, SONNET));
    expect(settled?.isPending).toBe(false);
    // Pending is cleared: a fresh render now reflects live, not the overlay.
    expect(effectiveChoice("a3", live("claude-sonnet-5", "medium", false, SONNET))?.isPending).toBe(false);
    await flush();
  });

  it("posts the pick to the model endpoint", async () => {
    setModelChoice("a4", { model_id: "opus[1m]", effort: "high", fast: true }, OPUS, ["effort", "fast"]);
    await flush();
    const call = mockRequest.mock.calls.find((args) => (args[0] as RequestOptions).method === "POST");
    expect(call).toBeDefined();
    const options = call![0] as RequestOptions;
    expect(options.url).toBe("/api/chats/:agentId/model");
    expect(options.body).toEqual({ model_id: "opus[1m]", effort: "high", fast: true, axes: ["effort", "fast"] });
  });

  it("on-change (optimistic=false) posts but holds no overlay -- the chip follows live", async () => {
    setModelChoice("a9", { model_id: "opus[1m]", effort: "high", fast: false }, OPUS, ["effort"], false);
    // No optimistic overlay: the bar reflects live, not the pick.
    const shown = effectiveChoice("a9", live("opus[1m]", "medium", false, OPUS));
    expect(shown?.identity.effort).toBe("medium");
    expect(shown?.isPending).toBe(false);
    await flush();
    // ...but the switch was still POSTed.
    const call = mockRequest.mock.calls.find((args) => (args[0] as RequestOptions).method === "POST");
    expect(call).toBeDefined();
    expect((call![0] as RequestOptions).body).toEqual({
      model_id: "opus[1m]",
      effort: "high",
      fast: false,
      axes: ["effort"],
    });
  });

  it("applies rapid picks in click order", async () => {
    setModelChoice("a5", { model_id: "sonnet", effort: "medium", fast: false }, SONNET, ["model"]);
    setModelChoice("a5", { model_id: "opus[1m]", effort: "medium", fast: false }, OPUS, ["model"]);
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
    expect((call![0] as RequestOptions).body).toEqual({
      model_id: "opus[1m]",
      effort: "high",
      fast: true,
      axes: ["fast"],
    });
  });

  it("setFastMode is a no-op for a model that does not support fast", async () => {
    mockGetAgentById.mockReturnValue({ model_choice: live("sonnet", "medium", false, SONNET) });
    setFastMode("a8", true);
    await flush();
    expect(mockRequest).not.toHaveBeenCalled();
  });
});
