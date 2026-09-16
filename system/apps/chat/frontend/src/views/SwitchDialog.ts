/**
 * The dialog a handoff opens (spec 5.1): "Switch to Codex?" with the model the successor should
 * run on, "Switch this chat" as the thing it is for, and "Start a new chat" for leaving this chat
 * alone. One dialog per page, opened by the provider menu and by the composer's "Change" link, so
 * both open the same one.
 *
 * "Switch this chat" applies nothing yet: it arms the pending switch (``PendingLane``), which the
 * composer's next send carries out. Only a switch that will write a summary asks (``beginSwitchTo``):
 * a chat that has had no user turn has no context to hand over and switches at once, and a rebind
 * keeps the agent and its conversation, so it is armed at once instead.
 */

import m from "mithril";
import { makeNoticeDialog } from "@imbue/workspace-ui/src/components/NoticeDialog";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { describeRequestError } from "@imbue/workspace-ui/src/models/request-error";
import { fetchAccountModelOptions } from "../models/AccountModelOptions";
import { getChatById } from "../models/Chats";
import type { ChatSnapshot } from "../models/Chats";
import { switchChat } from "../models/Handoffs";
import type { CatalogModelOption } from "../models/HarnessCatalog";
import type { ModelIdentity } from "../models/ModelSettings";
import { setPendingAccount, setPendingSwitch, switchKind } from "../models/PendingLane";
import type { PendingPick } from "../models/PendingLane";
import type { ProviderAccount } from "../models/Providers";
import { getEventsForChat, isTranscriptLoaded, mintMessageId } from "../models/Response";
import { startChatOnAccount } from "../shell";
import { harnessLabel } from "./harness-labels";
import { raiseFailureNotice, restoreComposerDraft, takeComposerDraft } from "./MessageInput";
import { hasUserTurn } from "./turn-grouping";

/** The value of the model select's first row: the target harness's own default. */
const DEFAULT_MODEL_VALUE = "";

interface OpenDialog {
  chatId: string;
  target: ProviderAccount;
  /** The successor's pickable models, once fetched; null while loading. */
  options: CatalogModelOption[] | null;
  /** Why the models could not be fetched, when they could not be; null otherwise. */
  optionsError: string | null;
  modelId: string;
  effort: string | null;
  fast: boolean;
  isBusy: boolean;
}

let open: OpenDialog | null = null;

/**
 * Switch ``chatId`` to ``target``, arm the switch, or ask first. A chat with no user turn yet has
 * nothing to hand over, so it switches at once with no summary and no dialog; the draft, if any,
 * stays in the composer and goes out normally once the chat runs on the new account. A rebind is
 * armed at once with no dialog: the next send carries it out, so a turn in progress is not cut
 * short by the press. A handoff with context gets the dialog.
 */
export function beginSwitchTo(chatId: string, target: ProviderAccount): void {
  // Only a loaded transcript can say there is no user turn: an unloaded (or failed) one reads as
  // empty, and switching a chat of hundreds of turns without asking is the worse mistake of the two.
  if (isTranscriptLoaded(chatId) && !hasUserTurn(getEventsForChat(chatId))) {
    void switchFreshChat(chatId, target);
    return;
  }
  const chat = getChatById(chatId);
  if (chat !== undefined && switchKind(chat, target) === "rebind") {
    setPendingSwitch(chatId, target.id, null);
    m.redraw();
    return;
  }
  openSwitchDialog(chatId, target);
}

async function switchFreshChat(chatId: string, target: ProviderAccount): Promise<void> {
  setPendingAccount(chatId, null);
  try {
    await switchChat(chatId, target.id, "", mintMessageId());
  } catch (error) {
    raiseFailureNotice(chatId, {
      title: `Couldn't switch to ${target.label}`,
      detail: describeRequestError(error),
    });
  }
  m.redraw();
}

/** Open the dialog for ``chatId`` handing off to ``target``; the picker loads the target's models. */
export function openSwitchDialog(chatId: string, target: ProviderAccount): void {
  open = {
    chatId,
    target,
    options: null,
    optionsError: null,
    modelId: DEFAULT_MODEL_VALUE,
    effort: null,
    fast: false,
    isBusy: false,
  };
  m.redraw();
  void fetchAccountModelOptions(target.id)
    .then((options) => {
      if (open !== null && open.chatId === chatId && open.target.id === target.id) open.options = options;
    })
    .catch((error: unknown) => {
      console.warn(`Failed to load the models of account ${target.id}`, error);
      // Said, not swallowed: an empty option list means "this harness offers no pick", which is a
      // claim about the harness rather than a report that the request failed.
      if (open !== null && open.chatId === chatId && open.target.id === target.id) {
        open.options = [];
        open.optionsError = describeRequestError(error);
      }
    })
    .finally(() => {
      m.redraw();
    });
}

export function closeSwitchDialog(): void {
  open = null;
  m.redraw();
}

/** The chosen option, or null for the default. */
function chosenOption(dialog: OpenDialog): CatalogModelOption | null {
  return (dialog.options ?? []).find((option) => option.id === dialog.modelId) ?? null;
}

/** The pick the dialog's state amounts to: null for the default, else the identity and the label the page shows. */
function pickOf(dialog: OpenDialog): PendingPick | null {
  const option = chosenOption(dialog);
  if (option === null) return null;
  const identity: ModelIdentity = {
    model_id: option.id,
    effort: option.efforts.length > 0 ? dialog.effort : null,
    fast: option.supports_fast ? dialog.fast : false,
  };
  const effortPart = identity.effort === null ? "" : ` · ${capitalize(identity.effort)}`;
  return { identity, label: `${option.label}${effortPart}${identity.fast ? " · fast" : ""}` };
}

