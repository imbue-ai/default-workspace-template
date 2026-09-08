/**
 * Shared rendering functions for transcript events.
 * Used by both ChatPanel and SubagentView.
 */

import m from "mithril";
import { MarkdownContent } from "../markdown";
import type { TranscriptEvent, AssistantMessageEvent, ToolResultEvent, ToolCall } from "../models/Response";
import { getEventDetailState, getEventDetailVersion, requestEventDetail } from "../models/Response";
import { getAgentById } from "../models/AgentManager";
import { openProviderChooser } from "../models/Providers";
import { openSubagentTab, startChatOnAccount } from "./DockviewWorkspace";
import { hoverTooltipAttrs } from "./components/hoverTooltip";
import { activityDotClass } from "./components/activityDot";
import { isBlockExpanded, setBlockExpanded } from "./expansion-state";
import type { PermissionResolution } from "./message-classification";
import { isSkillExpansionUserMessage } from "./message-classification";
import { PermissionCard, isFiledPermissionRequest, parsePermissionRequest } from "./permission-card";
import { renderToolBlock, type PayloadState } from "./components/ToolCallBlock";
import { badgeClass } from "./components/Badge";

/** A permission-request tool call's own verdict: its own request id's entry in
 *  `resolutionsByRequestId`, or null while the request awaits a decision (or
 *  when the call is unparseable). */
function resolutionForCall(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  resolutionsByRequestId: ReadonlyMap<string, PermissionResolution>,
): PermissionResolution | null {
  const details = parsePermissionRequest(toolCall, toolResult);
  return (details ? resolutionsByRequestId.get(details.requestId) : undefined) ?? null;
}

// Per-kind user_message rendering lives in user-message-display.ts (the display
// half of the classify/display split). Re-exported here so existing importers --
// conversation-rows, ProgressBlock -- keep their import path.
export { renderUserMessage, StableUserMessage } from "./user-message-display";

/** Build a tool_call_id -> tool_result map, merging skill-expansion
 *  user_messages into the output of their preceding "Skill" tool call so
 *  the SKILL.md body renders inside the same dropdown rather than as a
 *  separate inline chip. */
export function buildToolResultsWithSkillExpansions(events: TranscriptEvent[]): Map<string, ToolResultEvent> {
  const sorted = [...events].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
  const toolResults = new Map<string, ToolResultEvent>();
  for (const e of sorted) {
    if (e.type === "tool_result" && e.tool_call_id) {
      toolResults.set(e.tool_call_id, e);
    }
  }
  // Walk chronologically. Skill tool_call_ids are queued in FIFO order as
  // they appear and matched to skill-expansion user_messages in the same
  // order. A single assistant_message may carry multiple Skill calls; a
  // second assistant_message may queue another Skill call before any
  // expansion for the previous one has arrived. In both cases each
  // expansion must land on the right call, hence a queue rather than a
  // single "most recent" slot.
  const pendingSkillCallIds: string[] = [];
  for (const e of sorted) {
    if (e.type === "assistant_message" && e.tool_calls) {
      for (const tc of e.tool_calls) {
        if (tc.tool_name === "Skill") {
          pendingSkillCallIds.push(tc.tool_call_id);
        }
      }
      continue;
    }
    if (e.type === "user_message" && isSkillExpansionUserMessage(e) && pendingSkillCallIds.length > 0) {
      const targetCallId = pendingSkillCallIds.shift() as string;
      const existing = toolResults.get(targetCallId);
      const expansion = e.content ?? "";
      const baseOutput = existing?.output ?? "";
      const mergedOutput = baseOutput ? `${baseOutput}\n\n${expansion}` : expansion;
      if (existing) {
        toolResults.set(targetCallId, { ...existing, output: mergedOutput });
      } else {
        toolResults.set(targetCallId, {
          timestamp: e.timestamp,
          type: "tool_result",
          event_id: `skill-expansion-${targetCallId}`,
          source: e.source,
          tool_call_id: targetCallId,
          tool_name: "Skill",
          output_chars: mergedOutput.length,
          output: mergedOutput,
          is_error: false,
        });
      }
    }
  }
  return toolResults;
}

