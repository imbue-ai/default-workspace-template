import { describe, expect, it } from "vitest";
import { probeEngineDownload } from "./downloadProbe";
import { engineSpec, engineVariant, pinnedFileUrl, variantBytes, variantFiles } from "./engines";
import type { PinnedFileStore } from "./fileStore";

const SPEC = engineSpec("moonshine-base");
const OPTIONS = { engineId: SPEC.id, preferredDevice: null, assetBase: "https://chat.test/assets/voice/" };

/** A store that says it keeps exactly ``urls``, and records what it was asked for. */
function storeKeeping(urls: Iterable<string>): PinnedFileStore & { asked: string[] } {
  const kept = new Set(urls);
  const asked: string[] = [];
  return {
    asked,
    has: async (url) => {
      asked.push(url);
      return kept.has(url);
    },
    read: async () => null,
    write: async () => undefined,
    prune: async () => undefined,
  };
}

function urlsOf(device: "webgpu" | "wasm"): string[] {
  return variantFiles(SPEC, engineVariant(SPEC, device)!).map((file) => pinnedFileUrl(SPEC, file));
}

describe("probeEngineDownload", () => {
  it("finds the device's variant downloaded when the store keeps every one of its files", async () => {
    const store = storeKeeping(urlsOf("webgpu"));
    const probe = await probeEngineDownload(OPTIONS, {
      store,
      navigator: { gpu: { requestAdapter: async () => ({}) } },
    });
    expect(probe).toEqual({
      isDownloaded: true,
      totalBytes: variantBytes(SPEC, engineVariant(SPEC, "webgpu")!),
      device: "webgpu",
    });
    expect(store.asked.sort()).toEqual(urlsOf("webgpu").sort());
  });

  it("finds it not downloaded when one file is missing, and sizes the download the device would run", async () => {
    const [, ...allButFirst] = urlsOf("wasm");
    const probe = await probeEngineDownload(OPTIONS, { store: storeKeeping(allButFirst), navigator: {} });
    expect(probe).toEqual({
      isDownloaded: false,
      totalBytes: variantBytes(SPEC, engineVariant(SPEC, "wasm")!),
      device: "wasm",
    });
    // The WebGPU variant being kept says nothing about a device that runs WebAssembly.
    const other = await probeEngineDownload(OPTIONS, { store: storeKeeping(urlsOf("webgpu")), navigator: {} });
    expect(other.isDownloaded).toBe(false);
  });

  it("asks about the variant the page insists on", async () => {
    const probe = await probeEngineDownload(
      { ...OPTIONS, preferredDevice: "wasm" },
      { store: storeKeeping(urlsOf("wasm")), navigator: { gpu: { requestAdapter: async () => ({}) } } },
    );
    expect(probe.device).toBe("wasm");
    expect(probe.isDownloaded).toBe(true);
  });
});
