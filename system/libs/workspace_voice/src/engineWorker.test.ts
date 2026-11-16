import { afterEach, describe, expect, it, vi } from "vitest";
import type { ConnectablePort, WorkerScopeLike } from "./engineWorker";

vi.mock("./transformersEngine", () => ({ loadTransformersEngine: vi.fn() }));

function fakePort(): ConnectablePort & { start: () => void } {
  return { postMessage: vi.fn(), onmessage: null, start: vi.fn<() => void>() };
}

afterEach(() => {
  vi.resetModules();
});

describe("the engine worker's entry", () => {
  it("takes each page's port as it connects to a shared worker, which has no postMessage of its own", async () => {
    const { attachWorkerScope } = await import("./engineWorker");
    const scope: WorkerScopeLike = { postMessage: undefined, onconnect: null, onmessage: null };
    attachWorkerScope(scope);
    expect(scope.onconnect).toBeTypeOf("function");
    const port = fakePort();
    scope.onconnect!({ ports: [port] });
    expect(port.onmessage).toBeTypeOf("function");
    expect(port.start).toHaveBeenCalledTimes(1);
  });

  it("is its page's own port as a dedicated worker", async () => {
    const { attachWorkerScope } = await import("./engineWorker");
    const scope: WorkerScopeLike = { postMessage: vi.fn(), onmessage: null };
    attachWorkerScope(scope);
    expect(scope.onmessage).toBeTypeOf("function");
  });

  it("does nothing in a window", async () => {
    const { attachWorkerScope } = await import("./engineWorker");
    const scope: WorkerScopeLike = { window: {}, postMessage: vi.fn(), onmessage: null };
    attachWorkerScope(scope);
    expect(scope.onmessage).toBeNull();
  });
});
