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
import { interruptAgent, sendMessage, getEventsForAgent } from "../models/Response";
import {
  addPendingMessage,
  getEffectiveActivityState,
  markPendingMessageQueued,
  removePendingMessage,
} from "../models/PendingMessages";
import { describeRequestError } from "../models/request-error";
import { effectiveChoice, isPickInFlight, setModelChoice } from "../models/ModelSettings";
import type { ModelIdentity } from "../models/ModelSettings";
import { ensureHarnessCatalogs, getHarnessCatalog } from "../models/HarnessCatalog";
import type { CatalogModelOption, HarnessCatalog } from "../models/HarnessCatalog";
import { getAgentById } from "../models/AgentManager";
import { openLoginModal } from "../models/ClaudeAuth";
import { findDeclinedSlashCommand } from "../models/claudeSlashCommands";
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

/** The effort to carry when switching to `option`: keep the current one if the new
 *  model declares it, else the model's first shown (or first declared) effort. Null
 *  when the model has no effort axis. */
function clampEffort(option: CatalogModelOption, currentEffort: string | null): string | null {
  if (option.efforts.length === 0) {
    return null;
  }
  if (currentEffort !== null && option.efforts.some((effort) => effort.level === currentEffort)) {
    return currentEffort;
  }
  const shown = option.efforts.filter((effort) => effort.in_picker);
  return (shown[0] ?? option.efforts[0]).level;
}

