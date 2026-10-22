/**
 * Who this page is, and who everyone else is (the multiplayer spec, sections 4 and 5.4): the page's own
 * participant key from ``GET /api/me``, and the roster from ``GET /api/participants`` (the shell's, served by the
 * chat app with the workspace agent's picture on this origin). Both load once per page; a transcript turn is drawn
 * as the viewer's own, or as another person's with their picture and name, from these.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

/** What a roster entry is. */
export type ParticipantKind = "person" | "invited" | "visiting_agent" | "workspace_agent";

/** One entry of the roster, as the shell answers it. */
export interface Participant {
  key: string;
  kind: ParticipantKind;
  display_name: string;
  profile_picture_url: string | null;
  email: string | null;
  handle: string;
  color: string;
  is_owner: boolean;
  is_connected: boolean;
  last_seen: string | null;
  last_input_at: string | null;
}

/** The key of the owner of a workspace that has not shared (the spec's S12). */
export const OWNER_KEY = "owner";
/** The workspace's own agent's key. */
export const WORKSPACE_AGENT_KEY = "workspace-agent";

let ownParticipant: string | null = null;
let participants: Participant[] = [];
let loading: Promise<void> | null = null;
export const RETRY_DELAY_MS = 30_000;

/** The page's own participant key, or null before ``/api/me`` has answered. */
export function getOwnParticipant(): string | null {
  return ownParticipant;
}

/** Whether the page's viewer is the workspace's owner, under either of the owner's keys. */
export function isViewerOwner(): boolean {
  if (ownParticipant === null) return false;
  if (ownParticipant === OWNER_KEY) return true;
  return participants.some((entry) => entry.key === ownParticipant && entry.is_owner);
}

/** The roster as this page last saw it. */
export function getParticipants(): Participant[] {
  return participants;
}

export function getParticipant(key: string): Participant | null {
  return participants.find((entry) => entry.key === key) ?? null;
}

/** The owner's roster entry, if the roster has landed. */
export function getOwner(): Participant | null {
  return participants.find((entry) => entry.is_owner) ?? null;
}

/** The workspace's own agent: the name of the workspace and the face the avatar wears. */
export function getWorkspaceAgent(): Participant | null {
  return getParticipant(WORKSPACE_AGENT_KEY);
}

/** The display name of an author key: the roster's, else the name the turn carried, else the key. */
export function participantName(key: string, fallbackName: string | null): string {
  return getParticipant(key)?.display_name ?? fallbackName ?? key;
}

/**
 * Load the page's own key and the roster once; later calls share the first load. A load that fails is warned
 * about and leaves the page as an owner's page would be (every unannotated turn its own) until the next attempt,
 * which the failure itself schedules after ``RETRY_DELAY_MS``: nothing else asks again once the page has booted.
 */
export function ensureParticipants(): Promise<void> {
  if (ownParticipant !== null) return Promise.resolve();
  if (loading !== null) return loading;
  loading = Promise.all([
    m.request<{ participant: string }>({ method: "GET", url: apiUrl("/api/me") }),
    m.request<{ participants: Participant[] }>({ method: "GET", url: apiUrl("/api/participants") }),
  ])
    .then(([me, roster]) => {
      ownParticipant = me.participant;
      participants = roster.participants;
      m.redraw();
    })
    .catch((error: unknown) => {
      console.warn("Failed to load the page's participant and the roster", error);
      setTimeout(() => void ensureParticipants(), RETRY_DELAY_MS);
    })
    .finally(() => {
      loading = null;
    });
  return loading;
}

/** Make the page a given participant's, over a given roster, without a request. Test-only. */
export function setParticipantsForTesting(own: string | null, roster: Participant[]): void {
  ownParticipant = own;
  participants = roster;
  loading = null;
}
