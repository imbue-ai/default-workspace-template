/**
 * The engine worker's entry: a SharedWorker (one per workspace origin and engine) or, where the browser has
 * none, a dedicated worker, wired to the host in ``workerHost.ts``. Only this file and what it imports run in
 * the worker; it is the one place the speech runtime is imported.
 */

import type { Device, EngineSpec } from "./engines";
import { engineVariant } from "./engines";
import { loadTransformersEngine } from "./transformersEngine";
import type { HostPort } from "./workerHost";
import { createWorkerHost } from "./workerHost";
import type { PageMessage } from "./workerProtocol";

/** WebGPU when the device has an adapter and the engine a variant for it, else WebAssembly; the page's
 *  preference, when it has one and the engine can honour it, wins. */
async function pickDevice(spec: EngineSpec, preferred: Device | null): Promise<Device> {
  if (preferred !== null && engineVariant(spec, preferred) !== null) return preferred;
  const gpu = (navigator as Navigator & { gpu?: { requestAdapter(): Promise<unknown> } }).gpu;
  if (gpu !== undefined && engineVariant(spec, "webgpu") !== null) {
    try {
      if ((await gpu.requestAdapter()) !== null) return "webgpu";
    } catch {
      // No usable adapter: WebAssembly below.
    }
  }
  return "wasm";
}

interface ConnectablePort extends HostPort {
  onmessage: ((event: MessageEvent<PageMessage>) => void) | null;
  start?(): void;
}

// The worker libs' types clash with the DOM's program-wide, so the scope is duck-typed, as Sculptor's worker is.
interface WorkerScopeLike extends ConnectablePort {
  onconnect?: ((event: { ports: ConnectablePort[] }) => void) | null;
  postMessage(message: unknown): void;
}

const host = createWorkerHost({ loadEngine: loadTransformersEngine, pickDevice });

function attach(port: ConnectablePort): void {
  const handle = host.connect(port);
  port.onmessage = (event) => void handle(event.data);
  port.start?.();
}

const scope = globalThis as unknown as WorkerScopeLike & { window?: unknown };
if (typeof scope.window === "undefined" && typeof scope.postMessage === "function") {
  if ("onconnect" in scope) {
    scope.onconnect = (event) => attach(event.ports[0]);
  } else {
    attach(scope);
  }
}
