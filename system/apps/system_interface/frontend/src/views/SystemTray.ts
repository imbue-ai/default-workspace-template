/**
 * The system tray at the taskbar's right end (the multiplayer spec, section 6.1): the share button (under a chrome
 * that takes it), the people, the viewer's own entry, and the agent. The overview button sits before the window
 * entries (section 8.2).
 */

import m from "mithril";
import { WORKSPACE_AGENT_KEY } from "../model/Bubbles";
import type { RosterParticipant } from "../model/records";
import type { AvatarState } from "../reducers/desktopState";
import { AgentEntry } from "./tray/AgentEntry";
import { PeopleStrip } from "./tray/PeopleStrip";
import { ShareButton } from "./tray/ShareButton";

export interface SystemTrayAttrs {
  readonly roster: readonly RosterParticipant[];
  readonly viewer: string | null;
  readonly now: Date;
  /** The keys of the entries hopping now: they just arrived, or a bubble of theirs just appeared. */
  readonly hopping: ReadonlySet<string>;
  /** The keys of the entries whose desktop somebody else is viewing, ringed in their colour (section 8.3). */
  readonly ringed: ReadonlySet<string>;
  readonly isShareOffered: boolean;
  readonly avatar: AvatarState;
  readonly workspaceName: string;
  readonly isAgentAttending: boolean;
  /** Whether the avatar is floating above the windows: the one creature is there, so the tray draws none. */
  readonly isAvatarFloating: boolean;
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
        hopping: attrs.hopping,
        ringed: attrs.ringed,
        onPersonClick: attrs.onPersonClick,
        onHoverStart: attrs.onPersonHoverStart,
        onHoverEnd: attrs.onHoverEnd,
      }),
      attrs.isAvatarFloating
        ? null
        : m(AgentEntry, {
            avatar: attrs.avatar,
            workspaceName: attrs.workspaceName,
            isAttending: attrs.isAgentAttending,
            isHopping: attrs.hopping.has(WORKSPACE_AGENT_KEY),
            onClick: attrs.onAgentClick,
            onHoverStart: attrs.onAgentHoverStart,
            onHoverEnd: attrs.onHoverEnd,
          }),
    ]);
  },
};
