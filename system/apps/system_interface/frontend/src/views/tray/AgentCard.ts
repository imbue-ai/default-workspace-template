/**
 * The agent's card (the multiplayer spec, section 6.4): the face, the workspace name, "Working" or "Idle", then
 * "Working on" (the chats whose status is working or background) and "Needs you" (the chats whose status is
 * attention), one row each with the chat's status dot and title, a click opening that chat; then the actions --
 * "Change avatar..." and "Float" (the card only shows while the avatar is docked, so Float is always the way out).
 */

import m from "mithril";
import type { ChatActivity } from "../../model/records";
import type { AvatarState } from "../../reducers/desktopState";
import { AvatarImage } from "../AvatarImage";
import { TrayCardFrame } from "./TrayCard";
import type { TrayCardFrameAttrs } from "./TrayCard";

const CARD_FACE_SIZE_PX = 36;

export interface AgentCardAttrs extends Omit<TrayCardFrameAttrs, "label"> {
  readonly avatar: AvatarState;
  readonly workspaceName: string;
  readonly workingOn: readonly ChatActivity[];
  readonly needsYou: readonly ChatActivity[];
  readonly onChangeAvatar: () => void;
  /** Move the one avatar creature out of the tray, floating above the windows. */
  readonly onFloat: () => void;
  readonly onOpenChat: (chatId: string) => void;
}

/** One of the card's actions: a full-width row like the chats', without a dot. */
function actionRow(label: string, dataAttr: string, onclick: () => void): m.Vnode {
  return m(
    "button",
    {
      type: "button",
      [dataAttr]: "",
      class:
        "flex w-full cursor-pointer items-center rounded-md border-0 bg-transparent px-1 py-0.5 text-left " +
        "text-primary hover:bg-fill-hover",
      onclick,
    },
    label,
  );
}

function chatRows(
  label: string,
  chats: readonly ChatActivity[],
  dotClass: string,
  onOpenChat: (chatId: string) => void,
): m.Children {
  if (chats.length === 0) return null;
  return m("div", { class: "flex flex-col gap-0.5", "data-tray-card-chats": label }, [
    m("div", { class: "text-(length:--font-size-helper) text-faint" }, label),
    chats.map((chat) =>
      m(
        "button",
        {
          key: chat.chat_id,
          type: "button",
          "data-tray-card-chat": chat.chat_id,
          class:
            "flex cursor-pointer items-center gap-1.5 rounded-md border-0 bg-transparent px-1 py-0.5 text-left text-primary hover:bg-fill-hover",
          onclick: () => onOpenChat(chat.chat_id),
        },
        [
          m("span", { class: `inline-block size-2 shrink-0 rounded-full ${dotClass}` }),
          m("span", { class: "min-w-0 flex-1 truncate" }, chat.title),
        ],
      ),
    ),
  ]);
}

export const AgentCard: m.Component<AgentCardAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    const name = attrs.workspaceName === "" ? "The agent" : attrs.workspaceName;
    const isWorking = attrs.avatar.status.mood === "working";
    return m(TrayCardFrame, { anchor: attrs.anchor, label: name, onEnter: attrs.onEnter, onLeave: attrs.onLeave }, [
      m("div", { class: "flex items-center gap-2" }, [
        m(
          "span",
          {
            class: "flex shrink-0 items-center justify-center [&>img]:size-full [&>svg]:size-full",
            style: { width: `${CARD_FACE_SIZE_PX}px`, height: `${CARD_FACE_SIZE_PX}px` },
          },
          m(AvatarImage, {
            design: attrs.avatar.design,
            defaultDesign: attrs.avatar.defaultDesign,
            mood: attrs.avatar.status.mood,
            class: "size-full",
          }),
        ),
        m("div", { class: "flex min-w-0 flex-col" }, [
          m("span", { class: "truncate font-semibold", "data-tray-card-name": "" }, name),
          m("div", { class: "flex items-center gap-1.5 text-(length:--font-size-helper) text-secondary" }, [
            m("span", { class: `inline-block size-2 shrink-0 rounded-full ${isWorking ? "bg-accent" : "bg-faint"}` }),
            m("span", { "data-tray-card-status": "" }, isWorking ? "Working" : "Idle"),
          ]),
        ]),
      ]),
      chatRows("Working on", attrs.workingOn, "bg-accent", attrs.onOpenChat),
      chatRows("Needs you", attrs.needsYou, "bg-warning", attrs.onOpenChat),
      m("div", { class: "flex flex-col gap-0.5 border-t border-subtle pt-1.5", "data-tray-card-actions": "" }, [
        actionRow("Change avatar...", "data-tray-card-change-avatar", attrs.onChangeAvatar),
        actionRow("Float", "data-tray-card-float", attrs.onFloat),
      ]),
    ]);
  },
};
