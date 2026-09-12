/**
 * Agent discovery -- compatibility layer.
 * Delegates to the chats model for state, kept for plugin/hook backward compatibility.
 */

import { getChats as getListedChats, type ChatSnapshot } from "./Chats";

export interface Agent {
  id: string;
  name: string;
  state: string;
}

// Keep Conversation interface for hook compatibility
export interface Conversation {
  id: string;
  name: string;
  model: string;
  latest_response_datetime_utc: string | null;
}

function toAgent(a: ChatSnapshot): Agent {
  return { id: a.id, name: a.name, state: a.state };
}

export function getAgents(): Agent[] {
  return getListedChats().map(toAgent);
}

export function getAgentsLoaded(): boolean {
  return true;
}

export function getLoadingError(): string | null {
  return null;
}

export async function fetchAgents(): Promise<void> {
  // No-op: agent state comes from the WebSocket via the chats model
}

// Compatibility shim for hooks/slots that expect conversations
export function getConversations(): Conversation[] {
  return getListedChats().map((a) => ({
    id: a.id,
    name: a.name,
    model: a.state,
    latest_response_datetime_utc: null,
  }));
}

// Keep fetchConversations as an alias for fetchAgents
export const fetchConversations = fetchAgents;
