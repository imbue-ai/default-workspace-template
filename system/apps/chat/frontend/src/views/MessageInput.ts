import m from "mithril";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import type { SendFailureKind } from "@imbue/workspace-ui/src/models/request-error";
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
import { drainToComposer, getEventsForChat, interruptAgent, mintMessageId, sendMessage } from "../models/Response";
import { cancelHandoff, switchChat } from "../models/Handoffs";
import { getPendingPick, pendingSwitchTarget, setPendingAccount, switchKind } from "../models/PendingLane";
import type { ProviderAccount } from "../models/Providers";
import { openSwitchDialog } from "./SwitchDialog";
import { addOutgoing, clearOutgoing, dropOutgoing, getOutgoingMessages } from "../models/OutgoingMessages";
import { describeRequestError, describeRequestErrorKind } from "@imbue/workspace-ui/src/models/request-error";
import { getSelectedAccount, openProviderChooser } from "../models/Providers";
import {
  ensureHarnessCatalogs,
  findComposerPopup,
  getHarnessCatalog,
  hasFastModeLimit,
} from "../models/HarnessCatalog";
import { chooseFastMode, parseFastModeCommand } from "./fast-mode-limit";
import {
  getChatById,
  getProvisionalChat,
  isHandoffCancellable,
  launchChat,
  whenChatRegistered,
} from "../models/Chats";
import { isWorkingActivityState } from "./ActivityIndicator";
import { harnessLabel } from "./harness-labels";
import { handoffComposerPlaceholder } from "./handoff-phase";
import { hoverTooltipAttrs } from "@imbue/workspace-ui/src/components/hoverTooltip";
import { icon, stopIcon } from "@imbue/workspace-ui/src/components/icons";
import { Button } from "@imbue/workspace-ui/src/components/Button";

const MAX_TEXTAREA_HEIGHT_PX = 200;

/* Styling.
 * Utilities in the markup; the message-input and composer-attachment class
 * names stay as bare markers (the e2e tests drive the textbox and send button
 * by them). Attachment status looks are resolved in code, one utility per
 * property. */

/** The composer card. No shadow: it sits at the foot of its own pane
 *  rather than floating over the transcript. */
const INPUT_BOX_CLASS =
  "message-input-box flex flex-col rounded-xl border bg-composer " +
  "[transition:border-color_150ms] focus-within:border-accent";

const ATTACHMENT_DETAIL_BASE = "composer-attachment-detail text-(length:--font-size-helper)";

const MESSAGE_TEXT_KEY_PREFIX = "message-text:";

function messageTextKey(chatId: string): string {
  return `${MESSAGE_TEXT_KEY_PREFIX}${chatId}`;
}

// Blocks handed back to the composer from OUTSIDE this component (a native shoulder tap whose combined
// resend failed -- see QueuedMessageView), keyed by agent. The composer's own ``messageText`` is a
// per-instance closure, so a sibling view cannot merge into it directly; it drops the block here and
// redraws, and the composer applies it on its next view pass (prepend, then persist), the same
// merge-not-drop rule Stop's drain-to-composer uses. Persisted to localStorage regardless, so the text
// survives even if the composer is not currently mounted -- never swallowed (contract A1a).
const pendingComposerPrepends = new Map<string, string>();

/** A failure raised from OUTSIDE this component, waiting for the composer to show it. */
interface PendingFailureNotice {
  title: string;
  detail: string;
  /** mngr's classification, so a sibling's failure earns the same buttons the composer's would. */
  kind?: SendFailureKind;
  /** Repeated by Retry. Omitted when the operation cannot be repeated. */
  retry?: () => Promise<void>;
}
const pendingFailureNotices = new Map<string, PendingFailureNotice>();

/**
 * Raise the chat's failure notice for ``chatId`` from a sibling view.
 *
 * The composer owns the notice because it owns the composer -- Cancel means "the message is back
 * in the box, go look at it". A sibling that fails a send hands the failure here rather than
 * putting up its own dialog, so one shape of failure gets one shape of answer no matter which
 * button started it.
 */
export function raiseFailureNotice(chatId: string, notice: PendingFailureNotice): void {
  // Only ever one pending per agent, and only for the agent it concerns: a composer that is not
  // mounted cannot show this, and a stale entry would otherwise surface as a modal about
  // something that failed long ago the next time the user opened that chat.
  pendingFailureNotices.clear();
  pendingFailureNotices.set(chatId, notice);
  m.redraw();
}

// Chats whose draft was taken out of the composer by a sibling view (the switch dialog's "Start a
// new chat" moves it to the new chat); the mounted composer clears its own text on its next pass.
const pendingComposerClears = new Set<string>();

/** What a sibling view took out of ``chatId``'s composer to send somewhere else. */
export interface TakenComposerDraft {
  /** What the user typed, for putting back if it could not be sent. */
  text: string;
  /** What goes on the wire: the text with the attachment references appended. */
  finalText: string;
  attachments: readonly ComposerAttachment[];
}

/**
 * Take ``chatId``'s draft out of the composer, leaving it empty: what a new chat started from the
 * switch dialog begins with. The attachments travel with the text, so a file dropped in this
 * composer is not left behind by the move, and in-flight uploads are awaited first so a file
 * dropped a moment ago still makes it. The persisted copy goes too, so a reload does not bring it
 * back. Null when an upload failed: the file would leave the message silently and its chip would be
 * cleared along with the rest, so this refuses and names it instead, as an ordinary send does.
 */
