/** A roster entry for tests: a connected person who is not the owner, with every field overridable. */

import type { Participant } from "./Participants";

export function personFixture(key: string, displayName: string, overrides: Partial<Participant> = {}): Participant {
  return {
    key,
    kind: "person",
    display_name: displayName,
    profile_picture_url: null,
    email: null,
    handle: displayName,
    color: "#000",
    is_owner: false,
    is_connected: true,
    last_seen: null,
    last_input_at: null,
    ...overrides,
  };
}

/** A shared workspace's roster for tests: its owner, Josh, and a visitor, Glenn. */
export function ownerAndVisitorRosterFixture(): Participant[] {
  return [personFixture("user:owner-1", "Josh", { is_owner: true }), personFixture("user:glenn-2", "Glenn")];
}
