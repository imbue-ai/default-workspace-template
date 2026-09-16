/**
 * The fast-mode chooser: a small modal, opened from the model picker's fast row, with the
 * chat's three modes (models/FastMode.ts). Choosing applies at once (views/fast-mode-limit.ts),
 * so the modal needs no confirm; under Auto the workspace's turn limit is editable, and any mode
 * can be made the default for new chats.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { inputClass } from "@imbue/workspace-ui/src/components/Input";
import { MODAL_MESSAGE_CLASS, Modal } from "@imbue/workspace-ui/src/components/Modal";
import {
  DEFAULT_CHAT_SETTINGS,
  ensureChatSettings,
  getChatSettings,
  updateChatSettings,
} from "../models/ChatSettings";
import type { ChatFastModeState, FastModeMode } from "../models/FastMode";
import { ensureFastModeState, getFastModeState } from "../models/FastMode";
import { getEventsForChat } from "../models/Response";
import { chooseFastMode } from "./fast-mode-limit";

const OPTION_CLASS =
  "fast-mode-option flex w-full cursor-pointer items-start gap-3 rounded-md border px-3 py-2 text-left " +
  "hover:bg-fill-hover focus-visible:outline-2 focus-visible:outline-accent";
const OPTION_SELECTED_CLASS = "border-accent bg-fill-subtle";
const OPTION_UNSELECTED_CLASS = "border-default";

/** The line under each mode; auto's names the limit it runs to. */
export function fastModeDetail(mode: FastModeMode, turnLimit: number): string {
  if (mode === "off") return "Standard speed for the whole chat.";
  if (mode === "on") return "Fast for the whole chat.";
  const turns = turnLimit === 1 ? "1 turn" : `${turnLimit} turns`;
  return `Fast for the first ${turns}, then standard speed.`;
}

const MODES: readonly { mode: FastModeMode; label: string }[] = [
  { mode: "off", label: "Off" },
  { mode: "auto", label: "Auto" },
  { mode: "on", label: "On" },
];

export function FastModeModal(): m.Component<{ chatId: string; onClose: () => void }> {
  // What the turn-limit field shows while it is being typed into, or null when it shows the
  // settings' limit: mithril re-asserts `value` on every redraw, and every keystroke causes one.
  let limitDraft: string | null = null;

  return {
    view(vnode) {
      const { chatId, onClose } = vnode.attrs;
      const settings = getChatSettings();
      if (settings === null) void ensureChatSettings();
      const known = getFastModeState(chatId);
      if (known === null) void ensureFastModeState(chatId);
      const effectiveSettings = settings ?? DEFAULT_CHAT_SETTINGS;
      const state: ChatFastModeState = known ?? { mode: effectiveSettings.fast_mode_default, is_switched: false };
      const limit = effectiveSettings.fast_mode_turn_limit;
      const applyLimit = (raw: string): void => {
        const current = getChatSettings();
        if (current === null) return;
        const parsed = Number.parseInt(raw, 10);
        if (Number.isNaN(parsed) || parsed < 1 || parsed === current.fast_mode_turn_limit) return;
        void updateChatSettings({ ...current, fast_mode_turn_limit: parsed });
      };
      const isDefault = effectiveSettings.fast_mode_default === state.mode;

      return m(
        Modal,
        {
          onDismiss: onClose,
          onEscape: onClose,
          title: "Fast Mode",
          card: { class: "", "data-e2e": "fast-mode-modal", role: "dialog", "aria-label": "Fast Mode" },
          actions: [m(Button, { variant: "primary", extra: "fast-mode-done", onclick: onClose }, "Done")],
        },
        [
          m("p", { class: MODAL_MESSAGE_CLASS }, "How fast this chat's agent answers."),
          m(
            "div",
            { class: "fast-mode-options flex flex-col gap-2", role: "radiogroup", "aria-label": "Fast mode" },
            MODES.map(({ mode, label }) => {
              const isSelected = state.mode === mode;
              return m(
                "button",
                {
                  type: "button",
                  role: "radio",
                  "aria-checked": isSelected ? "true" : "false",
                  "data-fast-mode": mode,
                  class: `${OPTION_CLASS} ${isSelected ? OPTION_SELECTED_CLASS : OPTION_UNSELECTED_CLASS}`,
                  onclick: () => {
                    if (!isSelected) chooseFastMode(chatId, mode, getEventsForChat(chatId));
                  },
                },
                [
                  m("span", { class: "flex flex-col" }, [
                    m("span", { class: "font-medium text-primary" }, [
                      label,
                      isSelected && mode === "auto" && state.is_switched
                        ? m("span", { class: "ml-1.5 type-helper text-faint" }, "(off now)")
                        : null,
                    ]),
                    m("span", { class: "type-helper text-secondary" }, fastModeDetail(mode, limit)),
                  ]),
                ],
              );
            }),
          ),
          state.mode === "auto"
            ? m("label", { class: "fast-mode-limit mt-3 flex items-center gap-2 text-secondary" }, [
                "Turn off after",
                m("input", {
                  type: "number",
                  min: 1,
                  step: 1,
                  class: inputClass({ extra: "fast-limit-input w-16 py-1 text-right" }),
                  "aria-label": "Fast mode turn limit",
                  value: limitDraft ?? String(limit),
                  disabled: settings === null,
                  oninput: (event: Event) => {
                    limitDraft = (event.target as HTMLInputElement).value;
                  },
                  onchange: (event: Event) => {
                    limitDraft = null;
                    applyLimit((event.target as HTMLInputElement).value);
                  },
                  onblur: () => {
                    limitDraft = null;
                  },
                  onkeydown: (event: KeyboardEvent) => {
                    if (event.key === "Enter") (event.target as HTMLInputElement).blur();
                  },
                }),
                limit === 1 ? "turn" : "turns",
              ])
            : null,
          m("label", { class: "fast-mode-default mt-4 flex items-center gap-2 type-helper text-secondary" }, [
            m("input", {
              type: "checkbox",
              checked: isDefault,
              disabled: settings === null || isDefault,
              onchange: (event: Event) => {
                const current = getChatSettings();
                if (current === null || !(event.target as HTMLInputElement).checked) return;
                void updateChatSettings({ ...current, fast_mode_default: state.mode });
              },
            }),
            `Use ${MODES.find((option) => option.mode === state.mode)?.label ?? ""} for new chats`,
          ]),
        ],
      );
    },
  };
}