export async function takeComposerDraft(chatId: string): Promise<TakenComposerDraft | null> {
  await waitForComposerUploads(chatId);
  const failed = getComposerAttachments(chatId).filter((attachment) => attachment.status === "error");
  if (failed.length > 0) {
    const names = failed.map((attachment) => attachment.fileName).join(", ");
    raiseFailureNotice(chatId, {
      title: failed.length === 1 ? "An attachment didn't upload" : "Some attachments didn't upload",
      detail: `${names} could not be uploaded, so the new chat was not started. Remove the attachment, or try again.`,
    });
    return null;
  }
  const text = localStorage.getItem(messageTextKey(chatId)) ?? "";
  const attachments = getComposerAttachments(chatId);
  const finalText = buildMessageWithAttachments(text, getReadyAttachmentPaths(chatId));
  localStorage.removeItem(messageTextKey(chatId));
  clearComposerAttachments(chatId);
  pendingComposerClears.add(chatId);
  m.redraw();
  return { text, finalText, attachments };
}

/** Hand a taken draft back to ``chatId``'s composer when what it was taken for did not happen. */
export function restoreComposerDraft(chatId: string, taken: TakenComposerDraft): void {
  restoreToComposer(chatId, taken.text, taken.attachments);
}

/**
 * Put ``text`` and ``attachments`` back in ``chatId``'s composer, above whatever is in it now.
 *
 * Prepending is what lets it run unconditionally: the returned message first and any draft typed
 * since after it, and neither is lost. The attachments merge rather than overwrite, since anything
 * attached in the meantime would otherwise go with them.
 */
function restoreToComposer(chatId: string, text: string, attachments: readonly ComposerAttachment[]): void {
  prependToComposer(chatId, text);
  const existingAttachments = getComposerAttachments(chatId);
  const existingIds = new Set(existingAttachments.map((attachment) => attachment.localId));
  restoreComposerAttachments(chatId, [
    ...attachments.filter((attachment) => !existingIds.has(attachment.localId)),
    ...existingAttachments,
  ]);
}

