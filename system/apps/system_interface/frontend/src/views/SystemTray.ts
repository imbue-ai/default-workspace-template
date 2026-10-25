/**
 * The system tray at the taskbar's right end (the multiplayer spec, section 6.1): the share button (under a chrome
 * that takes it), the people, the viewer's own entry, and the agent. The desktop switcher sits before the window
 * entries now, where the overview button of the desktops phase will.
 */

import m from "mithril";
import type { RosterParticipant } from "../model/records";
import type { AvatarState } from "../reducers/desktopState";
import { AgentEntry } from "./tray/AgentEntry";
import { PeopleStrip } from "./tray/PeopleStrip";
import { ShareButton } from "./tray/ShareButton";

export interface SystemTrayAttrs {
  readonly roster: readonly RosterParticipant[];
  readonly viewer: string | null;
  readonly now: Date;
  readonly arrived: ReadonlySet<string>;
  readonly isShareOffered: boolean;
  readonly avatar: AvatarState;
  readonly workspaceName: string;
  readonly isAgentAttending: boolean;
  readonly isAgentHopping: boolean;
  readonly onPersonClick: (entry: RosterParticipant) => void;
  readonly onAgentClick: () => void;
  readonly onPersonHoverStart: (entry: RosterParticipant, element: HTMLElement) => void;
  readonly onAgentHoverStart: (element: HTMLElement) => void;
  readonly onHoverEnd: () => void;
}

export const SystemTray: m.Component<SystemTrayAttrs> = {
  view(vnode) {
    const attrs = vnode.attrs;
    return m("div", { "data-system-tray": "", class: "system-tray flex shrink-0 items-center gap-2 pl-2" }, [
      attrs.isShareOffered ? m(ShareButton) : null,
      m(PeopleStrip, {
        roster: attrs.roster,
        viewer: attrs.viewer,
        now: attrs.now,
        arrived: attrs.arrived,
        onPersonClick: attrs.onPersonClick,
        onHoverStart: attrs.onPersonHoverStart,
        onHoverEnd: attrs.onHoverEnd,
      }),
      m(AgentEntry, {
        avatar: attrs.avatar,
        workspaceName: attrs.workspaceName,
        isAttending: attrs.isAgentAttending,
        isHopping: attrs.isAgentHopping,
        onClick: attrs.onAgentClick,
        onHoverStart: attrs.onAgentHoverStart,
        onHoverEnd: attrs.onHoverEnd,
      }),
    ]);
  },
};