/**
 * Hide auth-error turns from the pre-login prefix once login has recovered.
 *
 * A fresh chat with no Claude credentials produces a run of "Not logged in"
 * assistant messages before the user authenticates. Once login succeeds and
 * /welcome is resent, the first visible turn should be the friendly greeting,
 * not the prior failed attempts.
 *
 * Restricted to the PREFIX of the transcript (turns that occurred before any
 * successful assistant message). A mid-session token expiration -- where the
 * user has already had successful exchanges before the auth error -- is left
 * intact, since the user may want to scroll back to see what they were doing.
 */
export function computeAuthErrorHiddenEventIds(events: TranscriptEvent[]): Set<string> {
  const hidden = new Set<string>();

  let firstSuccessIdx = -1;
  for (let i = 0; i < events.length; i++) {
    const ev = events[i];
    if (ev.type === "assistant_message" && ev.is_auth_error !== true) {
      firstSuccessIdx = i;
      break;
    }
  }
  if (firstSuccessIdx === -1) return hidden;

  for (let i = 0; i < firstSuccessIdx; i++) {
    const ev = events[i];
    if (ev.type !== "assistant_message" || ev.is_auth_error !== true) continue;
    hidden.add(ev.event_id);
    for (let j = i - 1; j >= 0; j--) {
      const prev = events[j];
      if (prev.type === "user_message") {
        hidden.add(prev.event_id);
        break;
      }
      if (prev.type === "assistant_message") break;
    }
  }

  return hidden;
}

export function countResolvedToolResults(
  toolCalls: ToolCall[] | undefined,
  toolResults: Map<string, ToolResultEvent>,
): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (toolResults.has(tc.tool_call_id)) count++;
  }
  return count;
}

export function countSubagentCards(toolCalls: ToolCall[] | undefined): number {
  if (!toolCalls) return 0;
  let count = 0;
  for (const tc of toolCalls) {
    if (tc.subagent_metadata) count++;
  }
  return count;
}

/** A cheap content fingerprint of the tool_results this message resolves, so the memo
 *  below repaints when a result is SUPERSEDED in place (same tool_call_id, new content) --
 *  e.g. a codex interrupt's synthetic "Interrupted." result is later replaced by the real
 *  one. Result-presence counting alone misses this (both are "present"). */
function resolvedResultSignature(
  toolCalls: ToolCall[] | undefined,
  toolResults: Map<string, ToolResultEvent>,
): string {
  if (!toolCalls) {
    return "";
  }
  return toolCalls
    .map((tc) => {
      const result = toolResults.get(tc.tool_call_id);
      return result
        ? `${tc.tool_call_id}:${result.is_error}:${result.output_chars}:${result.error_snippet ?? ""}:${
            result.tk_stamp?.length ?? 0
          }`
        : "-";
    })
    .join("|");
}

export function StableAssistantMessage(): m.Component<{
  event: AssistantMessageEvent;
  toolResults: Map<string, ToolResultEvent>;
  agentId: string;
}> {
  let renderedEvent: AssistantMessageEvent | null = null;
  let renderedToolResultCount = 0;
  let renderedSubagentCardCount = 0;
  let renderedResultSignature = "";
  let renderedDetailVersion = -1;
  return {
    onbeforeupdate(vnode) {
      const { event, toolResults, agentId } = vnode.attrs;
      const currentToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      // A subagent card can appear after the message was first rendered: the
      // backend re-broadcasts the parent with subagent_metadata once a running
      // subagent's linkage lands. Repaint when that count grows so the plain
      // tool-call block upgrades to the rich card.
      const currentSubagentCardCount = countSubagentCards(event.tool_calls);
      const currentResultSignature = resolvedResultSignature(event.tool_calls, toolResults);
      return (
        // A supersession replaces the event object in the store (new reference), so a
        // reference change catches an assistant-message text/tool_calls rewrite; the
        // result signature catches a tool_result rewrite. Both are needed since the
        // presence counts alone do not move when content is replaced in place. The
        // detail version catches an on-demand payload arriving for an expanded block.
        event !== renderedEvent ||
        currentToolResultCount !== renderedToolResultCount ||
        currentSubagentCardCount !== renderedSubagentCardCount ||
        currentResultSignature !== renderedResultSignature ||
        getEventDetailVersion(agentId) !== renderedDetailVersion
      );
    },
    view(vnode) {
      const event = vnode.attrs.event;
      const toolResults = vnode.attrs.toolResults;
      const agentId = vnode.attrs.agentId;
      renderedEvent = event;
      renderedToolResultCount = countResolvedToolResults(event.tool_calls, toolResults);
      renderedSubagentCardCount = countSubagentCards(event.tool_calls);
      renderedResultSignature = resolvedResultSignature(event.tool_calls, toolResults);
      renderedDetailVersion = getEventDetailVersion(agentId);

      return m("div", renderAssistantMessageChildren(event, toolResults, agentId));
    },
  };
}

