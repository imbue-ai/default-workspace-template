import { afterEach, describe, expect, it } from "vitest";
import {
  applyRoster,
  getRoster,
  monogramOf,
  presenceStateOf,
  resetRosterForTesting,
  rosterEntry,
  shortAge,
  statusLineOf,
} from "./Roster";
import { rosterParticipantRecord } from "../testing/records";

const NOW = new Date("2026-10-25T12:00:00Z");

afterEach(() => {
  resetRosterForTesting();
});

describe("the roster model", () => {
  it("holds the last push, whole, and finds an entry by key", () => {
    applyRoster([rosterParticipantRecord("user:user-bob-4471", { display_name: "Bob" })]);
    expect(getRoster().map((entry) => entry.display_name)).toEqual(["Bob"]);
    expect(rosterEntry("user:user-bob-4471")?.display_name).toBe("Bob");
    expect(rosterEntry("user:nobody")).toBeNull();
    applyRoster([]);
    expect(getRoster()).toEqual([]);
  });
});

describe("presenceStateOf and statusLineOf", () => {
  it("reads active from recent input, idle from stale input, offline from no connection, invited from the kind", () => {
    const active = rosterParticipantRecord("user:a", { is_connected: true, last_input_at: "2026-10-25T11:59:30Z" });
    const idle = rosterParticipantRecord("user:b", { is_connected: true, last_input_at: "2026-10-25T11:56:00Z" });
    const unknown = rosterParticipantRecord("user:c", { is_connected: true, last_input_at: null });
    const offline = rosterParticipantRecord("user:d", { is_connected: false, last_seen: "2026-10-25T09:00:00Z" });
    const invited = rosterParticipantRecord("email:e@x", { kind: "invited" });
    const agent = rosterParticipantRecord("workspace-agent", { kind: "workspace_agent", is_connected: true });
    expect([active, idle, unknown, offline, invited, agent].map((entry) => presenceStateOf(entry, NOW))).toEqual([
      "active",
      "idle",
      "idle",
      "offline",
      "invited",
      "active",
    ]);
    expect(statusLineOf(active, NOW)).toBe("Active");
    expect(statusLineOf(idle, NOW)).toBe("Idle · 4m");
    expect(statusLineOf(unknown, NOW)).toBe("Idle");
    expect(statusLineOf(offline, NOW)).toBe("Offline · last seen 3h ago");
    expect(statusLineOf(rosterParticipantRecord("user:f", { is_connected: false }), NOW)).toBe("Offline");
    expect(statusLineOf(invited, NOW)).toBe("Invited");
  });
});

describe("shortAge and monogramOf", () => {
  it("says now, minutes, hours, or days, and takes up to two initials", () => {
    expect(shortAge("2026-10-25T11:59:40Z", NOW)).toBe("now");
    expect(shortAge("2026-10-25T11:30:00Z", NOW)).toBe("30m");
    expect(shortAge("2026-10-24T12:00:00Z", NOW)).toBe("1d");
    expect(shortAge("not a time", NOW)).toBe("");
    expect(monogramOf({ display_name: "Josh Albrecht", email: null })).toBe("JA");
    expect(monogramOf({ display_name: "Bob", email: null })).toBe("B");
    expect(monogramOf({ display_name: "", email: "carol@example.com" })).toBe("C");
    expect(monogramOf({ display_name: "", email: null })).toBe("?");
  });
});
