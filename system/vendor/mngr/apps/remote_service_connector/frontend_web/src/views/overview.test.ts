// The overview tile-state decision table against a stubbed fetch. The case
// that matters most: a workspace with share materials but a dead tunnel
// (connector says the share is active; the gateway probe fails at the
// transport layer) must render as UNREACHABLE -- never as "not shared",
// which would hide that something is wrong.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { WireRecord } from "../api";
import { type Tile, resolveTileHealth, visibleTiles } from "./overview";

const HOST_ID = "host-" + "f".repeat(32);
const DOMAIN = `${HOST_ID}.owner.us1.example.com`;

function record(overrides: Partial<WireRecord>): WireRecord {
  return {
    host_id: HOST_ID,
    agent_id: "agent-1",
    display_name: "ws",
    color: null,
    provider_kind: "imbue_cloud",
    hosting_device_id: null,
    device_label: "web",
    state: "active",
    restored_from_host_id: null,
    encrypted_secrets: null,
    revision: 1,
    ...overrides,
  };
}

function tile(overrides: Partial<WireRecord> = {}): Tile {
  return {
    record: record(overrides),
    health: "checking",
    workspaceDomain: null,
  };
}

interface StubOptions {
  shareState: string | null; // null = no share row (404)
  probe: "network_error" | "alive_204" | { backend: string };
}

function stubConnectorAndGateway(options: StubOptions): void {
  vi.stubGlobal("fetch", async (url: string) => {
    if (url.startsWith(`/shares/${HOST_ID}/status`)) {
      if (options.shareState === null)
        return new Response(null, { status: 404 });
      return new Response(
        JSON.stringify({
          host_id: HOST_ID,
          workspace_domain: DOMAIN,
          state: options.shareState,
          entry_label: "system_interface-abc123",
        }),
        { status: 200 },
      );
    }
    if (url.includes("/_health")) {
      if (options.probe === "network_error") {
        // A dead tunnel fails at the transport layer (DNS/TLS/connect), which
        // surfaces from fetch as a rejection, not an HTTP status.
        throw new TypeError("NetworkError when attempting to fetch resource");
      }
      if (options.probe === "alive_204")
        return new Response(null, { status: 204 });
      return new Response(JSON.stringify({ backend: options.probe.backend }), {
        status: 200,
      });
    }
    throw new Error(`unexpected fetch: ${url}`);
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("resolveTileHealth", () => {
  it("reports share-active-but-dead-tunnel as unreachable, not desktop-only", async () => {
    stubConnectorAndGateway({ shareState: "active", probe: "network_error" });
    const t = tile();

    await resolveTileHealth(t);

    expect(t.health).toBe("unreachable");
    // The domain is still recorded so the tile can link/retry.
    expect(t.workspaceDomain).toBe(DOMAIN);
  });

  it("reports a workspace with no share row as not_shared", async () => {
    stubConnectorAndGateway({ shareState: null, probe: "network_error" });
    const t = tile();

    await resolveTileHealth(t);

    expect(t.health).toBe("not_shared");
  });

  it("reports an inactive share as not_shared", async () => {
    stubConnectorAndGateway({ shareState: "inactive", probe: "network_error" });
    const t = tile();

    await resolveTileHealth(t);

    expect(t.health).toBe("not_shared");
  });

  it("reports a live gateway as healthy", async () => {
    stubConnectorAndGateway({ shareState: "active", probe: "alive_204" });
    const t = tile();

    await resolveTileHealth(t);

    expect(t.health).toBe("healthy");
  });

  it("reports a live gateway with an unhealthy backend as degraded", async () => {
    stubConnectorAndGateway({
      shareState: "active",
      probe: { backend: "starting" },
    });
    const t = tile();

    await resolveTileHealth(t);

    expect(t.health).toBe("degraded");
  });

  it("reports a destroyed record without probing anything", async () => {
    vi.stubGlobal("fetch", async (url: string) => {
      throw new Error(`no fetch expected for a destroyed record: ${url}`);
    });
    const t = tile({ state: "destroyed" });

    await resolveTileHealth(t);

    expect(t.health).toBe("destroyed");
  });
});

describe("visibleTiles", () => {
  it("hides destroyed tiles by default and shows them only via the toggle", () => {
    const active = tile();
    const destroyed = tile({ state: "destroyed" });

    expect(visibleTiles([active, destroyed], false)).toEqual([active]);
    expect(visibleTiles([active, destroyed], true)).toEqual([
      active,
      destroyed,
    ]);
    expect(visibleTiles([destroyed], false)).toEqual([]);
    expect(visibleTiles([], false)).toEqual([]);
  });
});