export function renderAssistantMessage(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
): m.Vnode {
  return m(
    "div",
    {
      id: event.event_id,
      class: "message message-assistant mb-5",
      key: event.event_id,
    },
    m(StableAssistantMessage, { event, toolResults, agentId }),
  );
}

export function renderSubagentCard(toolCall: ToolCall, agentId: string, isRunning: boolean): m.Vnode {
  const metadata = toolCall.subagent_metadata;
  // Description and agent type come from the tool call itself, so the card renders fully
  // even before the subagent session is linked; fall back to metadata if the tool input
  // fields are absent (older events).
  const description = toolCall.description || metadata?.description || "Sub-agent";
  const agentType = toolCall.subagent_type || metadata?.agent_type || "";
  const sessionId = metadata?.session_id;

  // The header status indicator communicates whether the sub-agent is still working: a pulsing
  // green dot while the Agent call is in flight (no tool result yet), switching to a muted
  // checkmark -- like a completed progress step -- once the sub-agent finishes. On completion the
  // whole card also drops its green accent for neutral grey, since green reads as "active".
  const statusIndicator = isRunning
    ? m("span", {
        class: `subagent-card-status-dot subagent-card-status-dot--running ${activityDotClass("h-[7px] w-[7px]")}`,
        "aria-label": "Sub-agent is working",
        ...hoverTooltipAttrs("Working"),
      })
    : m(
        "svg",
        {
          class: "subagent-card-status-check shrink-0 text-secondary",
          width: 16,
          height: 16,
          viewBox: "0 0 16 16",
          fill: "none",
          "aria-label": "Sub-agent finished",
          ...hoverTooltipAttrs("Finished"),
        },
        // Same filled-circle-with-check mark used for a done step in the progress timeline.
        m.trust(
          '<circle cx="8" cy="8" r="7" fill="currentColor"/>' +
            '<path d="M4.5 8L7 10.5L11.5 6" stroke="white" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/>',
        ),
      );

  const cardTone = isRunning
    ? "border-accent bg-accent-light"
    : "subagent-card--done border-default bg-surface-secondary";
  const linkBase =
    "shrink-0 whitespace-nowrap text-(length:--font-size-body) transition-[color] duration-(--dur-base)";
  return m(
    "div",
    {
      class: `subagent-card my-[0.75em] flex items-center justify-between gap-3 rounded-md border px-4 py-3 ${cardTone}`,
    },
    [
      m("div", { class: "subagent-card-header flex min-w-0 flex-1 items-center gap-2" }, [
        statusIndicator,
        m(
          "span",
          { class: "subagent-card-description truncate text-(length:--font-size-body) font-medium text-primary" },
          description,
        ),
        agentType ? m("span", { class: badgeClass("accent", { mono: true, extra: "shrink-0" }) }, agentType) : null,
      ]),
      // The click-through needs the subagent session_id, which only arrives once the call is
      // linked. The label stays "View conversation" throughout so it doesn't flip-flop; before
      // the session is known it renders as a muted, non-clickable placeholder (there is no
      // conversation to open yet), becoming an active link the moment linkage lands.
      sessionId
        ? m(
            "a",
            {
              class: `subagent-card-link ${linkBase} text-accent no-underline hover:text-accent-hover hover:underline`,
              href: "javascript:void(0)",
              onclick(e: Event) {
                e.preventDefault();
                e.stopPropagation();
                openSubagentTab(agentId, sessionId, description);
              },
            },
            "View conversation",
          )
        : m(
            "span",
            {
              class: `subagent-card-link subagent-card-link--pending ${linkBase} cursor-default text-faint hover:text-secondary`,
            },
            "View conversation",
          ),
    ],
  );
}

