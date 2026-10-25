/**
 * The roster (the multiplayer spec, section 4.2): everyone who may be in the workspace, as the shell pushes it
 * over the socket (`roster_updated`, on connect and whenever presence or a grants file changes), and what the
 * tray reads off each entry: whether they are active, idle, offline, or only invited, and how to say so.
 */

import type { RosterParticipant } from "./records";

/** How long after a page's last input a connected person still counts as active (the multiplayer spec, 4.3). */
export const ACTIVE_INPUT_WINDOW_MS = 60_000;

let participants: RosterParticipant[] = [];

export function getRoster(): RosterParticipant[] {
  return participants;
}

/** Apply a `roster_updated` push: the whole roster, replacing the last one. */
export function applyRoster(next: RosterParticipant[]): void {
  participants = next;
}

export function rosterEntry(key: string): RosterParticipant | null {
  return participants.find((entry) => entry.key === key) ?? null;
}

/** Forget the roster, so a test starts from none. Test-only. */
export function resetRosterForTesting(): void {
  participants = [];
}

/** What an entry is doing, as the card's status line and the strip's fade read it. */
export type PresenceState = "active" | "idle" | "offline" | "invited";

export function presenceStateOf(entry: RosterParticipant, now: Date): PresenceState {
  if (entry.kind === "invited") return "invited";
  if (entry.kind === "workspace_agent") return "active";
  if (!entry.is_connected) return "offline";
  // A visiting agent never reports input: its presence is the gateway's last-seen record, so connected is active.
  if (entry.kind === "visiting_agent") return "active";
  // A page from before input was reported says nothing about it: the person is active while connected (the
  // multiplayer spec, section 11).
  const inputAt = entry.last_input_at === null ? null : Date.parse(entry.last_input_at);
  if (inputAt === null || Number.isNaN(inputAt)) return "active";
  return now.getTime() - inputAt < ACTIVE_INPUT_WINDOW_MS ? "active" : "idle";
}

/** A short age: "4m", "3h", "2d"; "now" under a minute. */
export function shortAge(since: string, now: Date): string {
  const at = Date.parse(since);
  if (Number.isNaN(at)) return "";
  const seconds = Math.max(0, Math.round((now.getTime() - at) / 1000));
  if (seconds < 60) return "now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

/** The card's status line (the multiplayer spec, section 6.3): "Active", "Idle · 4m", "Offline" (with "last seen 3h
 *  ago" when known), or "Invited". */
export function statusLineOf(entry: RosterParticipant, now: Date): string {
  switch (presenceStateOf(entry, now)) {
    case "active":
      return "Active";
    case "idle":
      return entry.last_input_at === null ? "Idle" : `Idle · ${shortAge(entry.last_input_at, now)}`;
    case "offline":
      return entry.last_seen === null ? "Offline" : `Offline · last seen ${shortAge(entry.last_seen, now)} ago`;
    case "invited":
      return "Invited";
  }
}

/** The letters an entry without a picture wears: the initials of its name, up to two. */
export function monogramOf(entry: Pick<RosterParticipant, "display_name" | "email">): string {
  const source = entry.display_name.trim() || entry.email || "";
  const words = source.split(/\s+/).filter((word) => word !== "");
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 1).toUpperCase();
  return (words[0].slice(0, 1) + words[words.length - 1].slice(0, 1)).toUpperCase();
}
