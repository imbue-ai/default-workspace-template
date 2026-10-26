/**
 * Who a web look-up found for the user (the backend's ``IdentityMatch``, at ``/api/identity-matches``),
 * shown as an "Is this you?" popup until they answer.
 *
 * The onboarding agent runs the look-up in the background and files what it found with the chat app,
 * which pushes every change over the chat WebSocket (``identity_match_updated``) and sends the pending
 * ones to a page that connects later. The user's pick, or "None of these", goes back through the two verb
 * routes, and the chat app passes it to the agent as a message in the user's words.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface IdentityCandidate {
  name: string;
  summary: string;
  source_url: string;
  source_title: string;
}

export type IdentityMatchStatus = "pending" | "confirmed" | "rejected" | "superseded";

export interface IdentityMatch {
  match_id: string;
  chat_id: string;
  searched_for: string;
  candidates: IdentityCandidate[];
  status: IdentityMatchStatus;
  chosen_index: number | null;
}

// The pending match for each chat: at most one, since a newer look-up supersedes the last.
const pendingByChat = new Map<string, IdentityMatch>();
// Matches the user put aside for now (Escape, or a press outside the popup). Still pending on the
// backend, so a reload shows them again.
const setAsideMatchIds = new Set<string>();

/** Take in one pushed match: a pending one is shown for its chat, any other state closes it. */
export function applyIdentityMatchUpdate(match: IdentityMatch): void {
  if (match.status === "pending") {
    pendingByChat.set(match.chat_id, match);
    return;
  }
  if (pendingByChat.get(match.chat_id)?.match_id === match.match_id) pendingByChat.delete(match.chat_id);
}

/** Drop what this page holds, before a (re)connect's replay sends the pending matches again. */
export function forgetIdentityMatches(): void {
  pendingByChat.clear();
}

/** The look-up waiting for this chat's answer, unless the user put it aside; null when there is none. */
export function getPendingIdentityMatch(chatId: string): IdentityMatch | null {
  const match = pendingByChat.get(chatId);
  if (match === undefined || setAsideMatchIds.has(match.match_id)) return null;
  return match;
}

/** Hide a match on this page without answering it. */
export function setIdentityMatchAside(matchId: string): void {
  setAsideMatchIds.add(matchId);
}

async function answer(matchId: string, verb: "confirm" | "reject", body?: object): Promise<void> {
  const answered = await m.request<IdentityMatch>({
    method: "POST",
    url: apiUrl(`/api/identity-matches/:matchId/${verb}`),
    params: { matchId },
    body,
  });
  applyIdentityMatchUpdate(answered);
}

/** Tell the agent the candidate at ``candidateIndex`` is the user. */
export function confirmIdentityMatch(matchId: string, candidateIndex: number): Promise<void> {
  return answer(matchId, "confirm", { candidate_index: candidateIndex });
}

/** Tell the agent none of the candidates is the user. */
export function rejectIdentityMatch(matchId: string): Promise<void> {
  return answer(matchId, "reject");
}

/** The site a candidate's page is on, for the popup's source line: the host without a leading "www.". */
export function sourceSite(sourceUrl: string): string {
  try {
    return new URL(sourceUrl).hostname.replace(/^www\./, "");
  } catch {
    return sourceUrl;
  }
}