export function renderToolCallBlock(
  toolCall: ToolCall,
  toolResult: ToolResultEvent | null,
  agentId: string,
  assistantEventId: string,
): m.Vnode {
  // The harness's parser already worked out what this call should read as -- for
  // codex that means unwrapping an `exec` whose real operation is buried in a JS
  // argument, which is not something this view should have to know. Falls back to
  // the tool name for events parsed before the labels existed.
  const isError = toolResult?.is_error === true;
  // Keyed by the tool call's stable id so the expansion survives the row
  // unmounting and remounting (virtualization) or re-rendering (streaming).
  const expansionKey = `tc:${toolCall.tool_call_id}`;

  // Events are payload-free on the wire: the full input/output are fetched on demand
  // (cached frontend-side for the page session) the first time the row is expanded.
  const requestPayloads = () => {
    if (toolCall.input_chars > 0) {
      requestEventDetail(agentId, assistantEventId);
    }
    if (toolResult && toolResult.output_chars > 0 && !toolResult.event_id.startsWith("skill-expansion-")) {
      requestEventDetail(agentId, toolResult.event_id);
    }
  };
  if (isBlockExpanded(expansionKey)) {
    // Re-request on every expanded render: a no-op when cached or in flight, and it
    // heals an entry dropped by a transient fetch failure.
    requestPayloads();
  }

  let inputText = "";
  let inputState: PayloadState = "loaded";
  if (toolCall.input_chars > 0 || Boolean(toolCall.tk_command)) {
    const inputDetail = getEventDetailState(agentId, assistantEventId);
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
    // A frontend-synthesized skill expansion carries its body inline; a real result's
    // output is fetched. When both exist (a Skill call with real output plus its
    // expansion), the fetched output leads and the expansion follows.
    const isFetchable = toolResult.output_chars > 0 && !toolResult.event_id.startsWith("skill-expansion-");
    const fetched = isFetchable ? getEventDetailState(agentId, toolResult.event_id) : undefined;
    const inline = toolResult.output ?? "";
    if (fetched?.state === "loaded") {
      outputText = [fetched.detail.output ?? "", inline].filter((part) => part).join("\n\n");
    } else if (isFetchable) {
      outputState = fetched?.state ?? "loading";
    } else {
      outputText = inline;
    }
  }

  return renderToolBlock({
    headerText: toolCall.header_label || `Tool: ${toolCall.tool_name}`,
    inputText,
    outputText,
    inputState,
    outputState,
    isError,
    errorSnippet: toolResult?.error_snippet ?? undefined,
    // The markdown rhythm: the same vertical slot a paragraph-adjacent block
    // gets in the assistant flow.
    extra: "my-[0.25em]",
    expansionKey,
    // Kick off the payload fetches; the redraw renders the loading note (or
    // the cached payload) into the just-revealed details section.
    onExpand: () => {
      requestPayloads();
      m.redraw();
    },
  });
}

/** The "sign in again" affordance under an auth failure.
 *
 * Resolves the chat's own account from its `account` label, so the chooser opens ON that
 * account and re-authenticates it in place -- every chat bound to it recovers. Without the
 * label (a chat from before accounts, say) it opens the chooser plainly, which is still the
 * right destination.
 */
const REAUTH_ACTION_CLASS = "message-api-error-action cursor-pointer text-accent underline hover:text-accent-hover";

