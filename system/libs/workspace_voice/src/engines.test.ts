import { describe, expect, it } from "vitest";
import {
  DEFAULT_ENGINE_ID,
  ENGINES,
  EngineRegistryError,
  engineSpec,
  engineVariant,
  engineWindowSeconds,
  maxTokensFor,
  parseEngineRegistry,
  pinnedFileUrl,
  variantBytes,
  variantFiles,
} from "./engines";

describe("the engine registry", () => {
  it("ships Moonshine Base as the default engine, with a variant for each device", () => {
    const spec = engineSpec(DEFAULT_ENGINE_ID);
    expect(ENGINES.map((engine) => engine.id)).toContain(DEFAULT_ENGINE_ID);
    expect(spec.runtime).toBe("transformers-js");
    expect(spec.variants.map((variant) => variant.device).sort()).toEqual(["wasm", "webgpu"]);
    // The quantized weights are what WebAssembly runs; the full-precision ones are WebGPU's (the spec's D2).
    expect(variantBytes(spec, engineVariant(spec, "wasm")!)).toBeLessThan(70_000_000);
    expect(variantBytes(spec, engineVariant(spec, "webgpu")!)).toBeGreaterThan(250_000_000);
  });

  it("names every file at the engine's commit, never at a branch", () => {
    const spec = engineSpec(DEFAULT_ENGINE_ID);
    for (const variant of spec.variants) {
      for (const file of variantFiles(spec, variant)) {
        expect(pinnedFileUrl(spec, file)).toBe(
          `https://huggingface.co/onnx-community/moonshine-base-ONNX/resolve/${spec.commit}/${file.path}`,
        );
        expect(file.sha256).toMatch(/^[0-9a-f]{64}$/);
      }
    }
    expect(engineVariant(spec, "wasm")!.files.map((file) => file.path)).toEqual([
      "onnx/encoder_model_quantized.onnx",
      "onnx/decoder_model_merged_quantized.onnx",
    ]);
  });

  it("sizes the streaming window from the engine's call limit", () => {
    expect(engineWindowSeconds(engineSpec(DEFAULT_ENGINE_ID))).toEqual({ softCapSeconds: 20, hardCapSeconds: 30 });
  });

  it("caps the decoder generously for short audio and in proportion for long audio", () => {
    const spec = engineSpec(DEFAULT_ENGINE_ID);
    expect(maxTokensFor(spec, 0.4)).toBe(16);
    expect(maxTokensFor(spec, 10)).toBe(75);
    expect(maxTokensFor({ ...spec, maxTokensPerSecond: null }, 10)).toBeNull();
  });

  it("refuses a registry that pins a file loosely", () => {
    const [engine] = JSON.parse(JSON.stringify({ engines: ENGINES })).engines;
    engine.commit = "main";
    expect(() => parseEngineRegistry({ engines: [engine] })).toThrow(EngineRegistryError);
    const [other] = JSON.parse(JSON.stringify({ engines: ENGINES })).engines;
    other.sharedFiles[0].sha256 = "abc";
    expect(() => parseEngineRegistry({ engines: [other] })).toThrow(/sha256/);
  });

  it("refuses a registry that names one file twice or one device twice", () => {
    const [engine] = JSON.parse(JSON.stringify({ engines: ENGINES })).engines;
    engine.variants[1].files.push({ ...engine.sharedFiles[0] });
    expect(() => parseEngineRegistry({ engines: [engine] })).toThrow(/twice/);
    const [other] = JSON.parse(JSON.stringify({ engines: ENGINES })).engines;
    other.variants[1].device = other.variants[0].device;
    expect(() => parseEngineRegistry({ engines: [other] })).toThrow(/two variants/);
  });

  it("refuses an unknown engine id", () => {
    expect(() => engineSpec("nope")).toThrow(EngineRegistryError);
  });
});
