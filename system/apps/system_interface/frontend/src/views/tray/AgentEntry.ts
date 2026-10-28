/**
 * The agent's tray entry (the multiplayer spec, section 6.4): the workspace avatar at 36px, in its mood, attending
 * while the pinned chat window is focused. The one avatar creature's docked form: while a client floats the avatar
 * it is drawn above the windows instead, and the tray draws none. A click opens the main chat; hovering opens its
 * card.
 */

import m from "mithril";
import type { AvatarState } from "../../reducers/desktopState";
import { AvatarImage } from "../AvatarImage";

export const AGENT_PICTURE_SIZE_PX = 36;

export interface AgentEntryAttrs {
  readonly avatar: AvatarState;
  readonly workspaceName: string;
  readonly isAttending: boolean;
  /** Whether a bubble of the agent's just appeared, which the entry hops for. */
  readonly isHopping: boolean;
  readonly onClick: () => void;
  readonly onHoverStart: (element: HTMLElement) => void;
  readonly onHoverEnd: () => void;
}

export const AgentEntry: m.Component<AgentEntryAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m(
      "button",
      {
        type: "button",
        "data-tray-agent": "",
        "data-mood": attrs.avatar.status.mood,
        class:
          "tray-agent flex shrink-0 cursor-pointer items-center justify-center rounded-2xl border-0 bg-transparent p-0 " +
          "outline-none focus-visible:ring-2 focus-visible:ring-accent [&>img]:size-full [&>svg]:size-full " +
          (attrs.isHopping ? "tray-hop " : ""),
        style: { width: `${AGENT_PICTURE_SIZE_PX}px`, height: `${AGENT_PICTURE_SIZE_PX}px` },
        "aria-label": attrs.workspaceName === "" ? "The agent" : attrs.workspaceName,
        onclick: attrs.onClick,
        onmouseenter: (event: MouseEvent) => attrs.onHoverStart(event.currentTarget as HTMLElement),
        onmouseleave: attrs.onHoverEnd,
      },
      m(AvatarImage, {
        design: attrs.avatar.design,
        defaultDesign: attrs.avatar.defaultDesign,
        mood: attrs.avatar.status.mood,
        isAttending: attrs.isAttending,
        class: "size-full",
      }),
    );
  },
};