function renderReauthAction(agentId: string): m.Children {
  const accountId = getAgentById(agentId)?.labels?.account ?? "";
  return m("div", { class: "message-api-error-note mt-[0.4em] text-[0.85em] text-faint" }, [
    "This provider is no longer working. ",
    m(
      "button",
      {
        type: "button",
        class: REAUTH_ACTION_CLASS,
        onclick: () => openProviderChooser(accountId ? { accountId } : {}),
      },
      "Sign in again",
    ),
    // Two ways out, because only the first one revives THIS conversation: a chat binds to its
    // account when it is created and nothing rebinds it, so "switch provider" cannot mean
    // moving this chat -- it means starting a fresh one somewhere that works. Both are offered
    // because the right choice depends on whether the credential is fixable, which the user
    // knows and we do not: an expired login is, a spent quota mostly is not.
    " or ",
    m(
      "button",
      {
        type: "button",
        class: REAUTH_ACTION_CLASS,
        onclick: () => openProviderChooser({ onSignedIn: (chosen) => startChatOnAccount(chosen) }),
      },
      "switch to another provider",
    ),
    ".",
  ]);
}

/** The grey note shown under a provider-fault API error: the failure is the model
 *  provider's, not ours. Wording nudged by kind; every harness's provider faults
 *  land here since they stamp the same is_provider_fault flag. */
function providerFaultNote(kind: string | null): string {
  const cause =
    kind === "api_error" ? "the model provider's servers hit an error" : "the model provider's servers are overloaded";
  return `This isn't Minds' fault -- ${cause}. Try again in a moment.`;
}

/** The tiny muted "thinking" toggle atop an assistant message whose harness recorded
 *  readable reasoning. The text itself loads on demand (payload-free wire) and expands
 *  inline; the toggle is deliberately minimal -- most readers never open it. */
function renderThinkingDisclosure(event: AssistantMessageEvent, agentId: string): m.Vnode {
  const expansionKey = `think:${event.event_id}`;
  const isExpanded = isBlockExpanded(expansionKey);
  if (isExpanded) {
    requestEventDetail(agentId, event.event_id);
  }
  // The body is always in the DOM (CSS reveals it under --expanded), so the click
  // handler's direct class toggle is all a collapse needs -- no re-render.
  const detail = getEventDetailState(agentId, event.event_id);
  let body: m.Vnode;
  if (detail?.state === "loaded") {
    body = m("div", { class: "thinking-body" }, detail.detail.thinking ?? "");
  } else if (detail?.state === "unavailable") {
    body = m("div", { class: "thinking-body thinking-body--note" }, "No longer available");
  } else {
    body = m("div", { class: "thinking-body thinking-body--note" }, "Loading\u2026");
  }
  return m("div", { class: `thinking-disclosure${isExpanded ? " thinking-disclosure--expanded" : ""}` }, [
    m(
      "button",
      {
        type: "button",
        class: "thinking-toggle",
        onclick(e: Event) {
          const disclosure = (e.currentTarget as HTMLElement).parentElement;
          if (disclosure) {
            // Toggle the DOM directly (memoized wrappers skip re-patching)
            // AND record it so a fresh mount renders in the same state.
            const nowExpanded = disclosure.classList.toggle("thinking-disclosure--expanded");
            setBlockExpanded(expansionKey, nowExpanded);
            if (nowExpanded) {
              // Kick off the fetch; the redraw renders the loading note (or the
              // cached text) into the just-revealed body.
              requestEventDetail(agentId, event.event_id);
              m.redraw();
            }
          }
        },
      },
      [m("span", { class: "tool-call-chevron" }, "\u25B8"), "thinking"],
    ),
    body,
  ]);
}

/**
 * Render the children (text + tool calls) of an assistant message.
 * Used by both the stable (memoized) and simple assistant message renderers.
 */