/** Hand ``block`` back to ``chatId``'s composer (prepended above any draft), from a sibling view. */
export function prependToComposer(chatId: string, block: string): void {
  if (!block) {
    return;
  }
  const existingDraft = localStorage.getItem(messageTextKey(chatId)) ?? "";
  const merged = existingDraft.trim().length === 0 ? block : `${block}\n\n${existingDraft}`;
  localStorage.setItem(messageTextKey(chatId), merged);
  pendingComposerPrepends.set(chatId, merged);
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

export function MessageInput(): m.Component<{ chatId: string | null }> {
  let messageText = "";
  let currentChatId: string | null = null;
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
  // `text` is what the user typed, for putting back in the composer; `sentText` is what was
  // actually sent (attachment references appended), for repeating the send. They differ whenever
  // the message carried attachments, and sending the typed text alone would silently drop them.
  type SendRecovery = {
    chatId: string;
    text: string;
    sentText: string;
    attachments: readonly ComposerAttachment[];
  };
  let actionFailureDetail: string | null = null;
  // A failed Stop shares this notice, and telling the user their message could not be sent when
  // they clicked Stop is simply wrong copy.
  let actionFailureTitle = "Couldn't send your message";
  let actionFailureRecovery: SendRecovery | null = null;
  // Which recovery action is mid-flight, so the buttons disable rather than fire twice.
  let actionFailureInFlight: "retry" | "force" | null = null;
  // One notice instance each: the component closes over its own dismiss handler, so sharing one
  // between notices would let whichever rendered last answer the others' Escape.
  const actionFailureNotice = makeNoticeDialog();
  const declinedCommandNotice = makeNoticeDialog();
  const authCommandNotice = makeNoticeDialog();
  // mngr's classification of the failure, which decides which recoveries are worth offering.
  let actionFailureKind: SendFailureKind = "unknown";
  // Retry for a failure raised by a sibling view, which knows how to repeat its own operation.
  let externalRetry: (() => Promise<void>) | null = null;
  let fileInputElement: HTMLInputElement | null = null;
  let isInterruptInFlight = false;
  // The switch to the pending lane (spec 5.1): the handoff request is out, or the cancel of a
  // running switch is out.
  let isSwitchInFlight = false;
  let isCancelSwitchInFlight = false;

  function focusMessageTextarea(): void {
    messageTextareaElement?.focus();
  }

  // Its own handler rather than the one above: each notice clears only its own state, and
  // registering one shared function reference from two overlays would be de-duplicated by
  // addEventListener and then torn down by whichever overlay closed first.
  function renderComposerAttachment(chatId: string, attachment: ComposerAttachment): m.Vnode {
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
              ...hoverTooltipAttrs(attachment.fileName, "above"),
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
                ...hoverTooltipAttrs("Remove attachment", "above"),
                onclick: () => removeComposerAttachment(chatId, attachment.localId),
              },
              m.trust(icon("close", { size: 12, strokeWidth: 2.5 })),
            ),
      ],
    );
  }

  return {
    view(vnode) {
      const chatId = vnode.attrs.chatId;

      if (!chatId) {
        return null;
      }

      if (currentChatId !== chatId) {
        currentChatId = chatId;
        messageText = localStorage.getItem(messageTextKey(chatId)) ?? "";
        isInterruptInFlight = false;
        isSwitchInFlight = false;
        isCancelSwitchInFlight = false;
        // The notices name a command typed for the previous agent, so they must not follow the
        // user to the next one.
        declinedSlashCommand = null;
        interceptedAuthCommand = null;
        // Safe to drop: the failed message was put back in that agent's composer when the send
        // failed, so nothing is lost by closing its notice unanswered.
        actionFailureDetail = null;
        actionFailureRecovery = null;
        actionFailureInFlight = null;
        actionFailureKind = "unknown";
        externalRetry = null;
      }

      // A sibling view (a native tap whose resend failed) merged a returned block into this agent's
      // persisted draft; adopt it into the live composer so it is visible at once, then clear the flag.
      // A sibling view raised a failure for this agent; adopt it into the notice.
      const pendingNotice = pendingFailureNotices.get(chatId);
      if (pendingNotice !== undefined) {
        pendingFailureNotices.delete(chatId);
        clearActionFailureNotice();
        actionFailureTitle = pendingNotice.title;
        actionFailureDetail = pendingNotice.detail;
        actionFailureKind = pendingNotice.kind ?? "unknown";
        externalRetry = pendingNotice.retry ?? null;
      }

      const pendingPrepend = pendingComposerPrepends.get(chatId);
      // The clear before the prepend: a take-then-hand-straight-back (a new chat the shell could
      // not start) leaves both pending on the same pass, and the restored draft must win.
      if (pendingComposerClears.delete(chatId)) {
        messageText = "";
      }
      if (pendingPrepend !== undefined) {
        pendingComposerPrepends.delete(chatId);
        messageText = pendingPrepend;
      }

      /** What a send (or a switch) puts on the wire, with what to put back if it fails. */
      interface PreparedSend {
        /** What the user typed, for the bubble and for restoring the composer. */
        text: string;
        /** What goes to the agent: the text with the attachment references appended. */
        finalText: string;
        attachments: readonly ComposerAttachment[];
      }

      /**
       * The preflight every send shares: the harness's composer guard, the upload wait, the
       * failed-upload refusal, and the empty check; then the composer is cleared and what was
       * in it comes back for the wire. Null when nothing is to be sent (the guard raised a notice,
       * an upload failed, or the box was empty).
       */
      async function prepareSend(): Promise<PreparedSend | null> {
        if (!chatId) {
          return null;
        }
        // The composer guard is whatever the agent's harness declared (its
        // `composer_command` popups on the catalog): auth commands open the
        // harness's agent-auth surface, declined commands get the notice, and a
        // harness that declared nothing (pi's declines) sends everything as-is.
        // Only slash-shaped messages consult it, and only those block on the
        // catalog when it has not loaded yet -- otherwise an early /login could
        // slip through the fetch window.
        if (messageText.trim().startsWith("/")) {
          const harness = getChatById(chatId)?.active_agent.harness;
          if (getHarnessCatalog(harness) === null) {
            await ensureHarnessCatalogs();
          }
          // ``/fast on`` and ``/fast off`` choose the chat's fast mode (on a harness that has
          // one) rather than reaching the harness or its notice: the mode is the chat's setting.
          const fastMode = parseFastModeCommand(messageText);
          if (fastMode !== null && hasFastModeLimit(harness)) {
            chooseFastMode(chatId, fastMode, getEventsForChat(chatId));
            messageText = "";
            localStorage.removeItem(messageTextKey(chatId));
            m.redraw();
            return null;
          }
          const match = findComposerPopup(harness, messageText);
          if (match !== null) {
            if (match.popup.action === "open_auth") {
              interceptedAuthCommand = match.command;
            } else {
              declinedSlashCommand = { command: match.command, body: match.popup.notice_body ?? null };
            }
            m.redraw();
            return null;
          }
        }
        // Wait for in-flight uploads so a just-dropped file is included rather
        // than dropped from the message.
        await waitForComposerUploads(chatId);

        const attachmentPaths = getReadyAttachmentPaths(chatId);
        const text = messageText;

        // An upload that failed is dropped by getReadyAttachmentPaths and its chip is cleared
        // below, so without this the file would leave the message silently and the only clue
        // would be a small label that then disappears. Refuse the send and say which file.
        const failedAttachments = getComposerAttachments(chatId).filter((attachment) => attachment.status === "error");
        if (failedAttachments.length > 0) {
          // Same guard as the send-failure path: the upload wait above is awaited, so the user
          // may have switched agents, and a notice about this agent's files must not land on
          // another agent's chat.
          if (currentChatId !== chatId) {
            return null;
          }
          const names = failedAttachments.map((attachment) => attachment.fileName).join(", ");
          actionFailureTitle =
            failedAttachments.length === 1 ? "An attachment didn't upload" : "Some attachments didn't upload";
          actionFailureDetail = `${names} could not be uploaded, so the message was not sent. Remove the attachment, or try again.`;
          actionFailureRecovery = null;
          externalRetry = null;
          m.redraw();
          return null;
        }

        if (!text.trim() && attachmentPaths.length === 0) {
          // Nothing to send. Reachable by clicking Send with an empty box, which needs no
          // explanation -- the button simply does nothing.
          return null;
        }

        const finalText = buildMessageWithAttachments(text, attachmentPaths);
        // Snapshot for rollback if the send fails.
        const attachments = getComposerAttachments(chatId);

        messageText = "";
        clearComposerAttachments(chatId);
        localStorage.removeItem(messageTextKey(chatId));
        return { text, finalText, attachments };
      }

      async function handleSend(): Promise<void> {
        if (!chatId) {
          return;
        }
        const prepared = await prepareSend();
        if (prepared === null) {
          return;
        }
        const { text: sentText, finalText, attachments: sentAttachments } = prepared;

        // Paint an optimistic "Sending…" bubble at the tail immediately -- the ONE
        // optimism the frontend is allowed (contract A2). It is a client-only overlay
        // (see models/OutgoingMessages) whose removal is BACKEND-DRIVEN: it drops only
        // once the real message arrives from the backend (its queued chip or committed
        // transcript turn), real-first, so there is never a gap. The id minted here rides
        // the send too, so a chat app that holds the message while the chat switches
        // harness can name it back and the bubble stands down for that rendering.
        const messageId = mintMessageId();
        const outgoingId = addOutgoing(chatId, sentText, messageId);
        m.redraw();

        try {
          // A seeded chat awaiting its first send has no agent yet, and this send is what
          // launches one: on the signed-in account, else on the one the chooser produces. The
          // message rides the launch as the agent's first, so nothing is sent after it lands.
          if (getProvisionalChat(chatId)?.phase === "awaiting_first_send" && getChatById(chatId) === undefined) {
            const accountId = await chooseAccountForFirstSend();
            if (accountId === null) {
              // The chooser closed with no sign-in: nothing was launched, and the message is
              // the user's to keep.
              dropOutgoing(chatId, outgoingId);
              restoreFailedMessageToComposer(chatId, sentText, sentAttachments);
              m.redraw();
              return;
            }
            await launchChat(chatId, accountId, finalText);
            try {
              await whenChatRegistered(chatId);
            } catch (err) {
              // The create failed after the launch took the message: the chat keeps it as its
              // first message and the page's "Try again" delivers it, so it does not go back to
              // the composer (a copy there would be sent twice). The page shows the reason.
              console.error(`The seeded chat ${chatId} could not be started: ${describeRequestError(err)}`);
              dropOutgoing(chatId, outgoingId);
              m.redraw();
              return;
            }
            refocusAfterSend();
            return;
          }
          // A chat still being created has no agent to deliver to yet: the bubble stays
          // "Sending…" until the create lands, and the send goes out then. A create that
          // fails rejects here and the message goes back to the composer like any failed send.
          await whenChatRegistered(chatId);
          await sendMessage(chatId, finalText, messageId);
          // The send resolved: the message is now real (committed, queued, or held for the
          // agent the chat is switching to), so its "Sending…" bubble is removed by the arriving
          // transcript turn, queued snapshot, or held-send snapshot (see
          // OutgoingMessages) -- nothing to do here.
        } catch (err) {
          // The send genuinely failed (the backend confirms delivery before
          // resolving, so a rejection means the message was NOT accepted). Drop the
          // optimistic bubble and handle failure the original way: restore the
          // text/attachments to the composer, then surface a popup.
          const detail = describeRequestError(err);
          console.error(`Failed to send message to chat ${chatId}: ${detail}`);
          dropOutgoing(chatId, outgoingId);
          // Back in the composer immediately: the recovery record is closure state, so a reload
          // would take the message with it (contract A1a). A repeat send removes that copy once
          // it has landed.
          restoreFailedMessageToComposer(chatId, sentText, sentAttachments);
          // Actions only if they are still on the agent that failed -- this catch runs after an
          // await, so they may have switched and the switch-clear has already gone by.
          if (currentChatId === chatId) {
            actionFailureTitle = "Couldn't send your message";
            actionFailureDetail = detail;
            actionFailureKind = describeRequestErrorKind(err);
            actionFailureRecovery = { chatId, text: sentText, sentText: finalText, attachments: sentAttachments };
          }
          m.redraw();
        }

        refocusAfterSend();
      }

      /**
       * The account a seeded chat's first send launches it on: the signed-in one when there is
       * one, else whatever the provider chooser produces, or null when it is dismissed instead.
       */
      function chooseAccountForFirstSend(): Promise<string | null> {
        const account = getSelectedAccount();
        if (account !== null) {
          return Promise.resolve(account.id);
        }
        return new Promise((resolve) => {
          openProviderChooser({ onSignedIn: (accountId) => resolve(accountId), onDismissed: () => resolve(null) });
        });
      }

      /**
       * The armed switch, carried out (spec 5.1): the typed message becomes the new agent's first,
       * through the handoff route, with the model picked in the dialog. The choice was made in the
       * dialog, so nothing asks again here. The bubble painted here carries the send-time id, so
       * the held-send rendering the next snapshot brings replaces it exactly; the queued text
       * draining took off the old agent comes back for the composer.
       */
      async function handleSwitchAndSend(target: ProviderAccount): Promise<void> {
        if (!chatId || isSwitchInFlight) {
          return;
        }
        isSwitchInFlight = true;
        m.redraw();
        const prepared = await prepareSend();
        if (prepared === null) {
          isSwitchInFlight = false;
          m.redraw();
          return;
        }
        const { text: sentText, finalText, attachments: sentAttachments } = prepared;
        const messageId = mintMessageId();
        const outgoingId = addOutgoing(chatId, sentText, messageId);
        m.redraw();
        try {
          await whenChatRegistered(chatId);
          const pick = getPendingPick(chatId)?.identity ?? null;
          const { returned_block } = await switchChat(chatId, target.id, finalText, messageId, pick);
          prependToComposer(chatId, returned_block);
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to switch chat ${chatId} to ${target.id}: ${detail}`);
          dropOutgoing(chatId, outgoingId);
          restoreFailedMessageToComposer(chatId, sentText, sentAttachments);
          if (currentChatId === chatId) {
            clearActionFailureNotice();
            actionFailureTitle = `Couldn't switch to ${harnessLabel(target.harness)}`;
            actionFailureDetail = detail;
          }
        } finally {
          isSwitchInFlight = false;
          m.redraw();
        }
        refocusAfterSend();
      }

      /** Call a running switch off (spec 5.6): the confirming message comes back to the composer, and
       *  the pending lane stays so the user can try again. */
      async function handleCancelSwitch(): Promise<void> {
        if (!chatId || isCancelSwitchInFlight) {
          return;
        }
        isCancelSwitchInFlight = true;
        m.redraw();
        try {
          const { returned_block } = await cancelHandoff(chatId);
          prependToComposer(chatId, returned_block);
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to cancel the switch of chat ${chatId}: ${detail}`);
          clearActionFailureNotice();
          actionFailureTitle = "Couldn't cancel the switch";
          actionFailureDetail = detail;
        } finally {
          isCancelSwitchInFlight = false;
          m.redraw();
        }
      }

      function refocusAfterSend(): void {
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
        if (!chatId || isInterruptInFlight) {
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
        const preInterruptBubbleIds = getOutgoingMessages(chatId).map((message) => message.id);
        m.redraw();
        try {
          // Interrupt the agent and pull any queued messages back into the composer,
          // unsent, for the user to edit and send. Empty block = nothing was queued
          // (a clean no-op).
          const { block } = await drainToComposer(chatId);
          // Every not-Delivered message is now back in the composer (or was Delivered and
          // dropped its own bubble); clear the pre-interrupt Sending bubbles so none ghost.
          clearOutgoing(chatId, preInterruptBubbleIds);
          if (block) {
            // Merge instead of drop: prepend the handed-back block above any existing draft
            // (block, blank line, draft) rather than dropping it when the composer is
            // non-empty. Under pi's native retract the messages survive nowhere else, so
            // dropping them here would lose them outright.
            const draft = messageText;
            const merged = draft.trim().length === 0 ? block : `${block}\n\n${draft}`;
            messageText = merged;
            localStorage.setItem(messageTextKey(chatId), merged);
          }
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to interrupt chat ${chatId}: ${detail}`);
          // Surface the failure: they deliberately clicked Stop, and on failure
          // the agent is still running. Same notice as a failed send -- leaving this one as a
          // system alert while its neighbour is a styled notice is worse than either.
          // Never inherit a previous send's recovery: an interrupt has nothing to repeat, and
          // leaving one attached would offer Retry bound to an unrelated message.
          clearActionFailureNotice();
          actionFailureTitle = "Couldn't stop the agent";
          actionFailureDetail = detail;
        } finally {
          isInterruptInFlight = false;
          m.redraw();
        }
      }

      /** Enter and the send button do the same thing: send, or carry the armed switch out. */
      function handleSubmit(): Promise<void> {
        if (switchTarget !== null) {
          return chatId && canSend ? handleSwitchAndSend(switchTarget) : Promise.resolve();
        }
        return handleSend();
      }

      function handleKeydown(event: KeyboardEvent): void {
        if (event.key === "Enter" && !event.shiftKey) {
          event.preventDefault();
          void handleSubmit();
        }
      }

      function handlePaste(event: ClipboardEvent): void {
        if (!chatId) {
          return;
        }
        const files = imageFilesFromClipboard(event.clipboardData);
        if (files.length > 0) {
          event.preventDefault();
          uploadFilesToComposer(chatId, files);
        }
      }

      function openFilePicker(): void {
        fileInputElement?.click();
      }

      function dismissAuthCommandNotice(): void {
        interceptedAuthCommand = null;
        messageText = "";
        if (chatId) {
          localStorage.removeItem(messageTextKey(chatId));
        }
        m.redraw();
      }

      function dismissDeclinedCommandNotice(): void {
        declinedSlashCommand = null;
        m.redraw();
      }

      /** Put a failed message back in the composer, in FRONT of whatever is already there. */
      function restoreFailedMessageToComposer(
        forChatId: string,
        text: string,
        attachments: readonly ComposerAttachment[],
      ): void {
        // Reuses the module-level hand-back that Stop's drain, QueuedMessageView and the switch
        // dialog already go through, rather than inventing a second set of rules for the same job.
        restoreToComposer(forChatId, text, attachments);
        if (currentChatId === forChatId) {
          messageText = localStorage.getItem(messageTextKey(forChatId)) ?? text;
        }
      }

      /** Drop the first occurrence of ``block`` from ``text``, tidying the separator it left. */
      function removeFirstBlock(text: string, block: string): string {
        if (!block) {
          // An attachments-only message has no text to remove, and indexOf("") matches at 0 --
          // which would reformat a draft this never touched.
          return text;
        }
        const at = text.indexOf(block);
        if (at === -1) {
          return text;
        }
        const before = text.slice(0, at);
        const after = text.slice(at + block.length);
        return `${before}${after}`
          .replace(/^\n+/, "")
          .replace(/\n{3,}/g, "\n\n")
          .trimEnd();
      }

      /** Remove just the restored copy once a repeat send has landed, leaving the rest alone. */
      function clearRestoredMessage(
        forChatId: string,
        restoredText: string,
        deliveredAttachments: readonly ComposerAttachment[],
      ): void {
        const deliveredIds = new Set(deliveredAttachments.map((attachment) => attachment.localId));
        // Emphatically NOT "clear the composer". By this point the box can also hold a draft
        // typed while the send was failing, and -- after Force -- the queue block drained out of
        // the harness so the restart would not destroy it. Wiping it wholesale would throw away
        // the very messages this feature exists to protect. Remove the one copy that was just
        // delivered, and leave everything else exactly where it is.
        // Removed wherever it sits, not just at the front: Force drains the harness queue back
        // into the composer BEFORE sending, so by now the delivered message usually has that
        // block above it and a prefix-only strip would leave it behind -- sent, and still in the
        // box. Only the first occurrence goes, so a user who genuinely typed the same text twice
        // keeps their copy.
        const current = localStorage.getItem(messageTextKey(forChatId)) ?? "";
        const withoutRestored = removeFirstBlock(current, restoredText);
        if (withoutRestored) {
          localStorage.setItem(messageTextKey(forChatId), withoutRestored);
        } else {
          localStorage.removeItem(messageTextKey(forChatId));
        }
        // The delivered attachments go regardless of whether text remains. Keying this off the
        // text emptying meant a Retry after the user had typed something left the files behind,
        // to be sent again with whatever they wrote next.
        restoreComposerAttachments(
          forChatId,
          getComposerAttachments(forChatId).filter((attachment) => !deliveredIds.has(attachment.localId)),
        );
        if (currentChatId === forChatId) {
          messageText = withoutRestored;
        }
      }

      function clearActionFailureNotice(): void {
        actionFailureDetail = null;
        actionFailureRecovery = null;
        actionFailureInFlight = null;
        externalRetry = null;
        actionFailureKind = "unknown";
        actionFailureTitle = "Couldn't send your message";
      }

      /** Cancel: give the message back and close. Also what Escape and a backdrop press do. */
      function dismissActionFailureNotice(): void {
        // Never dismiss out from under a running action: the send it started is still in flight
        // and will report its own outcome.
        if (actionFailureInFlight !== null) {
          return;
        }
        // The message went back to the composer when the send failed, so there is nothing to
        // restore here -- Cancel just means "leave it there and let me look at it".
        clearActionFailureNotice();
        m.redraw();
        // Hand focus back to where the user was typing, which the send path skipped while the
        // notice was up.
        focusMessageTextarea();
      }

      /** Retry: the ordinary send again, so it re-runs preflight and can fail again. */
      async function retryFailedSend(): Promise<void> {
        if (actionFailureInFlight !== null) {
          return;
        }
        const recovery = actionFailureRecovery;
        const retryExternal = externalRetry;
        if (recovery === null && retryExternal === null) {
          return;
        }
        actionFailureInFlight = "retry";
        m.redraw();
        if (recovery !== null) {
          await repeatFailedSend(recovery);
          return;
        }
        // A sibling's operation: it knows how to repeat itself, and reports its own failure the
        // same way it reported the first one.
        try {
          await retryExternal!();
          clearActionFailureNotice();
        } catch (err) {
          actionFailureDetail = describeRequestError(err);
          actionFailureInFlight = null;
        }
        m.redraw();
      }

      /**
       * Shared tail of Retry and Force: send the message again and settle the notice.
       *
       * Sends ``sentText`` -- what actually went to the agent, attachment references included --
       * not the typed prose, which would drop the attachments silently.
       */
      async function repeatFailedSend(recovery: SendRecovery): Promise<void> {
        // Paint the same optimistic bubble the normal send path paints, so a retried message is
        // not simply absent from the transcript until the backend catches up.
        const messageId = mintMessageId();
        const outgoingId = addOutgoing(recovery.chatId, recovery.text, messageId);
        try {
          await whenChatRegistered(recovery.chatId);
          await sendMessage(recovery.chatId, recovery.sentText, messageId);
          // Landed, so take the restored copy back out of the composer.
          clearRestoredMessage(recovery.chatId, recovery.text, recovery.attachments);
          clearActionFailureNotice();
          focusMessageTextarea();
        } catch (err) {
          dropOutgoing(recovery.chatId, outgoingId);
          // Failed again. Only re-open the notice if they are still on that agent -- otherwise
          // it would surface this agent's error over a different chat, with no way to act on it.
          // The message is already back in that agent's composer either way.
          if (currentChatId === recovery.chatId) {
            actionFailureDetail = describeRequestError(err);
            // The reason can change between attempts -- a blocked input can become an agent that
            // is gone -- and the buttons follow the kind, so it has to be re-read with the text.
            actionFailureKind = describeRequestErrorKind(err);
            actionFailureInFlight = null;
          } else {
            clearActionFailureNotice();
          }
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
        // Rescue anything queued inside the harness first. The restart below SIGKILLs the agent,
        // which drops that queue -- and a feature whose rule is "never lose the message" must not
        // have a button that quietly loses other people's. Best-effort on purpose: the agent we
        // are about to force is often the one that is stuck, so a drain that fails or is refused
        // must not stop the restart the user actually asked for.
        try {
          const drained = await drainToComposer(recovery.chatId);
          if (drained.block) {
            prependToComposer(recovery.chatId, drained.block);
          }
        } catch {
          // Nothing to do: the restart still goes ahead, and anything queued is lost with it.
        }
        try {
          // The restart itself, unconditionally: the drain does not guarantee one. Claude's
          // empty-queue path is a native chord, and pi/codex hand back a block without ever
          // restarting -- so a non-empty block is not evidence the process was replaced, and a
          // wedged agent is exactly what Force is for. If this is refused (the services agent
          // carries is_primary=true, say) that refusal becomes the notice's text, nothing is sent.
          await interruptAgent(recovery.chatId);
        } catch (err) {
          actionFailureDetail = describeRequestError(err);
          actionFailureInFlight = null;
          m.redraw();
          return;
        }
        await repeatFailedSend(recovery);
      }

      function renderActionFailureNotice(detail: string): m.Children {
        const recovery = actionFailureRecovery;
        // A pane that is gone is not going to be there on the next attempt, so Retry is not
        // offered at all rather than offered and guaranteed to fail. Every other kind -- and
        // anything unclassified -- keeps it.
        const canRetryHelp = actionFailureKind !== "agent_unreachable";
        const isRepeatable = (recovery !== null || externalRetry !== null) && canRetryHelp;
        return m(actionFailureNotice, {
          title: actionFailureTitle,
          body: [
            detail,
            // Only when the detail does not already say what to do. An input_blocked reason is
            // the dialog's own advice ("press Enter in its terminal to run it") -- following it
            // with a vaguer paraphrase of the same instruction is the third time one screen has
            // told the reader to go look at their terminal.
            actionFailureKind === "agent_unreachable"
              ? "The agent's terminal is gone, so restarting it is the only way to deliver this."
              : isRepeatable && actionFailureKind !== "input_blocked"
                ? "You can open the agent's terminal, fix it there, then Retry."
                : null,
          ],
          dismissLabel: isRepeatable || recovery !== null ? "Cancel" : "OK",
          isDismissable: actionFailureInFlight === null,
          onDismiss: dismissActionFailureNotice,
          actions: [
            ...(isRepeatable
              ? [
                  {
                    label: actionFailureInFlight === "retry" ? "Retrying…" : "Retry",
                    tooltip: "Tries the same thing again",
                    isDisabled: actionFailureInFlight !== null,
                    run: () => void retryFailedSend(),
                  },
                ]
              : []),
            // Force needs a message to send afterwards, so it is offered only for our own send --
            // and never for an agent that is merely still starting, where restarting would
            // discard the session it was about to finish bringing up. It is the only thing that
            // helps an agent that is GONE, which is why it survives Retry being withheld.
            ...(recovery === null || actionFailureKind === "not_ready"
              ? []
              : [
                  {
                    label: actionFailureInFlight === "force" ? "Forcing…" : "Force",
                    tooltip: "Restarts agent to reset it & resends message",
                    isDestructive: true,
                    isDisabled: actionFailureInFlight !== null,
                    run: () => void forceFailedSend(),
                  },
                ]),
          ],
        });
      }

      /**
       * What the armed switch will do, above the composer (spec 5.1): the account the next message
       * moves the chat to and the model picked for it, with a way to call the choice off. A handoff
       * also offers a way back into the dialog; a rebind was armed without one, and carries no pick
       * to change.
       */
      function renderSwitchStrip(target: ProviderAccount, isHandoff: boolean): m.Children {
        const pick = chatId ? getPendingPick(chatId) : null;
        const destination = pick === null ? target.label : `${target.label}, ${pick.label}`;
        return m(
          "div",
          {
            class:
              "message-input-switch-strip mx-1 mb-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 " +
              "text-(length:--font-size-helper) text-secondary",
            role: "status",
          },
          [
            m(
              "span",
              { class: "message-input-switch-strip-text" },
              `Your next message switches this chat to ${destination}`,
            ),
            isHandoff
              ? m(
                  Button,
                  {
                    variant: "ghost",
                    sm: true,
                    extra: "message-input-switch-change",
                    onclick: () => {
                      if (chatId) openSwitchDialog(chatId, target);
                    },
                  },
                  "Change",
                )
              : null,
            m(
              Button,
              {
                variant: "ghost",
                sm: true,
                extra: "message-input-switch-cancel",
                onclick: () => {
                  if (chatId) setPendingAccount(chatId, null);
                },
              },
              "Cancel",
            ),
          ],
        );
      }

      function renderDeclinedCommandNotice(declined: { command: string; body: string | null }): m.Children {
        return m(declinedCommandNotice, {
          title: `${declined.command} can't be sent from chat`,
          body: [declined.body ?? "You can still send it from the agent's terminal."],
          dismissLabel: "OK",
          onDismiss: dismissDeclinedCommandNotice,
        });
      }

      function renderAuthCommandNotice(command: string): m.Children {
        return m(authCommandNotice, {
          title: command === "/logout" ? "Sign-out is managed here" : "Sign-in is managed here",
          body: [
            `Sending ${command} to the agent would run its own auth flow inside the agent's terminal, ` +
              "where this workspace cannot see the result. Sign in from the provider list instead: " +
              "it signs in to a fresh account of its own, so the agent's own credential is left alone.",
          ],
          dismissLabel: "Cancel",
          onDismiss: dismissAuthCommandNotice,
          actions: [
            {
              label: "Open providers",
              run: () => {
                dismissAuthCommandNotice();
                openProviderChooser();
              },
            },
          ],
        });
      }

      const attachments = getComposerAttachments(chatId);
      const hasMessageText = messageText.trim().length > 0;
      const canSend = hasMessageText || hasReadyAttachments(chatId);

      const chat = getChatById(chatId);
      // The switch the chat is in the middle of, if any, and the one the next send would start.
      // Nothing is pending once a switch is running: the lane was applied by confirming it.
      const handoff = chat?.handoff ?? null;
      const switchTarget = handoff === null ? pendingSwitchTarget(chatId) : null;

      // The stop button is only meaningful while the agent has an interruptible
      // turn in progress -- the same condition that drives the activity indicator
      // above the input, read straight off the backend-derived activity state. While the
      // chat switches harness the turn in progress is the switch's own (the summary it asked
      // for), so the button is the switch's cancel instead, for as long as that is possible.
      const isAgentWorking = isWorkingActivityState(chat?.active_agent.activity_state ?? null);
      const isStopButtonVisible = isAgentWorking && !isInterruptInFlight && handoff === null;
      const isCancelSwitchVisible = handoff !== null && isHandoffCancellable(handoff);
      // Read straight off the backend's queue snapshot -- the frontend holds no queued state.
      const hasQueuedMessages = (chat?.active_agent.queued_messages ?? []).length > 0;
      const stopButtonLabel = hasQueuedMessages
        ? "Interrupt agent and bring queued messages to draft area"
        : "Interrupt agent";

      return m(
        "div",
        { class: "message-input mx-auto w-full max-w-[calc(var(--width-message-column)+2*var(--radius-xl))]" },
        [
          interceptedAuthCommand !== null ? renderAuthCommandNotice(interceptedAuthCommand) : null,
          declinedSlashCommand !== null ? renderDeclinedCommandNotice(declinedSlashCommand) : null,
          actionFailureDetail !== null ? renderActionFailureNotice(actionFailureDetail) : null,
          switchTarget !== null
            ? renderSwitchStrip(switchTarget, chat === undefined || switchKind(chat, switchTarget) === "handoff")
            : null,
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
              uploadFilesToComposer(chatId, input.files);
              input.value = "";
            },
          }),
          m("div", { class: INPUT_BOX_CLASS }, [
            attachments.length > 0
              ? m(
                  "div",
                  { class: "message-input-attachments flex flex-wrap gap-2 pt-3 pr-3 pl-4" },
                  attachments.map((attachment) => renderComposerAttachment(chatId, attachment)),
                )
              : null,
            m("div", { class: "message-input-row flex flex-row items-center" }, [
              m("textarea", {
                class:
                  "message-input-textbox flex-1 resize-none border-none bg-transparent pt-3.5 pr-2 pb-3.5 pl-5 " +
                  "font-sans text-(length:--font-size-body) leading-normal text-primary focus:outline-none " +
                  "placeholder:text-faint",
                placeholder:
                  handoff !== null
                    ? handoffComposerPlaceholder(handoff)
                    : isAgentWorking
                      ? "Type to queue more messages..."
                      : "Type a message...",
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
                  localStorage.setItem(messageTextKey(chatId), messageText);
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
                    ...hoverTooltipAttrs("Attach files", "above"),
                    "aria-label": "Attach files",
                    onclick: openFilePicker,
                  },
                  m.trust(icon("attach", { size: 18 })),
                ),
                isCancelSwitchVisible
                  ? m(
                      Button,
                      {
                        variant: "secondary",
                        sm: true,
                        extra: "message-input-cancel-switch-button shrink-0",
                        readonly: isCancelSwitchInFlight,
                        ...hoverTooltipAttrs(
                          "Keep this chat on its current agent; your message comes back here",
                          "above",
                        ),
                        "aria-label": "Cancel switch",
                        onclick: () => void handleCancelSwitch(),
                      },
                      isCancelSwitchInFlight ? "Cancelling…" : "Cancel switch",
                    )
                  : null,
                isStopButtonVisible
                  ? m(
                      Button,
                      {
                        variant: "stop",
                        icon: true,
                        round: true,
                        sm: true,
                        extra: "message-input-stop-button shrink-0",
                        // The label states what THIS press will do. The button always interrupts;
                        // it only hands messages back when there are some parked in the harness,
                        // so promising that unconditionally described a case that usually is not
                        // the one in front of the user.
                        ...hoverTooltipAttrs(stopButtonLabel, "above"),
                        "aria-label": stopButtonLabel,
                        onclick: handleStopToComposer,
                      },
                      m.trust(stopIcon(14)),
                    )
                  : null,
                // The send button reads "Switch and send" while a pending lane differs from the
                // chat's harness (spec 5.1): the next send moves the chat, so the button says so.
                canSend && switchTarget !== null
                  ? m(
                      Button,
                      {
                        variant: "primary",
                        sm: true,
                        extra: "message-input-send-button message-input-send-button--switch shrink-0",
                        ...hoverTooltipAttrs(`Switch this chat to ${switchTarget.label} and send`, "above"),
                        "aria-label": "Switch and send",
                        onclick: handleSubmit,
                      },
                      [
                        m("span", "Switch and send"),
                        m(
                          "span",
                          { class: "ml-1.5 inline-flex items-center" },
                          m.trust(icon("send", { size: 14, strokeWidth: 2.5 })),
                        ),
                      ],
                    )
                  : canSend
                    ? m(
                        Button,
                        {
                          variant: "primary",
                          icon: true,
                          round: true,
                          extra: "message-input-send-button shrink-0",
                          ...hoverTooltipAttrs("Send message", "above"),
                          "aria-label": "Send message",
                          onclick: handleSubmit,
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
