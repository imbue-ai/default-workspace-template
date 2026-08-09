import m from "mithril";
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
import { addOutgoing, dropOutgoing, registerPendingSend, resolveOutgoing } from "../models/OutgoingMessages";
import { describeRequestError } from "../models/request-error";
import { openLoginModal } from "../models/ClaudeAuth";
import { findDeclinedSlashCommand } from "../models/claudeSlashCommands";
import { getAgentById } from "../models/AgentManager";
import { isWorkingActivityState } from "./ActivityIndicator";
import { icon, stopIcon } from "./icons";

const MAX_TEXTAREA_HEIGHT_PX = 200;

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

function messageTextKey(agentId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${agentId}`;
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

// Compatibility export
export function setSelectedModelId(_modelId: string): void {}

export function MessageInput(): m.Component<{ agentId: string | null }> {
  let messageText = "";
  let currentAgentId: string | null = null;
  let messageTextareaElement: HTMLTextAreaElement | null = null;
  // Set instead of sending when the user types one of claude's own auth
  // commands. Delivered to the TUI, /logout would exit the agent's process
  // and wipe shared onboarding state without actually signing the workspace
  // out, and /login would start claude's interactive sign-in inside the
  // agent's terminal -- both bypassing the managed agent-auth screen (auth
  // lives in settings.json / claude's credential store, managed there).
  let interceptedAuthCommand: "/login" | "/logout" | null = null;
  // A slash command the chat declines to deliver, because it would change the agent's terminal
  // rather than start a turn. It still works from that terminal, which the notice says.
  let declinedSlashCommand: string | null = null;
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
          m("span", { class: "composer-attachment-name", title: attachment.fileName }, attachment.fileName),
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
                title: "Remove attachment",
                "aria-label": "Remove attachment",
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
        // The notice names a command typed for the previous agent, so it must not follow the user
        // to the next one.
        declinedSlashCommand = null;
      }

      async function handleSend(): Promise<void> {
        if (!agentId) {
          return;
        }
        // The auth intercepts (/login, /logout) and the declined-command notice are
        // facts about Claude Code's terminal, not about the chat, so they only apply
        // when a Claude agent is on the other end. For any other harness these are
        // just ordinary text -- its own slash commands are its own -- so we let them
        // send. (When a harness needs its own composer guard, this becomes a
        // per-harness lookup; see claudeSlashCommands.ts.)
        const isClaude = getAgentById(agentId)?.harness === "claude";
        const trimmedCommand = messageText.trim().toLowerCase();
        if (isClaude && (trimmedCommand === "/login" || trimmedCommand === "/logout")) {
          interceptedAuthCommand = trimmedCommand;
          m.redraw();
          return;
        }
        const declined = isClaude ? findDeclinedSlashCommand(messageText) : null;
        if (declined !== null) {
          declinedSlashCommand = declined;
          m.redraw();
          return;
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

        // Paint an optimistic "Sending…" bubble at the tail immediately. This is a
        // client-only, self-terminating overlay (see models/OutgoingMessages): it
        // drops the instant the real message arrives from the backend (a queued or
        // committed bubble). The backend stays fully decoupled; it is never told
        // about this state.
        const outgoingId = addOutgoing(agentId, sentText);
        // Track the in-flight send so Stop can wait for it to park (and so the shoulder
        // tap greys out and folds it in) rather than racing it -- see OutgoingMessages.
        const sendPromise = sendMessage(agentId, finalText);
        registerPendingSend(agentId, sendPromise);
        m.redraw();

        try {
          await sendPromise;
          // Delivered (the backend confirms before resolving). Removal is
          // arrival-driven; this only arms an anti-strand fallback.
          resolveOutgoing(agentId, outgoingId);
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
        m.redraw();
        try {
          // Interrupt the agent and pull any queued messages back into the composer,
          // unsent, for the user to edit and send. Empty block = nothing was queued
          // (a clean no-op).
          const { block } = await drainToComposer(agentId);
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

      function renderDeclinedCommandNotice(command: string): m.Vnode {
        return m(
          "div.custom-url-dialog-overlay",
          {
            oncreate() {
              document.addEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            onremove() {
              document.removeEventListener("keydown", handleDeclinedNoticeKeydown);
            },
            onclick(e: MouseEvent) {
              if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
                dismissDeclinedCommandNotice();
              }
            },
          },
          m(
            "div.custom-url-dialog",
            {
              onclick(e: MouseEvent) {
                e.stopPropagation();
              },
            },
            [
              m("h3.custom-url-dialog-title", `${command} can't be sent from chat`),
              m("p.logout-notice-body", "You can still send it from the agent's terminal."),
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

      function renderAuthCommandNotice(command: "/login" | "/logout"): m.Vnode {
        const title = command === "/login" ? "Sign-in is managed here" : "Sign-out is managed here";
        const explanation =
          command === "/login"
            ? "Sending /login to the agent would start Claude's own sign-in inside the agent's terminal, " +
              "which would not sign the rest of the workspace in. Use the agent auth screen instead."
            : "Sending /logout to the agent would shut it down without signing the workspace out. " +
              "Use the agent auth screen to switch or remove credentials.";
        return m(
          "div.custom-url-dialog-overlay",
          {
            onclick(e: MouseEvent) {
              if ((e.target as HTMLElement).classList.contains("custom-url-dialog-overlay")) {
                dismissAuthCommandNotice();
              }
            },
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
              m(
                "p.logout-notice-body",
                `This workspace's Claude sign-in is managed by this interface. ${explanation}`,
              ),
              m("div.custom-url-dialog-actions", [
                m("button.custom-url-dialog-cancel", { onclick: () => dismissAuthCommandNotice() }, "Cancel"),
                m(
                  "button.custom-url-dialog-open",
                  {
                    onclick: () => {
                      dismissAuthCommandNotice();
                      openLoginModal();
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