export function renderAssistantMessageChildren(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
  resolutionsByRequestId: ReadonlyMap<string, PermissionResolution> = new Map(),
): m.Children[] {
  const textContent = event.text || "";
  const toolCalls = event.tool_calls || [];

  const children: m.Children[] = [];
  if (event.has_thinking) {
    children.push(renderThinkingDisclosure(event, agentId));
  }
  if (textContent) {
    if (event.is_api_error || event.is_auth_error) {
      // A model API error: render the failure text in light red, and for a
      // provider-side fault (5xx / overloaded) add a grey "not Minds' fault" note.
      //
      // An auth error gets a button as well. It is the one failure the user can actually
      // fix, and the fix is not obvious from the provider's wording -- which is usually a
      // raw 401 body. Inline rather than a modal, so it waits to be clicked instead of
      // throwing a sign-in screen over whatever the user was doing.
      children.push(
        m("div", { class: "message-api-error rounded-md bg-danger/8 px-[0.75em] py-[0.5em] text-danger" }, [
          m(MarkdownContent, {
            content: textContent,
            requestedAt: event.timestamp,
            expansionKeyPrefix: event.event_id,
          }),
          event.is_provider_fault
            ? m(
                "div",
                { class: "message-api-error-note mt-[0.4em] text-[0.85em] text-faint" },
                providerFaultNote(event.api_error_kind),
              )
            : null,
          event.is_auth_error ? renderReauthAction(agentId) : null,
        ]),
      );
    } else {
      children.push(
        m(MarkdownContent, { content: textContent, requestedAt: event.timestamp, expansionKeyPrefix: event.event_id }),
      );
    }
  }
  for (const toolCall of toolCalls) {
    // Render the rich card as soon as we have the Agent call's description (from the tool
    // input), even before its subagent session is linked; the card shows a non-clickable
    // "Running…" state until subagent_metadata.session_id arrives.
    if (toolCall.tool_name === "Agent" && (toolCall.subagent_metadata || toolCall.description)) {
      // The Agent call's tool result arrives only when the sub-agent finishes, so its
      // absence is our signal that the sub-agent is still actively working.
      const subagentRunning = !toolResults.has(toolCall.tool_call_id);
      children.push(renderSubagentCard(toolCall, agentId, subagentRunning));
      continue;
    }
    const result = toolResults.get(toolCall.tool_call_id) ?? null;
    // A permission request renders as its own card (the request, a verdict or
    // button, and the raw call) rather than a generic tool block.
    // Gated on the input-only predicate so the card shows even while the request
    // is still pending -- the same signal the timeline walk uses to lift it out
    // of its step. The resolution (once the user decides) comes from the walk,
    // looked up by this call's own request id so a message batching more than
    // one permission request resolves each of its cards independently.
    if (isFiledPermissionRequest(toolCall, result)) {
      const resolution = resolutionForCall(toolCall, result, resolutionsByRequestId);
      children.push(
        m(PermissionCard, { toolCall, toolResult: result, resolution, agentId, assistantEventId: event.event_id }),
      );
      continue;
    }
    children.push(renderToolCallBlock(toolCall, result, agentId, event.event_id));
  }
  return children;
}

/**
 * Render a permission-break timeline item: the issuing assistant message (its
 * prose plus the permission card), with the user's granted/denied verdict
 * threaded into the card. Used by the timeline renderers for the `permission`
 * item; goes direct (not via the memoized StableAssistantMessage) so the
 * resolution reaches the card.
 */
export function renderPermissionItem(
  event: AssistantMessageEvent,
  toolResults: Map<string, ToolResultEvent>,
  agentId: string,
  resolutionsByRequestId: ReadonlyMap<string, PermissionResolution>,
  domId: string = event.event_id,
): m.Vnode {
  // ``domId`` defaults to the event id but a top-level permission row passes its
  // row key (``perm-<event_id>``) so the rendered root's ``id`` matches the key
  // the virtualization measures by -- otherwise the measured height is cached
  // under the bare event id and never read, leaving the row stuck at its estimate
  // and shifting content each time it crosses the window edge.
  return m(
    "div",
    { id: domId, class: "message message-assistant mb-5", key: event.event_id },
    renderAssistantMessageChildren(event, toolResults, agentId, resolutionsByRequestId),
  );
}
