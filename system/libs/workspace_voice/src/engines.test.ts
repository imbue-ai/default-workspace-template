import { describe, expect, it } from "vitest";
import {
  ENGINES,
  EngineRegistryError,
  defaultEngineId,
  engineSpec,
  engineVariant,
  engineWindowSeconds,
  maxTokensFor,
  parseEngineRegistry,
  pinnedFileUrl,
  variantBytes,
  variantFiles,
} from "./engines";

/** A mutable copy of the registered engine ``id``, as the registry file would spell it. */
function copyOfEngine(id: string) {
  return JSON.parse(JSON.stringify(engineSpec(id)));
}

/** A registry of ``engine`` alone, the default on every kind of device. */
function registryOf(engine: { id: string }) {
  return { engines: [engine], defaults: { desktop: engine.id, touch: engine.id } };
}

describe("the engine registry", () => {
  it("offers Moonshine Base on a desktop and Moonshine Tiny on a touch device, each with a variant for each device", () => {
    expect(defaultEngineId("desktop")).toBe("moonshine-base");
    expect(defaultEngineId("touch")).toBe("moonshine-tiny");
    for (const deviceClass of ["desktop", "touch"] as const) {
      const spec = engineSpec(defaultEngineId(deviceClass));
      expect(spec.devices).toContain(deviceClass);
      expect(spec.runtime).toBe("transformers-js");
      expect(spec.variants.map((variant) => variant.device).sort()).toEqual(["wasm", "webgpu"]);
    }
  });

  it("downloads the quantized weights for WebAssembly and the full-precision ones for WebGPU, fewer of each for Tiny", () => {
    const base = engineSpec("moonshine-base");
    const tiny = engineSpec("moonshine-tiny");
    // The quantized weights are what WebAssembly runs; the full-precision ones are WebGPU's (the spec's D2).
    expect(variantBytes(base, engineVariant(base, "wasm")!)).toBeLessThan(70_000_000);
    expect(variantBytes(base, engineVariant(base, "webgpu")!)).toBeGreaterThan(250_000_000);
    // Tiny is the touch default for being the lighter model and the smaller download (the spec's D9).
    expect(variantBytes(tiny, engineVariant(tiny, "wasm")!)).toBeLessThan(35_000_000);
    expect(variantBytes(tiny, engineVariant(tiny, "webgpu")!)).toBeLessThan(120_000_000);
  });

  it("names every file of every engine at the engine's commit, never at a branch", () => {
    expect(ENGINES.map((spec) => spec.repo)).toEqual([
      "onnx-community/moonshine-base-ONNX",
      "onnx-community/moonshine-tiny-ONNX",
    ]);
    for (const spec of ENGINES) {
      for (const variant of spec.variants) {
        for (const file of variantFiles(spec, variant)) {
          expect(pinnedFileUrl(spec, file)).toBe(
            `https://huggingface.co/${spec.repo}/resolve/${spec.commit}/${file.path}`,
          );
          expect(file.sha256).toMatch(/^[0-9a-f]{64}$/);
        }
      }
      expect(engineVariant(spec, "wasm")!.files.map((file) => file.path)).toEqual([
        "onnx/encoder_model_quantized.onnx",
        "onnx/decoder_model_merged_quantized.onnx",
      ]);
    }
  });

  it("sizes the streaming window from the engine's call limit", () => {
    expect(engineWindowSeconds(engineSpec("moonshine-base"))).toEqual({ softCapSeconds: 20, hardCapSeconds: 30 });
  });

  it("caps the decoder generously for short audio and in proportion for long audio", () => {
    const spec = engineSpec("moonshine-base");
    expect(maxTokensFor(spec, 0.4)).toBe(16);
    expect(maxTokensFor(spec, 10)).toBe(75);
    expect(maxTokensFor({ ...spec, maxTokensPerSecond: null }, 10)).toBeNull();
  });

  it("refuses a registry that pins a file loosely", () => {
    const engine = copyOfEngine("moonshine-base");
    engine.commit = "main";
    expect(() => parseEngineRegistry(registryOf(engine))).toThrow(EngineRegistryError);
    const other = copyOfEngine("moonshine-base");
    other.sharedFiles[0].sha256 = "abc";
    expect(() => parseEngineRegistry(registryOf(other))).toThrow(/sha256/);
  });

  it("refuses a registry that names one file twice or one device twice", () => {
    const engine = copyOfEngine("moonshine-base");
    engine.variants[1].files.push({ ...engine.sharedFiles[0] });
    expect(() => parseEngineRegistry(registryOf(engine))).toThrow(/twice/);
    const other = copyOfEngine("moonshine-base");
    other.variants[1].device = other.variants[0].device;
    expect(() => parseEngineRegistry(registryOf(other))).toThrow(/two variants/);
  });

  it("refuses a registry whose default for a kind of device is missing, unregistered, or not offered there", () => {
    const engine = copyOfEngine("moonshine-base");
    expect(() => parseEngineRegistry({ engines: [engine] })).toThrow(/defaults/);
    expect(() => parseEngineRegistry({ engines: [engine], defaults: { desktop: engine.id } })).toThrow(
      /defaults\.touch/,
    );
    expect(() =>
      parseEngineRegistry({ engines: [engine], defaults: { desktop: engine.id, touch: "moonshine-huge" } }),
    ).toThrow(/moonshine-huge/);
    engine.devices = ["desktop"];
    expect(() => parseEngineRegistry(registryOf(engine))).toThrow(/not offered on touch/);
  });

  it("refuses an unknown engine id", () => {
    expect(() => engineSpec("nope")).toThrow(EngineRegistryError);
  });
});
