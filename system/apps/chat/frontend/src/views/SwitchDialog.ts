/**
 * The dialog a provider choice opens (spec 5.1): "Switch to Codex?" with the model the successor
 * should run on, "Switch this chat" as the thing it is for, and "Start a new chat" for leaving
 * this chat alone. One dialog per page, opened by the provider menu and by the composer's
 * "Change" link, so both open the same one.
 *
 * "Switch this chat" applies nothing yet: it arms the pending switch (``PendingLane``), which the
 * composer's next send carries out. A chat that has had no user turn skips the dialog entirely
 * (``beginSwitchTo``): there is no context to hand over, so the switch runs at once.
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
import { getEventsForChat, mintMessageId } from "../models/Response";
import { startChatOnAccount } from "../shell";
import { harnessLabel } from "./agent-switch-chip";
import { prependToComposer, raiseFailureNotice, takeComposerDraft } from "./MessageInput";
import { hasUserTurn } from "./turn-grouping";

/** The value of the model select's first row: the target harness's own default. */
const DEFAULT_MODEL_VALUE = "";

interface OpenDialog {
  chatId: string;
  target: ProviderAccount;
  /** The successor's pickable models, once fetched; null while loading. */
  options: CatalogModelOption[] | null;
  modelId: string;
  effort: string | null;
  fast: boolean;
  isBusy: boolean;
}

let open: OpenDialog | null = null;

/**
 * Switch ``chatId`` to ``target``, or ask first. A chat with no user turn yet has nothing to
 * hand over, so it switches at once with no summary and no dialog; the draft, if any, stays in
 * the composer and goes out normally once the chat runs on the new account. Any other chat gets
 * the dialog.
 */
export function beginSwitchTo(chatId: string, target: ProviderAccount): void {
  if (!hasUserTurn(getEventsForChat(chatId))) {
    void switchFreshChat(chatId, target);
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

/** Open the dialog for ``chatId`` moving to ``target``; a handoff's picker loads the target's models. */
export function openSwitchDialog(chatId: string, target: ProviderAccount): void {
  const chat = getChatById(chatId);
  const isHandoff = chat === undefined || switchKind(chat, target) === "handoff";
  open = {
    chatId,
    target,
    options: isHandoff ? null : [],
    modelId: DEFAULT_MODEL_VALUE,
    effort: null,
    fast: false,
    isBusy: false,
  };
  m.redraw();
  if (!isHandoff) return;
  void fetchAccountModelOptions(target.id)
    .catch((error) => {
      console.warn(`Failed to load the models of account ${target.id}`, error);
      return [] as CatalogModelOption[];
    })
    .then((options) => {
      if (open !== null && open.chatId === chatId && open.target.id === target.id) open.options = options;
      m.redraw();
    });
}

export function closeSwitchDialog(): void {
  open = null;
  m.redraw();
}

export function isSwitchDialogOpen(chatId: string): boolean {
  return open !== null && open.chatId === chatId;
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

/** Leave this chat as it is and open a new one on the target, on the picked model, with the draft moved over. */
async function startNewChat(dialog: OpenDialog): Promise<void> {
  const pick = pickOf(dialog);
  dialog.isBusy = true;
  m.redraw();
  const draft = takeComposerDraft(dialog.chatId);
  setPendingAccount(dialog.chatId, null);
  const isStarted = await startChatOnAccount(dialog.target.id, draft, pick?.identity ?? null);
  if (!isStarted) prependToComposer(dialog.chatId, draft);
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
      const kind = chat === undefined ? "handoff" : switchKind(chat, current.target);
      const from = harnessLabel(chat?.active_agent.harness ?? "");
      const target = current.target;
      const isRebind = kind === "rebind";
      return m(
        dialog,
        {
          title: isRebind ? `Switch to ${target.label}?` : `Switch to ${harnessLabel(target.harness)}?`,
          body: isRebind
            ? [`${from} restarts on ${target.label} and keeps this conversation.`]
            : [
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
              tooltip: isRebind
                ? `Your next message restarts ${from} on ${target.label}`
                : `Your next message moves this chat to ${target.label}`,
              isDisabled: current.isBusy,
              run: () => {
                setPendingSwitch(current.chatId, target.id, isRebind ? null : pickOf(current));
                closeSwitchDialog();
              },
            },
          ],
        },
        isRebind ? null : renderPicker(current),
      );
    },
  };
}
