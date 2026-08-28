import m from "mithril";
import { backdropDismissAttrs } from "./modalBackdrop";
import {
  clearComposerAttachments,
  getComposerAttachments,
  getReadyAttachmentPaths,
  hasReadyAttachments,
  removeComposerAttachment,
  restoreComposerAttachments,
  uploadFilesToComposer,
  waitForComposerUploads,
} from "../models/ComposerAttachments";
import type { ComposerAttachment } from "../models/ComposerAttachments";
import { buildMessageWithAttachments, formatFileSize } from "../models/attachments";
import { drainToComposer, sendMessage } from "../models/Response";
import { addOutgoing, clearOutgoing, dropOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { describeRequestError } from "../models/request-error";
import { openAgentAuth } from "../models/AgentAuth";
import { ensureHarnessCatalogs, findComposerPopup, getHarnessCatalog } from "../models/HarnessCatalog";
import { getAgentById } from "../models/AgentManager";
import { isWorkingActivityState } from "./ActivityIndicator";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon, stopIcon } from "./icons";
import { Button } from "./Button";
import { MODAL_MESSAGE_CLASS, MODAL_TITLE_CLASS } from "./Modal";

const MAX_TEXTAREA_HEIGHT_PX = 200;

/* ── Styling ──────────────────────────────────────────────────────────────────
 * Utilities in the markup; the message-input and composer-attachment class
 * names stay as bare markers (the e2e tests drive the textbox and send button
 * by them). Attachment status looks are resolved in code, one utility per
 * property. */

/** The composer card. The two-layer shadows are design-system-exceptions: a
 *  unique upward-cast composer shadow (negative y, Notion-charcoal base) with
 *  an accent-tinted glow on focus, which no elevation-scale value expresses;
 *  the border/shadow transition runs its two properties at different speeds,
 *  hence the arbitrary transition property. */
const INPUT_BOX_CLASS =
  "message-input-box flex flex-col rounded-xl border bg-composer " +
  "shadow-[0_-4px_20px_rgba(55,53,47,0.06),0_-1px_6px_rgba(55,53,47,0.04)] " +
  "[transition:border-color_150ms,box-shadow_var(--dur-slow)] focus-within:border-accent " +
  "focus-within:shadow-[0_-4px_24px_rgba(47,107,79,0.08),0_-1px_8px_rgba(47,107,79,0.06)]";

const ATTACHMENT_DETAIL_BASE = "composer-attachment-detail text-(length:--font-size-helper)";

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

