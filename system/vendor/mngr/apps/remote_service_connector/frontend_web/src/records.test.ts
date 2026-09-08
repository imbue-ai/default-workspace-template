// CAS push behavior against a stubbed fetch: first-push, merge-on-conflict,
// and give-up-after-retries.

import { afterEach, describe, expect, it, vi } from "vitest";
import type { WireRecord } from "./api";
import { pushRecordWithCas, RecordTooNewError } from "./records";

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


describe("forward compatibility", () => {
  it("round-trips unknown record fields through a spread-based mutate", async () => {
    // A newer server added a field this bundle does not know about; the
    // spread-based mutate pattern must carry it through the push untouched.
    const stored = {
      ...wireRecord({ revision: 3 }),
      added_by_a_newer_server: "must-survive",
    } as WireRecord;
    const seen: Array<Record<string, unknown>> = [];
    vi.stubGlobal("fetch", async (_url: string, init?: RequestInit) => {
      const pushed = JSON.parse(String(init?.body)) as Record<string, unknown>;
      seen.push(pushed);
      return jsonResponse(200, pushed);
    });

    await pushRecordWithCas(stored.host_id, (latest) => ({
      ...(latest ?? stored),
      display_name: "renamed",
    }));

    expect(seen).toHaveLength(1);
    expect(seen[0].added_by_a_newer_server).toBe("must-survive");
    expect(seen[0].display_name).toBe("renamed");
  });

  it("refuses to mutate a record whose record_format is too new", async () => {
    // The first PUT conflicts, handing back a stored row written at a newer
    // record_format; the retry must refuse instead of rewriting it.
    const stored = wireRecord({ revision: 5, record_format: 99 });
    vi.stubGlobal("fetch", async () =>
      jsonResponse(409, { detail: { message: "revision conflict", stored } }),
    );

    await expect(
      pushRecordWithCas(stored.host_id, (latest) => ({
        ...(latest ?? stored),
        display_name: "renamed",
      })),
    ).rejects.toThrow(RecordTooNewError);
  });

  it("surfaces the server's record_format_too_new refusal as RecordTooNewError", async () => {
    vi.stubGlobal("fetch", async () =>
      jsonResponse(409, {
        detail: { code: "record_format_too_new", message: "update the app" },
      }),
    );

    await expect(
      pushRecordWithCas(wireRecord({}).host_id, () => wireRecord({})),
    ).rejects.toThrow(RecordTooNewError);
  });
});
