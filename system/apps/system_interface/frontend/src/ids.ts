// A chat's stable identity vs the id of the mngr agent currently backing it.
// Today the two hold the same value (a chat's id is its first agent's id), but
// they are different concepts: when a chat's backing agent is later replaced
// (harness switching), the ChatId stays fixed while the AgentId changes. The
// brands are erased at compile time -- map keys, localStorage keys, and JSON
// payloads are byte-identical -- but mixing the two in code is a type error.
// Cast only at boundaries (the WS payload fold, panel params, URL builders);
// everywhere else the brand flows through.

declare const chatIdBrand: unique symbol;
export type ChatId = string & { readonly [chatIdBrand]: true };

declare const agentIdBrand: unique symbol;
export type AgentId = string & { readonly [agentIdBrand]: true };

export function asChatId(value: string): ChatId {
  return value as ChatId;
}

export function asAgentId(value: string): AgentId {
  return value as AgentId;
}