function messageTextKey(agentId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${agentId}`;
}

// Blocks handed back to the composer from OUTSIDE this component (a native shoulder tap whose combined
// resend failed -- see QueuedMessageView), keyed by agent. The composer's own ``messageText`` is a
// per-instance closure, so a sibling view cannot merge into it directly; it drops the block here and
// redraws, and the composer applies it on its next view pass (prepend, then persist), the same
// merge-not-drop rule Stop's drain-to-composer uses. Persisted to localStorage regardless, so the text
// survives even if the composer is not currently mounted -- never swallowed (contract A1a).
const pendingComposerPrepends = new Map<string, string>();

/** Hand ``block`` back to ``agentId``'s composer (prepended above any draft), from a sibling view. */
export function prependToComposer(agentId: string, block: string): void {
  if (!block) {
    return;
  }
  const existingDraft = localStorage.getItem(messageTextKey(agentId)) ?? "";
  const merged = existingDraft.trim().length === 0 ? block : `${block}\n\n${existingDraft}`;
  localStorage.setItem(messageTextKey(agentId), merged);
  pendingComposerPrepends.set(agentId, merged);
  m.redraw();
}

function autoResizeTextarea(textarea: HTMLTextAreaElement): void {
  textarea.style.height = "auto";
  textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  textarea.style.overflowY = textarea.scrollHeight > MAX_TEXTAREA_HEIGHT_PX ? "auto" : "hidden";
}

function imageFilesFromClipboard(clipboardData: DataTransfer | null): File[] {
  if (clipboardData === null) {
    return [];
  }
  const files: File[] = [];
  for (const item of Array.from(clipboardData.items)) {
    if (item.kind === "file") {
      const file = item.getAsFile();
      if (file !== null) {
        files.push(file);
      }
    }
  }
  return files;
}

export function MessageInput(): m.Component<{ agentId: string | null }> {
  let messageText = "";
  let currentAgentId: string | null = null;
  let messageTextareaElement: HTMLTextAreaElement | null = null;
  // Set instead of sending when the user types one of the harness's declared
  // auth commands (the `open_auth` composer popup). Delivered raw, /login or
  // /logout would run the harness's own auth flow inside the agent's terminal
  // (or reach the model as prose), bypassing the managed agent-auth surface.
  let interceptedAuthCommand: string | null = null;
  // A slash command the chat declines to deliver, because it would change the agent's terminal
  // rather than start a turn. It still works from that terminal, which the notice says.
  let declinedSlashCommand: { command: string; body: string | null } | null = null;
  let fileInputElement: HTMLInputElement | null = null;
  let isInterruptInFlight = false;

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
  }

  // The declined-command notice has nothing focusable to hang an onkeydown off, so Escape comes
  // from a document listener while it is open (as the image lightbox does). Stable reference, for
  // the same reason as the dropdown handler below.
  function handleDeclinedNoticeKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      declinedSlashCommand = null;
      m.redraw();
    }
  }

  function renderComposerAttachment(agentId: string, attachment: ComposerAttachment): m.Vnode {
    const isReadyImage = attachment.status === "ready" && attachment.isImage && attachment.uploaded !== undefined;
    const thumbnail = isReadyImage
      ? m("img", {
          class: "composer-attachment-thumb h-9 w-9 shrink-0 rounded-md object-cover",
          src: attachment.uploaded?.url,
          alt: attachment.fileName,
        })
      : m(
          "span",
          {
            class: "composer-attachment-icon inline-flex h-9 w-9 shrink-0 items-center justify-center text-secondary",
          },
          attachment.status === "uploading"
            ? m("span", { class: "spinner" })
            : m.trust(icon("file", { size: 18, strokeWidth: 1.8 })),
        );
    return m(
      "div",
      {
        key: attachment.localId,
        // The status modifier is an interpolated marker; the one status the
        // look distinguishes (a failed upload's red border) rides beside it.
        class:
          `composer-attachment composer-attachment--${attachment.status} ` +
          "inline-flex max-w-[240px] items-center gap-2 rounded-lg border bg-sidebar py-1.5 pr-2 pl-1.5 " +
          (attachment.status === "error" ? "border-danger-border" : ""),
      },
      [
        thumbnail,
        m("span", { class: "composer-attachment-info flex min-w-0 flex-col" }, [
          m(
            "span",
            {
              class: "composer-attachment-name truncate text-(length:--font-size-body) text-primary",
              ...hoverTooltipAttrs(attachment.fileName),
            },
            attachment.fileName,
          ),
          attachment.status === "ready" && attachment.uploaded !== undefined
            ? m(
                "span",
                { class: `${ATTACHMENT_DETAIL_BASE} text-secondary` },
                formatFileSize(attachment.uploaded.size),
              )
            : null,
          attachment.status === "uploading"
            ? m("span", { class: `${ATTACHMENT_DETAIL_BASE} text-secondary` }, "Uploading…")
            : null,
          attachment.status === "error"
            ? m(
                "span",
                { class: `${ATTACHMENT_DETAIL_BASE} composer-attachment-detail--error text-danger` },
                "Upload failed",
              )
            : null,
        ]),
        attachment.status === "uploading"
          ? null
          : m(
              Button,
              {
                variant: "ghost",
                icon: true,
                xs: true,
                extra: "composer-attachment-remove shrink-0",
                "aria-label": "Remove attachment",
                ...hoverTooltipAttrs("Remove attachment"),
                onclick: () => removeComposerAttachment(agentId, attachment.localId),
              },
              m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
            ),
      ],
    );
  }

  return {
    view(vnode) {
      const agentId = vnode.attrs.agentId;

      if (!agentId) {
        return null;
      }

      if (currentAgentId !== agentId) {
        currentAgentId = agentId;
        messageText = localStorage.getItem(messageTextKey(agentId)) ?? "";
        isInterruptInFlight = false;
        // The notices name a command typed for the previous agent, so they must not follow the
        // user to the next one.
        declinedSlashCommand = null;
        interceptedAuthCommand = null;
      }

      // A sibling view (a native tap whose resend failed) merged a returned block into this agent's
      // persisted draft; adopt it into the live composer so it is visible at once, then clear the flag.
      const pendingPrepend = pendingComposerPrepends.get(agentId);
      if (pendingPrepend !== undefined) {
        pendingComposerPrepends.delete(agentId);
        messageText = pendingPrepend;
      }

      async function handleSend(): Promise<void> {
        if (!agentId) {
          return;
        }
        // The composer guard is whatever the agent's harness declared (its
        // `composer_command` popups on the catalog): auth commands open the
        // harness's agent-auth surface, declined commands get the notice, and a
        // harness that declared nothing (pi's declines) sends everything as-is.
        // Only slash-shaped messages consult it, and only those block on the
        // catalog when it has not loaded yet -- otherwise an early /login could
        // slip through the fetch window.
        if (messageText.trim().startsWith("/")) {
          const harness = getAgentById(agentId)?.harness;
          if (getHarnessCatalog(harness) === null) {
            await ensureHarnessCatalogs();
          }
          const match = findComposerPopup(harness, messageText);
          if (match !== null) {
            if (match.popup.action === "open_auth") {
              interceptedAuthCommand = match.command;
            } else {
              declinedSlashCommand = { command: match.command, body: match.popup.notice_body ?? null };
            }
            m.redraw();
            return;
          }
        }
        // Wait for in-flight uploads so a just-dropped file is included rather
        // than dropped from the message.
        await waitForComposerUploads(agentId);

        const attachmentPaths = getReadyAttachmentPaths(agentId);
        const text = messageText;
        if (!text.trim() && attachmentPaths.length === 0) {
          return;
        }

        const finalText = buildMessageWithAttachments(text, attachmentPaths);
        // Snapshot for rollback if the send fails.
        const sentText = text;
        const sentAttachments = getComposerAttachments(agentId);

        messageText = "";
        clearComposerAttachments(agentId);
        localStorage.removeItem(messageTextKey(agentId));

        // Paint an optimistic "Sending…" bubble at the tail immediately -- the ONE
        // optimism the frontend is allowed (contract A2). It is a client-only overlay
        // (see models/OutgoingMessages) whose removal is BACKEND-DRIVEN: it drops only
        // once the real message arrives from the backend (its queued chip or committed
        // transcript turn), real-first, so there is never a gap.
        const outgoingId = addOutgoing(agentId, sentText);
        m.redraw();

        try {
          await sendMessage(agentId, finalText);
          // The send resolved: the message is now real (committed or queued), so its
          // "Sending…" bubble is removed by the arriving transcript turn or queued
          // snapshot (see OutgoingMessages.noteBackendArrivals) -- nothing to do here.
        } catch (err) {
          // The send genuinely failed (the backend confirms delivery before
          // resolving, so a rejection means the message was NOT accepted). Drop the
          // optimistic bubble and handle failure the original way: restore the
          // text/attachments to the composer, then surface a popup.
          const detail = describeRequestError(err);
          console.error(`Failed to send message to agent ${agentId}: ${detail}`);
          dropOutgoing(agentId, outgoingId);
          // Restore the user's text and attachments so the send is not silently
          // lost -- but only if they have not already started a new draft for this
          // agent (the input was cleared at send time, so during the in-flight
          // request the user may have typed or attached something new; blindly
          // restoring would clobber that newer draft).
          const currentDraft =
            currentAgentId === agentId ? messageText : (localStorage.getItem(messageTextKey(agentId)) ?? "");
          const isComposerEmpty = currentDraft.trim().length === 0 && getComposerAttachments(agentId).length === 0;
          if (isComposerEmpty) {
            localStorage.setItem(messageTextKey(agentId), sentText);
            restoreComposerAttachments(agentId, sentAttachments);
            if (currentAgentId === agentId) {
              messageText = sentText;
            }
          }
          m.redraw();
          alert(`Failed to send message: ${detail}`);
        }

        requestAnimationFrame(() => {
          focusMessageTextarea();
        });
      }

      async function handleStopToComposer(): Promise<void> {
        if (!agentId || isInterruptInFlight) {
          return;
        }
        // Hide the stop button until the request settles so the user cannot fire
        // off multiple restarts in quick succession.
        isInterruptInFlight = true;
        // Snapshot the Sending bubbles that exist BEFORE the interrupt round-trip. On
        // success we clear exactly these: every one is now either Delivered (its turn
        // already dropped its bubble via arrival) or Returned into the composer via the
        // block below -- so any that remain are Returned with no arrival to clear them
        // (the ghost). Clearing only the pre-interrupt set leaves a message the user
        // sends DURING the round-trip untouched (it is not in the returned block).
        const preInterruptBubbleIds = getOutgoingMessages(agentId).map((message) => message.id);
        m.redraw();
        try {
          // Interrupt the agent and pull any queued messages back into the composer,
          // unsent, for the user to edit and send. Empty block = nothing was queued
          // (a clean no-op).
          const { block } = await drainToComposer(agentId);
          // Every not-Delivered message is now back in the composer (or was Delivered and
          // dropped its own bubble); clear the pre-interrupt Sending bubbles so none ghost.
          clearOutgoing(agentId, preInterruptBubbleIds);
          if (block) {
            // Merge instead of drop: prepend the handed-back block above any existing draft
            // (block, blank line, draft) rather than dropping it when the composer is
            // non-empty. Under pi's native retract the messages survive nowhere else, so
            // dropping them here would lose them outright.
            const draft = messageText;
            const merged = draft.trim().length === 0 ? block : `${block}\n\n${draft}`;
            messageText = merged;
            localStorage.setItem(messageTextKey(agentId), merged);
          }
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to interrupt agent ${agentId}: ${detail}`);
          // Surface the failure: they deliberately clicked Stop, and on failure
          // the agent is still running. Matches the alert-based feedback
          // convention for user-initiated mutations (see executeDestroy).
          alert(`Failed to interrupt agent: ${detail}`);
        } finally {
          isInterruptInFlight = false;
          m.redraw();
        }
      }

      function handleKeydown(event: KeyboardEvent): void {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          handleSend();
        }
      }

      function handlePaste(event: ClipboardEvent): void {
        if (!agentId) {
          return;
        }
        const files = imageFilesFromClipboard(event.clipboardData);
        if (files.length > 0) {
          event.preventDefault();
          uploadFilesToComposer(agentId, files);
        }
      }

      function openFilePicker(): void {
        fileInputElement?.click();
      }

      function dismissAuthCommandNotice(): void {
        interceptedAuthCommand = null;
        messageText = "";
        if (agentId) {
          localStorage.removeItem(messageTextKey(agentId));
        }
        m.redraw();
      }

      function dismissDeclinedCommandNotice(): void {
        declinedSlashCommand = null;
        m.redraw();
      }

      function renderDeclinedCommandNotice(declined: { command: string; body: string | null }): m.Vnode {
        return m(
          "div.modal-overlay",
          {
            oncreate() {
              document.addEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            ...backdropDismissAttrs(dismissDeclinedCommandNotice),
          },
          m("div.modal-card", [
            m(
              "div.modal-header",
              m("h3", { class: MODAL_TITLE_CLASS }, `${declined.command} can't be sent from chat`),
            ),
            m(
              "p",
              { class: MODAL_MESSAGE_CLASS },
              declined.body ?? "You can still send it from the agent's terminal.",
            ),
            m("div.modal-actions", [
              m(
                Button,
                {
                  // Focus it so Enter and Space dismiss too, and so the notice is reachable
                  // without a mouse.
                  oncreate: (buttonVnode) => (buttonVnode.dom as HTMLButtonElement).focus(),
                  onclick: () => dismissDeclinedCommandNotice(),
                },
                "OK",
              ),
            ]),
          ]),
        );
      }

      function renderAuthCommandNotice(command: string): m.Vnode {
        const title = command === "/logout" ? "Sign-out is managed here" : "Sign-in is managed here";
        const explanation =
          `Sending ${command} to the agent would run its own auth flow inside the agent's terminal, ` +
          "outside this workspace's managed sign-in. Use the agent auth screen instead.";
        return m(
          "div.modal-overlay",
          {
            ...backdropDismissAttrs(dismissAuthCommandNotice),
          },
          m("div.modal-card", [
            m("div.modal-header", m("h3", { class: MODAL_TITLE_CLASS }, title)),
            m("p", { class: MODAL_MESSAGE_CLASS }, explanation),
            m("div.modal-actions", [
              m(Button, { onclick: () => dismissAuthCommandNotice() }, "Cancel"),
              m(
                Button,
                {
                  variant: "primary",
                  onclick: () => {
                    dismissAuthCommandNotice();
                    if (agentId) {
                      openAgentAuth(agentId);
                    }
                  },
                },
                "Open agent auth",
              ),
            ]),
          ]),
        );
      }

      const attachments = getComposerAttachments(agentId);
      const hasMessageText = messageText.trim().length > 0;
      const canSend = hasMessageText || hasReadyAttachments(agentId);

      // The stop button is only meaningful while the agent has an interruptible
      // turn in progress -- the same condition that drives the activity indicator
      // above the input, read straight off the backend-derived activity state.
      const isAgentWorking = isWorkingActivityState(getAgentById(agentId)?.activity_state ?? null);
      const isStopButtonVisible = isAgentWorking && !isInterruptInFlight;

      return m(
        "div",
        { class: "message-input mx-auto w-full max-w-[calc(var(--width-message-column)+2*var(--radius-xl))]" },
        [
          interceptedAuthCommand !== null ? renderAuthCommandNotice(interceptedAuthCommand) : null,
          declinedSlashCommand !== null ? renderDeclinedCommandNotice(declinedSlashCommand) : null,
          m("input", {
            type: "file",
            multiple: true,
            class: "message-input-file-input hidden",
            oncreate: (inputVnode: m.VnodeDOM) => {
              fileInputElement = inputVnode.dom as HTMLInputElement;
            },
            onremove: () => {
              fileInputElement = null;
            },
            onchange: (event: Event) => {
              const input = event.target as HTMLInputElement;
              uploadFilesToComposer(agentId, input.files);
              input.value = "";
            },
          }),
          m("div", { class: INPUT_BOX_CLASS }, [
            attachments.length > 0
              ? m(
                  "div",
                  { class: "message-input-attachments flex flex-wrap gap-2 pt-3 pr-3 pl-4" },
                  attachments.map((attachment) => renderComposerAttachment(agentId, attachment)),
                )
              : null,
            m("div", { class: "message-input-row flex flex-row items-center" }, [
              m("textarea", {
                class:
                  "message-input-textbox flex-1 resize-none border-none bg-transparent pt-3.5 pr-2 pb-3.5 pl-5 " +
                  "font-sans text-(length:--font-size-body) leading-normal text-primary focus:outline-none " +
                  "placeholder:text-faint",
                placeholder: isAgentWorking ? "Type to queue more messages..." : "Type a message...",
                rows: 1,
                value: messageText,
                oncreate: (textareaVnode: m.VnodeDOM) => {
                  messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                  autoResizeTextarea(messageTextareaElement);
                  focusMessageTextarea();
                },
                onupdate: (textareaVnode: m.VnodeDOM) => {
                  messageTextareaElement = textareaVnode.dom as HTMLTextAreaElement;
                  autoResizeTextarea(messageTextareaElement);
                },
                onremove: () => {
                  messageTextareaElement = null;
                },
                oninput: (event: Event) => {
                  const textarea = event.target as HTMLTextAreaElement;
                  messageText = textarea.value;
                  localStorage.setItem(messageTextKey(agentId), messageText);
                  autoResizeTextarea(textarea);
                },
                onkeydown: handleKeydown,
                onpaste: handlePaste,
              }),
              m("div", { class: "message-input-toolbar flex shrink-0 items-center gap-2 pr-3" }, [
                m(
                  Button,
                  {
                    variant: "ghost",
                    icon: true,
                    round: true,
                    extra: "message-input-attach-button shrink-0",
                    ...hoverTooltipAttrs("Attach files"),
                    "aria-label": "Attach files",
                    onclick: openFilePicker,
                  },
                  m.trust(icon("attach", { size: 18 })),
                ),
                isStopButtonVisible
                  ? m(
                      Button,
                      {
                        variant: "stop",
                        icon: true,
                        round: true,
                        sm: true,
                        extra: "message-input-stop-button shrink-0",
                        ...hoverTooltipAttrs("Interrupt and bring queued messages to the composer"),
                        "aria-label": "Interrupt and bring queued messages to the composer",
                        onclick: handleStopToComposer,
                      },
                      m.trust(stopIcon(14)),
                    )
                  : null,
                canSend
                  ? m(
                      Button,
                      {
                        variant: "primary",
                        icon: true,
                        round: true,
                        extra: "message-input-send-button shrink-0",
                        ...hoverTooltipAttrs("Send message"),
                        "aria-label": "Send message",
                        onclick: handleSend,
                      },
                      m.trust(icon("send", { size: 16, strokeWidth: 2.5 })),
                    )
                  : null,
              ]),
            ]),
          ]),
        ],
      );
    },
  };
}
