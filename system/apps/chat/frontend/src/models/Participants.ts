/**
 * Who this page is, and who everyone else is (the multiplayer spec, sections 4 and 5.4): the page's own
 * participant key from ``GET /api/me``, and the roster from ``GET /api/participants`` (the shell's, served by the
 * chat app with every picture on this origin). Both load as the page boots and again whenever the page comes back
 * into view or focus, or someone's mentions change (a few seconds of cache folds a burst of those into one read),
 * so a person who joined after the page loaded, and the owner's key once the workspace is shared, are drawn right;
 * a transcript turn is drawn as the viewer's own, or as another person's with their picture and name, from these.
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
/** When the roster was last read, so the reads a visibility change, a focus, and a mentions push ask for within
 *  moments of each other are one. */
let loadedAt: number | null = null;
/** The one retry a failed load has scheduled, while it is pending: a second failure before it fires (a focus or a
 *  mentions push while the app is unreachable asks again) adds no retry of its own. */
let retryTimer: ReturnType<typeof setTimeout> | null = null;
export const RETRY_DELAY_MS = 30_000;
export const PARTICIPANTS_FRESH_MS = 5_000;

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

/** The workspace agent's name and picture as one string, empty before the roster lands: what a memoized
 *  assistant row compares to know the header over its bubbles has to be drawn again. */
export function workspaceAgentSignature(): string {
  const agent = getWorkspaceAgent();
  return agent === null ? "" : `${agent.display_name}|${agent.profile_picture_url ?? ""}`;
}

/** The display name of an author key: the roster's, else the name the turn carried, else the key. */
export function participantName(key: string, fallbackName: string | null): string {
  return getParticipant(key)?.display_name ?? fallbackName ?? key;
}

/** Whether a key is the owner's, under either of the owner's keys (the spec's S12): the synthetic ``owner`` of an
 *  unshared workspace, or the roster entry marked as the owner. */
export function isOwnerKey(key: string): boolean {
  return key === OWNER_KEY || (getParticipant(key)?.is_owner ?? false);
}

/** The owner's two keys read as one owner (the multiplayer spec, section 11): an annotation written before the
 *  workspace was shared stays under the synthetic ``owner``, so that key folds to the roster owner's -- itself on
 *  an unshared workspace -- and every other key is its own. */
export function foldOwnerKey(key: string): string {
  if (key !== OWNER_KEY) return key;
  return getOwner()?.key ?? key;
}

/** A picture the roster names by an origin-relative path (every picture, since this app serves them all) is
 *  reached under the page's base path, as every other API URL is; an absolute URL is left alone. */
function withPictureOnThisOrigin(entry: Participant): Participant {
  const url = entry.profile_picture_url;
  return url !== null && url.startsWith("/") ? { ...entry, profile_picture_url: apiUrl(url) } : entry;
}

/**
 * Load the page's own key and the roster, or read them again once the last read is older than
 * ``PARTICIPANTS_FRESH_MS``; calls within that window, and calls while a load runs, share the one load. A load
 * that fails is warned about and leaves the page as it was (an owner's page, every unannotated turn its own,
 * before any load landed) until the next attempt, which the failure itself schedules after ``RETRY_DELAY_MS``,
 * one at a time: a failure while a retry is pending schedules no second one.
 */
export function ensureParticipants(): Promise<void> {
  if (loading !== null) return loading;
  if (ownParticipant !== null && loadedAt !== null && Date.now() - loadedAt < PARTICIPANTS_FRESH_MS) {
    return Promise.resolve();
  }
  loading = Promise.all([
    m.request<{ participant: string }>({ method: "GET", url: apiUrl("/api/me") }),
    m.request<{ participants: Participant[] }>({ method: "GET", url: apiUrl("/api/participants") }),
  ])
    .then(([me, roster]) => {
      ownParticipant = me.participant;
      participants = roster.participants.map(withPictureOnThisOrigin);
      loadedAt = Date.now();
      m.redraw();
    })
    .catch((error: unknown) => {
      console.warn("Failed to load the page's participant and the roster", error);
      if (retryTimer !== null) return;
      retryTimer = setTimeout(() => {
        retryTimer = null;
        void ensureParticipants();
      }, RETRY_DELAY_MS);
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
  loadedAt = own === null ? null : Date.now();
  if (retryTimer !== null) clearTimeout(retryTimer);
  retryTimer = null;
}
