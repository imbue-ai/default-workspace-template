import { describe, expect, it } from "vitest";
import { pickDevice } from "./devicePick";
import { engineSpec } from "./engines";

const SPEC = engineSpec("moonshine-base");
const ONLY_WASM = { ...SPEC, variants: SPEC.variants.filter((variant) => variant.device === "wasm") };

describe("pickDevice", () => {
  it("picks WebGPU where the device has an adapter, else WebAssembly", async () => {
    expect(await pickDevice(SPEC, null, { gpu: { requestAdapter: async () => ({}) } })).toBe("webgpu");
    expect(await pickDevice(SPEC, null, { gpu: { requestAdapter: async () => null } })).toBe("wasm");
    expect(await pickDevice(SPEC, null, {})).toBe("wasm");
    expect(
      await pickDevice(SPEC, null, {
        gpu: {
          requestAdapter: async () => {
            throw new Error("no adapter");
          },
        },
      }),
    ).toBe("wasm");
  });

  it("honours the page's preference when the engine has a variant for it", async () => {
    const withAdapter = { gpu: { requestAdapter: async () => ({}) } };
    expect(await pickDevice(SPEC, "wasm", withAdapter)).toBe("wasm");
    expect(await pickDevice(SPEC, "webgpu", {})).toBe("webgpu");
    expect(await pickDevice(ONLY_WASM, "webgpu", withAdapter)).toBe("wasm");
  });
});
