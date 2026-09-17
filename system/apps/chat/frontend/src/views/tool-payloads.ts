/**
 * Resolving a tool call's input and output for display.
 *
 * Events are payload-free on the wire: a call carries only `input_chars` and a
 * result only `output_chars`, and the text itself is fetched on demand (cached
 * for the page session) the first time a reader opens the call. So "what text
 * does this call show" is never a plain field read -- it is a small state
 * machine over what has been fetched, what rides the event anyway, and what the
 * backend no longer holds.
 *
 * Lives apart from any one view because both things that show a tool call need
 * it: the inline chips' detail panel (views/ToolChipGroup.ts) and the
 * collapsible block that markdown fences and system chips still use.
 */

import type { ToolCall, ToolResultEvent } from "../models/Response";
import { getEventDetailState, requestEventDetail } from "../models/Response";
import type { PayloadState } from "./ToolCallBlock";

export interface ToolPayloads {
  inputText: string;
  inputState: PayloadState;
  outputText: string;
  outputState: PayloadState;
  /** Kick off the fetches for whatever is not in hand. A no-op when the payload
   *  is cached or already in flight, and it heals an entry dropped by a
   *  transient failure, so callers may call it on every expanded render. */
  requestPayloads: () => void;
}

/** A frontend-synthesized skill expansion carries its body inline; a real
 *  result's output is fetched. */
function isFetchableResult(toolResult: ToolResultEvent): boolean {
  return toolResult.output_chars > 0 && !toolResult.event_id.startsWith("skill-expansion-");
}

export function resolveToolPayloads(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  chatId: string,
  assistantEventId: string,
): ToolPayloads {
  const requestPayloads = (): void => {
    if (toolCall.input_chars > 0) {
      requestEventDetail(chatId, assistantEventId);
    }
    if (toolResult && isFetchableResult(toolResult)) {
      requestEventDetail(chatId, toolResult.event_id);
    }
  };

  let inputText = "";
  let inputState: PayloadState = "loaded";
  if (toolCall.input_chars > 0 || Boolean(toolCall.tk_command)) {
    const inputDetail = getEventDetailState(chatId, assistantEventId);
    if (inputDetail?.state === "loaded") {
      inputText = inputDetail.detail.inputs_by_tool_call_id[toolCall.tool_call_id] ?? toolCall.tk_command ?? "";
    } else if (toolCall.input_chars === 0) {
      // Only the stamped tk command exists (nothing to fetch).
      inputText = toolCall.tk_command ?? "";
    } else {
      inputState = inputDetail?.state ?? "loading";
    }
  }

  let outputText = "";
  let outputState: PayloadState = "loaded";
  if (toolResult) {
    // When both exist (a Skill call with real output plus its expansion), the
    // fetched output leads and the expansion follows.
    const fetchable = isFetchableResult(toolResult);
    const fetched = fetchable ? getEventDetailState(chatId, toolResult.event_id) : undefined;
    const inline = toolResult.output ?? "";
    if (fetched?.state === "loaded") {
      outputText = [fetched.detail.output ?? "", inline].filter((part) => part).join("\n\n");
    } else if (fetchable) {
      outputState = fetched?.state ?? "loading";
    } else {
      outputText = inline;
    }
  }

  return { inputText, inputState, outputText, outputState, requestPayloads };
}
