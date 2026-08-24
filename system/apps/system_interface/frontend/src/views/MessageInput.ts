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
import { drainToComposer, interruptAgent, sendMessage } from "../models/Response";
import { addOutgoing, clearOutgoing, dropOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { describeRequestError } from "../models/request-error";
import { openAgentAuth } from "../models/AgentAuth";
import { ensureHarnessCatalogs, findComposerPopup, getHarnessCatalog } from "../models/HarnessCatalog";
import { getAgentById } from "../models/AgentManager";
import { isWorkingActivityState } from "./ActivityIndicator";
import { hoverTooltipAttrs } from "./hoverTooltip";
import { icon, stopIcon } from "./icons";

const MAX_TEXTAREA_HEIGHT_PX = 200;

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
  // Why the last send or interrupt failed, in the harness's own words, shown as a notice.
  // Component state like the notices above, NOT module state: every open chat panel mounts its
  // own MessageInput, and a module-level value would raise the notice in all of them at once.
  // `recovery` is what makes the notice actionable, and it is carried by the OPERATION rather
  // than the failure: a send can be repeated, so it offers Cancel / Retry / Force, while a
  // failed interrupt has nothing to repeat and gets a plain OK. Any send failure qualifies --
  // a dialog holding the input, a readiness timeout, a transport error -- because the ways out
  // are the same whatever the reason; only the text differs.
  type SendRecovery = { agentId: string; text: string; attachments: readonly ComposerAttachment[] };
  let actionFailureDetail: string | null = null;
  let actionFailureRecovery: SendRecovery | null = null;
  // Which recovery action is mid-flight, so the buttons disable rather than fire twice.
  let actionFailureInFlight: "retry" | "force" | null = null;
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

  // Its own handler rather than the one above: each notice clears only its own state, and
  // registering one shared function reference from two overlays would be de-duplicated by
  // addEventListener and then torn down by whichever overlay closed first.
  function handleActionFailureNoticeKeydown(event: KeyboardEvent): void {
    if (event.key === "Escape") {
      actionFailureDetail = null;
      m.redraw();
    }
  }

  function renderComposerAttachment(agentId: string, attachment: ComposerAttachment): m.Vnode {
    const isReadyImage = attachment.status === "ready" && attachment.isImage && attachment.uploaded !== undefined;
    const thumbnail = isReadyImage
      ? m("img", {
          class: "composer-attachment-thumb",
          src: attachment.uploaded?.url,
          alt: attachment.fileName,
        })
      : m(
          "span",
          { class: "composer-attachment-icon" },
          attachment.status === "uploading"
            ? m("span", { class: "composer-attachment-spinner" })
            : m.trust(icon("file", { size: 18, strokeWidth: 1.8 })),
        );
    return m(
      "div",
      { key: attachment.localId, class: `composer-attachment composer-attachment--${attachment.status}` },
      [
        thumbnail,
        m("span", { class: "composer-attachment-info" }, [
          m(
            "span",
            { class: "composer-attachment-name", ...hoverTooltipAttrs(attachment.fileName) },
            attachment.fileName,
          ),
          attachment.status === "ready" && attachment.uploaded !== undefined
            ? m("span", { class: "composer-attachment-detail" }, formatFileSize(attachment.uploaded.size))
            : null,
          attachment.status === "uploading" ? m("span", { class: "composer-attachment-detail" }, "Uploading…") : null,
          attachment.status === "error"
            ? m("span", { class: "composer-attachment-detail composer-attachment-detail--error" }, "Upload failed")
            : null,
        ]),
        attachment.status === "uploading"
          ? null
          : m(
              "button",
              {
                type: "button",
                class: "composer-attachment-remove",
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
        actionFailureDetail = null;
        actionFailureRecovery = null;
        actionFailureInFlight = null;
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
          // The message is NOT put back in the composer here -- that is Cancel's job now.
          // Restoring it at this point would leave a copy in the box while the notice offers to
          // resend the same text, so taking Retry would send it and strand a duplicate. Until
          // the user chooses, the only place the message lives is this recovery record.
          //
          // Only if they are still looking at the agent that failed: the catch runs after an
          // await, so they may have switched, and the switch-clear above has already gone by.
          if (currentAgentId === agentId) {
            actionFailureDetail = detail;
            actionFailureRecovery = { agentId, text: sentText, attachments: sentAttachments };
          } else {
            // They moved on, so there is nobody to ask. Put it back where they left it rather
            // than dropping it.
            restoreFailedMessageToComposer(agentId, sentText, sentAttachments);
          }
          m.redraw();
        }

        requestAnimationFrame(() => {
          // Not while a notice is open: this rAF lands after mithril has mounted the notice and
          // focused its OK button, so refocusing the composer would steal it -- leaving a modal
          // the keyboard cannot dismiss, and an Enter that re-sends the just-restored text.
          if (actionFailureDetail === null) {
            focusMessageTextarea();
          }
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
          // the agent is still running. Same notice as a failed send -- leaving this one as a
          // system alert while its neighbour is a styled notice is worse than either.
          actionFailureDetail = detail;
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

      /** Put a failed message back in the composer, in FRONT of whatever is already there. */
      function restoreFailedMessageToComposer(
        forAgentId: string,
        text: string,
        attachments: readonly ComposerAttachment[],
      ): void {
        // Prepending is what lets this run unconditionally. The previous code restored only into
        // an empty composer, to avoid clobbering a draft typed while the send was in flight; put
        // the failed message first and that draft after it and neither is lost.
        const existingDraft =
          currentAgentId === forAgentId ? messageText : (localStorage.getItem(messageTextKey(forAgentId)) ?? "");
        const restored = existingDraft.trim().length === 0 ? text : `${text}\n${existingDraft}`;
        localStorage.setItem(messageTextKey(forAgentId), restored);
        restoreComposerAttachments(forAgentId, attachments);
        if (currentAgentId === forAgentId) {
          messageText = restored;
        }
      }

      function clearActionFailureNotice(): void {
        actionFailureDetail = null;
        actionFailureRecovery = null;
        actionFailureInFlight = null;
      }

      /** Cancel: give the message back and close. Also what Escape and a backdrop press do. */
      function dismissActionFailureNotice(): void {
        // Never dismiss out from under a running action: the send it started is still in flight
        // and will report its own outcome.
        if (actionFailureInFlight !== null) {
          return;
        }
        const recovery = actionFailureRecovery;
        clearActionFailureNotice();
        if (recovery !== null) {
          restoreFailedMessageToComposer(recovery.agentId, recovery.text, recovery.attachments);
        }
        m.redraw();
        // Hand focus back to where the user was typing, which the send path skipped while the
        // notice was up.
        focusMessageTextarea();
      }

      /** Retry: the ordinary send again, so it re-runs preflight and can fail again. */
      async function retryFailedSend(): Promise<void> {
        const recovery = actionFailureRecovery;
        if (recovery === null || actionFailureInFlight !== null) {
          return;
        }
        actionFailureInFlight = "retry";
        m.redraw();
        try {
          await sendMessage(recovery.agentId, recovery.text);
          clearActionFailureNotice();
          focusMessageTextarea();
        } catch (err) {
          // Failed again: show what it says NOW, since the reason may have changed, and keep
          // the message so Cancel and Force are still available.
          actionFailureDetail = describeRequestError(err);
          actionFailureInFlight = null;
        }
        m.redraw();
      }

      /** Force: restart the agent, then send. Destructive -- it ends any in-progress turn. */
      async function forceFailedSend(): Promise<void> {
        const recovery = actionFailureRecovery;
        if (recovery === null || actionFailureInFlight !== null) {
          return;
        }
        actionFailureInFlight = "force";
        m.redraw();
        try {
          // Stop-and-start, through the endpoint that already does exactly that
          // (`mngr start --restart --no-resume`). If it refuses -- the services agent carries
          // is_primary=true -- that refusal becomes the notice's text and nothing is sent.
          await interruptAgent(recovery.agentId);
        } catch (err) {
          actionFailureDetail = describeRequestError(err);
          actionFailureInFlight = null;
          m.redraw();
          return;
        }
        try {
          await sendMessage(recovery.agentId, recovery.text);
          clearActionFailureNotice();
          focusMessageTextarea();
        } catch (err) {
          actionFailureDetail = describeRequestError(err);
          actionFailureInFlight = null;
        }
        m.redraw();
      }

      function renderActionFailureNotice(detail: string): m.Vnode {
        return m(
          "div.custom-url-dialog-overlay",
          {
            oncreate() {
              document.addEventListener("keydown", handleActionFailureNoticeKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleActionFailureNoticeKeydown);
            },
            ...backdropDismissAttrs(dismissActionFailureNotice),
          },
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              // The body already names the agent when the failure is agent-specific, so the
              // title stays generic rather than repeating it.
              m("h3.custom-url-dialog-title", "Couldn't send your message"),
              m("p.logout-notice-body", detail),
              // Many of these are resolved in seconds by someone looking at the pane -- an
              // unsubmitted shell command, a dialog wanting a real answer -- so say so.
              actionFailureRecovery !== null
                ? m("p.logout-notice-body", "You can open the agent's terminal, fix it there, then Retry.")
                : null,
              m("div.custom-url-dialog-actions", [
                m(
                  "button.custom-url-dialog-cancel",
                  {
                    // Focused by default: the only choice that does not act.
                    oncreate: (buttonVnode: m.VnodeDOM) => (buttonVnode.dom as HTMLButtonElement).focus(),
                    disabled: actionFailureInFlight !== null,
                    onclick: () => dismissActionFailureNotice(),
                  },
                  actionFailureRecovery === null ? "OK" : "Cancel",
                ),
                actionFailureRecovery !== null
                  ? m(
                      "button.custom-url-dialog-cancel",
                      {
                        "data-tooltip": "Sends the message again",
                        disabled: actionFailureInFlight !== null,
                        onclick: () => void retryFailedSend(),
                      },
                      actionFailureInFlight === "retry" ? "Retrying…" : "Retry",
                    )
                  : null,
                actionFailureRecovery !== null
                  ? m(
                      "button.custom-url-dialog-danger",
                      {
                        "data-tooltip": "Restarts the agent and sends the message",
                        disabled: actionFailureInFlight !== null,
                        onclick: () => void forceFailedSend(),
                      },
                      actionFailureInFlight === "force" ? "Forcing…" : "Force",
                    )
                  : null,
              ]),
            ],
          ),
        );
      }

      function renderDeclinedCommandNotice(declined: { command: string; body: string | null }): m.Vnode {
        return m(
          "div.custom-url-dialog-overlay",
          {
            oncreate() {
              document.addEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            ...backdropDismissAttrs(dismissDeclinedCommandNotice),
          },
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", `${declined.command} can't be sent from chat`),
              m("p.logout-notice-body", declined.body ?? "You can still send it from the agent's terminal."),
              m("div.custom-url-dialog-actions", [
                m(
                  "button.custom-url-dialog-cancel",
                  {
                    // Focus it so Enter and Space dismiss too, and so the notice is reachable
                    // without a mouse.
                    oncreate: (buttonVnode: m.VnodeDOM) => (buttonVnode.dom as HTMLButtonElement).focus(),
                    onclick: () => dismissDeclinedCommandNotice(),
                  },
                  "OK",
                ),
              ]),
            ],
          ),
        );
      }

      function renderAuthCommandNotice(command: string): m.Vnode {
        const title = command === "/logout" ? "Sign-out is managed here" : "Sign-in is managed here";
        const explanation =
          `Sending ${command} to the agent would run its own auth flow inside the agent's terminal, ` +
          "outside this workspace's managed sign-in. Use the agent auth screen instead.";
        return m(
          "div.custom-url-dialog-overlay",
          {
            ...backdropDismissAttrs(dismissAuthCommandNotice),
          },
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", title),
              m("p.logout-notice-body", explanation),
              m("div.custom-url-dialog-actions", [
                m("button.custom-url-dialog-cancel", { onclick: () => dismissAuthCommandNotice() }, "Cancel"),
                m(
                  "button.custom-url-dialog-open",
                  {
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
            ],
          ),
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

      return m("div", { class: "message-input mx-auto w-full" }, [
        interceptedAuthCommand !== null ? renderAuthCommandNotice(interceptedAuthCommand) : null,
        declinedSlashCommand !== null ? renderDeclinedCommandNotice(declinedSlashCommand) : null,
        actionFailureDetail !== null ? renderActionFailureNotice(actionFailureDetail) : null,
        m("input", {
          type: "file",
          multiple: true,
          class: "message-input-file-input",
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
        m("div", { class: "message-input-box flex flex-col" }, [
          attachments.length > 0
            ? m(
                "div",
                { class: "message-input-attachments" },
                attachments.map((attachment) => renderComposerAttachment(agentId, attachment)),
              )
            : null,
          m("div", { class: "message-input-row flex flex-row items-center" }, [
            m("textarea", {
              class: "message-input-textbox flex-1 resize-none focus:outline-none",
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
            m("div", { class: "message-input-toolbar" }, [
              m(
                "button",
                {
                  type: "button",
                  class: "message-input-attach-button",
                  "data-tooltip": "Attach files",
                  "aria-label": "Attach files",
                  onclick: openFilePicker,
                },
                m.trust(icon("attach", { size: 18 })),
              ),
              isStopButtonVisible
                ? m(
                    "button",
                    {
                      class: "message-input-stop-button",
                      "data-tooltip": "Interrupt and bring queued messages to the composer",
                      "aria-label": "Interrupt and bring queued messages to the composer",
                      onclick: handleStopToComposer,
                    },
                    m.trust(stopIcon(14)),
                  )
                : null,
              canSend
                ? m(
                    "button",
                    {
                      class: "message-input-send-button",
                      "data-tooltip": "Send message",
                      "aria-label": "Send message",
                      onclick: handleSend,
                    },
                    m.trust(icon("send", { size: 16, strokeWidth: 2.5 })),
                  )
                : null,
            ]),
          ]),
        ]),
      ]);
    },
  };
}
