// CAS push behavior against a stubbed fetch: first-push, merge-on-conflict,
// and give-up-after-retries.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { WireRecord } from "./api";
import { pushRecordWithCas } from "./records";

function wireRecord(overrides: Partial<WireRecord>): WireRecord {
  return {
    host_id: "host-" + "a".repeat(32),
    agent_id: "agent-1",
    display_name: "ws",
    color: null,
    provider_kind: "imbue_cloud",
    hosting_device_id: null,
    device_label: "web",
    state: "active",
    restored_from_host_id: null,
    encrypted_secrets: null,
    revision: 0,
    ...overrides,
  };
}

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("pushRecordWithCas", () => {
  it("pushes revision 1 for a fresh record", async () => {
    const seen: WireRecord[] = [];
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      const pushed = JSON.parse(String(init?.body)) as WireRecord;
      seen.push(pushed);
      return jsonResponse(200, pushed);
    });

    const result = await pushRecordWithCas(wireRecord({}).host_id, () =>
      wireRecord({}),
    );

    expect(seen).toHaveLength(1);
    expect(seen[0].revision).toBe(1);
    expect(result.revision).toBe(1);
  });

  it("merges the stored row and retries on a 409", async () => {
    const stored = wireRecord({ revision: 4, display_name: "server-name" });
    const seen: WireRecord[] = [];
    let calls = 0;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      const pushed = JSON.parse(String(init?.body)) as WireRecord;
      seen.push(pushed);
      calls += 1;
      if (calls === 1) {
        return jsonResponse(409, {
          detail: { message: "revision conflict", stored },
        });
      }
      return jsonResponse(200, pushed);
    });

    const result = await pushRecordWithCas(stored.host_id, (latest) =>
      wireRecord({ display_name: latest?.display_name ?? "mine" }),
    );

    expect(seen).toHaveLength(2);
    expect(seen[1].revision).toBe(5);
    expect(seen[1].display_name).toBe("server-name");
    expect(result.revision).toBe(5);
  });

  it("preserves a concurrent desktop edit when re-applying its own inside one CAS window", async () => {
    // The desktop enriched the record (agent id, name, secrets) between the
    // web's read and its push. The web edit spreads the stored row before
    // re-applying only its own field, so the desktop's edit survives the
    // retry alongside the web's.
    const desktopRow = wireRecord({
      revision: 7,
      agent_id: "agent-desktop-enriched",
      display_name: "desktop-name",
      encrypted_secrets: "ZGVza3RvcC1zZWNyZXRz",
    });
    const seen: WireRecord[] = [];
    let calls = 0;
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      const pushed = JSON.parse(String(init?.body)) as WireRecord;
      seen.push(pushed);
      calls += 1;
      if (calls === 1) {
        return jsonResponse(409, {
          detail: { message: "revision conflict", stored: desktopRow },
        });
      }
      return jsonResponse(200, pushed);
    });

    const result = await pushRecordWithCas(desktopRow.host_id, (stored) => ({
      ...(stored ?? wireRecord({})),
      color: "#ff0000",
    }));

    expect(seen).toHaveLength(2);
    expect(seen[1].revision).toBe(8);
    // Both edits landed: the web's color and every desktop field.
    expect(result.color).toBe("#ff0000");
    expect(result.agent_id).toBe("agent-desktop-enriched");
    expect(result.display_name).toBe("desktop-name");
    expect(result.encrypted_secrets).toBe("ZGVza3RvcC1zZWNyZXRz");
  });

  it("gives up after repeated conflicts", async () => {
    const stored = wireRecord({ revision: 1 });
    vi.stubGlobal("fetch", async () =>
      jsonResponse(409, { detail: { message: "revision conflict", stored } }),
    );

    await expect(
      pushRecordWithCas(stored.host_id, () => wireRecord({})),
    ).rejects.toThrow(/kept conflicting/);
  });
});