function capitalize(level: string): string {
  return level.length === 0 ? level : level[0].toUpperCase() + level.slice(1);
}

/** The effort to start from when a model is chosen: the first shown, else the first declared. */
function firstEffort(option: CatalogModelOption): string | null {
  const shown = option.efforts.filter((effort) => effort.in_picker);
  return (shown[0] ?? option.efforts[0])?.level ?? null;
}

function renderPicker(dialog: OpenDialog): m.Children {
  if (dialog.options === null) {
    return m(
      "p",
      { class: "switch-dialog-loading text-(length:--font-size-helper) text-secondary" },
      "Loading models…",
    );
  }
  if (dialog.optionsError !== null) {
    return m(
      "p",
      { class: "switch-dialog-models-failed text-(length:--font-size-helper) text-secondary" },
      `Could not load ${harnessLabel(dialog.target.harness)}'s models (${dialog.optionsError}). ` +
        "It starts on its default model; you can change it once it is running.",
    );
  }
  if (dialog.options.length === 0) {
    return m(
      "p",
      { class: "switch-dialog-no-models text-(length:--font-size-helper) text-secondary" },
      `${harnessLabel(dialog.target.harness)} starts on its default model; you can change it once it is running.`,
    );
  }
  const option = chosenOption(dialog);
  const shownEfforts = option === null ? [] : option.efforts.filter((effort) => effort.in_picker);
  return m("div", { class: "switch-dialog-picker mb-4 flex flex-wrap items-center gap-2" }, [
    m(
      "select",
      {
        class: inputClass({ extra: "switch-dialog-model" }),
        "aria-label": "Model",
        value: dialog.modelId,
        onchange: (event: Event) => {
          dialog.modelId = (event.target as HTMLSelectElement).value;
          const chosen = chosenOption(dialog);
          dialog.effort = chosen === null ? null : firstEffort(chosen);
          dialog.fast = false;
        },
      },
      [
        m("option", { value: DEFAULT_MODEL_VALUE, selected: dialog.modelId === DEFAULT_MODEL_VALUE }, "Default model"),
        ...dialog.options.map((candidate) =>
          m("option", { value: candidate.id, selected: candidate.id === dialog.modelId }, candidate.label),
        ),
      ],
    ),
    shownEfforts.length > 1
      ? m(
          "select",
          {
            class: inputClass({ extra: "switch-dialog-effort" }),
            "aria-label": "Reasoning effort",
            value: dialog.effort ?? "",
            onchange: (event: Event) => {
              dialog.effort = (event.target as HTMLSelectElement).value;
            },
          },
          shownEfforts.map((effort) =>
            m("option", { value: effort.level, selected: effort.level === dialog.effort }, capitalize(effort.level)),
          ),
        )
      : null,
    option !== null && option.supports_fast
      ? m("label", { class: "switch-dialog-fast flex items-center gap-1.5 text-(length:--font-size-helper)" }, [
          m("input", {
            type: "checkbox",
            checked: dialog.fast,
            onchange: (event: Event) => {
              dialog.fast = (event.target as HTMLInputElement).checked;
            },
          }),
          "Fast mode",
        ])
      : null,
  ]);
}

/** Leave this chat as it is and open a new one on the target, on the picked model, with the draft --
 *  its attachments included -- moved over. A draft the composer refuses to give up leaves the dialog
 *  where it is, with the composer's own notice saying why. */
async function startNewChat(dialog: OpenDialog): Promise<void> {
  const pick = pickOf(dialog);
  dialog.isBusy = true;
  m.redraw();
  const draft = await takeComposerDraft(dialog.chatId);
  if (draft === null) {
    dialog.isBusy = false;
    m.redraw();
    return;
  }
  setPendingAccount(dialog.chatId, null);
  const isStarted = await startChatOnAccount(dialog.target.id, draft.finalText, pick?.identity ?? null);
  if (!isStarted) restoreComposerDraft(dialog.chatId, draft);
  if (open === dialog) open = null;
  m.redraw();
}

export function SwitchDialog(): m.Component<{ chatId: string }> {
  const dialog = makeNoticeDialog();
  return {
    view(vnode) {
      const current = open;
      if (current === null || current.chatId !== vnode.attrs.chatId) return null;
      const chat: ChatSnapshot | undefined = getChatById(current.chatId);
      const from = harnessLabel(chat?.active_agent.harness ?? "");
      const target = current.target;
      return m(
        dialog,
        {
          title: `Switch to ${harnessLabel(target.harness)}?`,
          body: [
            `${from} wraps up what it is doing and hands the conversation to ${target.label}, ` +
              "starting with your next message.",
          ],
          dismissLabel: "Cancel",
          isDismissable: !current.isBusy,
          onDismiss: closeSwitchDialog,
          actions: [
            {
              label: current.isBusy ? "Starting…" : "Start a new chat",
              tooltip: `Leaves this chat as it is and opens a new one on ${target.label}, with your draft`,
              isSecondary: true,
              isDisabled: current.isBusy,
              run: () => void startNewChat(current),
            },
            {
              label: "Switch this chat",
              tooltip: `Your next message moves this chat to ${target.label}`,
              isDisabled: current.isBusy,
              run: () => {
                setPendingSwitch(current.chatId, target.id, pickOf(current));
                closeSwitchDialog();
              },
            },
          ],
        },
        renderPicker(current),
      );
    },
  };
}