function capitalizeEffort(level: string): string {
  return level.length === 0 ? level : level[0].toUpperCase() + level.slice(1);
}

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
  // Which model-bar dropdown is open (the model picker, the effort picker, or
  // none), and the bar element used to detect an outside click closing it.
  let openModelBarDropdown: "model" | "effort" | null = null;
  let modelBarElement: HTMLElement | null = null;

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

  // Stable reference (defined once for the component's life) so the dropdown's
  // add/removeEventListener pair to the same function -- a per-render closure
  // would leak a listener each time the dropdown reopens.
  function handleModelOutsideMousedown(event: MouseEvent): void {
    if (modelBarElement !== null && !modelBarElement.contains(event.target as Node)) {
      openModelBarDropdown = null;
      m.redraw();
    }
  }

  // The model bar lives in the composer toolbar: [Logo][Model][Effort][Fast].
  // Everything it shows is data -- the static per-harness catalog (logo, options,
  // efforts, switch mode) from HarnessCatalog.ts, plus the agent's live model_choice
  // pushed onto the agents store. Which slots show is decided purely by the matched
  // catalog option (effort iff the model declares efforts; fast iff it supports fast);
  // the switch mode only decides whether the shown slots are interactive. A pick is
  // applied optimistically and reconciled from the pushed live choice (ModelSettings.ts).

  // A dropdown slot (the model picker or the effort picker), styled like the old
  // model selector. `openModelBarDropdown` names which one is open, if any.
  function renderModelBarDropdown(opts: {
    kind: "model" | "effort";
    triggerLabel: string;
    header: string;
    items: { id: string; label: string }[];
    selectedId: string | null;
    interactive: boolean;
    tooltip: string;
    onPick: (id: string) => void;
  }): m.Vnode {
    const isOpen = openModelBarDropdown === opts.kind;
    return m("div", { class: "model-selector-wrapper" }, [
      m(
        "button",
        {
          type: "button",
          class: "model-selector-trigger",
          disabled: !opts.interactive,
          "data-tooltip": opts.tooltip,
          onclick: (event: MouseEvent) => {
            event.stopPropagation();
            openModelBarDropdown = isOpen ? null : opts.kind;
          },
        },
        [
          m("span", { class: "model-selector-label" }, opts.triggerLabel),
          m("span", { class: "model-selector-chevron" }, m.trust(icon("chevron-down", { size: 12 }))),
        ],
      ),
      isOpen && opts.interactive
        ? m(
            "div",
            {
              class: "model-selector-dropdown",
              oncreate: () => document.addEventListener("mousedown", handleModelOutsideMousedown),
              onremove: () => document.removeEventListener("mousedown", handleModelOutsideMousedown),
            },
            [
              m("div", { class: "model-selector-dropdown-header" }, opts.header),
              m(
                "ul",
                { class: "model-selector-dropdown-list" },
                opts.items.map((item) =>
                  m(
                    "li",
                    {
                      key: item.id,
                      class:
                        "model-selector-option" +
                        (opts.selectedId === item.id ? " model-selector-option--selected" : ""),
                      onclick: () => {
                        openModelBarDropdown = null;
                        if (opts.selectedId !== item.id) {
                          opts.onPick(item.id);
                        }
                      },
                    },
                    item.label,
                  ),
                ),
              ),
            ],
          )
        : null,
    ]);
  }

  function renderModelBar(agentId: string): m.Vnode | null {
    const agent = getAgentById(agentId);
    const catalog: HarnessCatalog | null = getHarnessCatalog(agent?.harness);
    if (catalog === null) {
      // No catalog (feature-flagged off, or catalogs not loaded yet): no bar.
      return null;
    }
    const logo = m("span", { class: "model-bar-logo", "aria-hidden": "true" }, m.trust(catalog.icon_svg));

    const choice = effectiveChoice(agentId, agent?.model_choice);
    if (choice === null) {
      // The live selection has not resolved yet; show the logo alone.
      return m("div", { class: "model-bar" }, logo);
    }
    const matched = choice.matched;
    if (matched === null) {
      // The current combo matches no catalog option: a shrug, no model/effort/fast.
      return m("div", { class: "model-bar" }, [
        logo,
        m("span", { class: "model-bar-shrug", "data-tooltip": "Unrecognized model" }, "\u{1F937}"),
      ]);
    }

    const interactive = catalog.switch_mode !== "read_only" && !isPickInFlight(agentId);
    const currentEffort = choice.identity.effort;
    const currentFast = choice.identity.fast;

    const modelSlot = renderModelBarDropdown({
      kind: "model",
      triggerLabel: matched.label,
      header: "Model",
      items: catalog.options
        .filter((option) => option.in_picker)
        .map((option) => ({ id: option.id, label: option.label })),
      selectedId: matched.id,
      interactive,
      tooltip: "Select model",
      onPick: (modelId) => {
        const option = catalog.options.find((candidate) => candidate.id === modelId);
        if (option === undefined) {
          return;
        }
        // Clamp effort into the new model's declared set, and drop fast if the new
        // model does not support it (the backend validates the same).
        const nextIdentity: ModelIdentity = {
          model_id: option.id,
          effort: clampEffort(option, currentEffort),
          fast: option.supports_fast ? currentFast : false,
        };
        setModelChoice(agentId, nextIdentity, option);
      },
    });

    const shownEfforts = matched.efforts.filter((effort) => effort.in_picker);
    const effortSlot =
      matched.efforts.length > 0
        ? renderModelBarDropdown({
            kind: "effort",
            triggerLabel: currentEffort === null ? "Effort" : capitalizeEffort(currentEffort),
            header: "Effort",
            items: shownEfforts.map((effort) => ({ id: effort.level, label: capitalizeEffort(effort.level) })),
            selectedId: currentEffort,
            interactive,
            tooltip: "Select reasoning effort",
            onPick: (level) => {
              const nextIdentity: ModelIdentity = { model_id: matched.id, effort: level, fast: currentFast };
              setModelChoice(agentId, nextIdentity, matched);
            },
          })
        : null;

    const fastSlot = matched.supports_fast
      ? m(
          "button",
          {
            type: "button",
            class: `fast-toggle${currentFast ? " fast-toggle--on" : ""}`,
            disabled: !interactive,
            "data-tooltip": currentFast ? "Disable fast mode" : "Enable fast mode",
            "aria-label": currentFast ? "Disable fast mode" : "Enable fast mode",
            "aria-pressed": currentFast ? "true" : "false",
            onclick: () => {
              const nextIdentity: ModelIdentity = { model_id: matched.id, effort: currentEffort, fast: !currentFast };
              setModelChoice(agentId, nextIdentity, matched);
            },
          },
          m.trust(icon("zap", { size: 16 })),
        )
      : null;

    return m(
      "div",
      {
        class: "model-bar",
        oncreate: (barVnode: m.VnodeDOM) => {
          modelBarElement = barVnode.dom as HTMLElement;
        },
        onremove: () => {
          modelBarElement = null;
        },
      },
      [logo, modelSlot, effortSlot, fastSlot],
    );
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
        openModelBarDropdown = null;
        // The notice names a command typed for the previous agent, so it must not follow the user
        // to the next one.
        declinedSlashCommand = null;
        // Load the static per-harness model catalogs once; the live selection
        // rides the agents store, so there is nothing per-agent to fetch.
        void ensureHarnessCatalogs();
      }

      async function handleSend(): Promise<void> {
        if (!agentId) {
          return;
        }
        const trimmedCommand = messageText.trim().toLowerCase();
        if (trimmedCommand === "/login" || trimmedCommand === "/logout") {
          interceptedAuthCommand = trimmedCommand;
          m.redraw();
          return;
        }
        const declined = findDeclinedSlashCommand(messageText);
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
        // Show the message immediately (and force "Thinking..." if the agent is
        // idle) instead of waiting for it to round-trip through the transcript.
        const pendingId = addPendingMessage(agentId, finalText, getEventsForAgent(agentId));
        m.redraw();

        try {
          await sendMessage(agentId, finalText);
          // The POST resolves once the backend confirms the agent accepted the
          // message into its queue, so move the bubble to "queued". It stays up
          // until the real transcript event reconciles it away -- that is when
          // the agent has genuinely received it (the user-facing "sent").
          if (pendingId !== null) {
            markPendingMessageQueued(agentId, pendingId);
          }
        } catch (err) {
          // The send genuinely failed (the backend confirms delivery before
          // resolving, so a rejection means the message was NOT accepted). Roll
          // the optimistic bubble back (clearing the forced-"Thinking..."
          // override) so the UI does not show a message that was never
          // delivered, and surface the real error.
          const detail = describeRequestError(err);
          console.error(`Failed to send message to agent ${agentId}: ${detail}`);
          if (pendingId !== null) {
            removePendingMessage(agentId, pendingId);
          }
          // Restore the user's text and attachments so the send is not silently
          // lost -- but only if they have not already started a new draft for
          // this agent (the input was cleared at send time, so during the
          // in-flight request the user may have typed or attached something
          // new; blindly restoring would clobber that newer draft).
          const currentDraft =
            currentAgentId === agentId ? messageText : (localStorage.getItem(messageTextKey(agentId)) ?? "");
          const isComposerEmpty = currentDraft.trim().length === 0 && getComposerAttachments(agentId).length === 0;
          if (isComposerEmpty) {
            localStorage.setItem(messageTextKey(agentId), sentText);
            restoreComposerAttachments(agentId, sentAttachments);
            if (currentAgentId === agentId) {
              messageText = sentText;
              m.redraw();
            }
          }
          // Surface the failure to the user with an explicit signal: the bubble
          // vanishing on its own is too subtle to read as "your message did not
          // send." Matches the alert-based feedback convention for user-initiated
          // mutations in this file (see handleInterrupt).
          alert(`Failed to send message: ${detail}`);
        }

        requestAnimationFrame(() => {
          focusMessageTextarea();
        });
      }

      async function handleInterrupt(): Promise<void> {
        if (!agentId || isInterruptInFlight) {
          return;
        }
        // Hide the stop button until the restart request settles so the user
        // cannot fire off multiple restarts in quick succession.
        isInterruptInFlight = true;
        m.redraw();
        try {
          await interruptAgent(agentId);
        } catch (err) {
          const detail = describeRequestError(err);
          console.error(`Failed to interrupt agent ${agentId}: ${detail}`);
          // Surface the failure to the user: they deliberately clicked Stop,
          // and on failure the agent is still running. Matches the alert-based
          // feedback convention for user-initiated mutations (see executeDestroy).
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
      // turn in progress -- the same condition that drives the activity
      // indicator above the input. Use the effective state so a just-sent
      // message that forced "Thinking..." also surfaces the stop button, keeping
      // the two in lockstep. Hide it whenever the agent is idle.
      const isAgentWorking = isWorkingActivityState(getEffectiveActivityState(agentId));
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
              placeholder: "Type a message...",
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
              renderModelBar(agentId),
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
                      "data-tooltip": "Interrupt",
                      "aria-label": "Interrupt",
                      onclick: handleInterrupt,
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
