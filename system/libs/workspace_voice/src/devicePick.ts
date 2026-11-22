/**
 * The device an engine runs on here (the workspace-voice-input spec, D2): WebGPU when the device has an adapter
 * and the engine a variant for it, else WebAssembly; the page's preference, when it has one and the engine can
 * honour it, wins. The worker picks this way for the load, and the page picks the same way to ask whether that
 * variant's files are already on the device (``downloadProbe.ts``), so the two never disagree.
 */

import type { Device, EngineSpec } from "./engines";
import { engineVariant } from "./engines";

/** The part of a navigator the pick reads: WebGPU's entry point, where the browser has it. */
export interface GpuNavigatorLike {
  readonly gpu?: { requestAdapter(): Promise<unknown> };
}

export async function pickDevice(
  spec: EngineSpec,
  preferred: Device | null,
  navigatorLike: GpuNavigatorLike,
): Promise<Device> {
  if (preferred !== null && engineVariant(spec, preferred) !== null) return preferred;
  const gpu = navigatorLike.gpu;
  if (gpu !== undefined && engineVariant(spec, "webgpu") !== null) {
    try {
      if ((await gpu.requestAdapter()) !== null) return "webgpu";
    } catch {
      // No usable adapter: WebAssembly below.
    }
  }
  return "wasm";
}
