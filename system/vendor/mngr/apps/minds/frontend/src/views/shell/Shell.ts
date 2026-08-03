// The persistent app shell: titlebar + switcher popover + the routed page
// body. Two body modes, matching ChromeShell.jinja:
//
// - Local page: content scrolls inside #local-page-scroll, the inset white
//   card below the fixed titlebar (accent bleeds around it).
// - Agent content surface (/workspace/<id>): the page IS the fixed iframe
//   surface; no scroll container.

import m from "mithril";
import type { ShellState } from "./shell-state";
import { SidebarMenu } from "./SidebarMenu";
import { Titlebar } from "./Titlebar";
import { WorkspaceFrame } from "./WorkspaceFrame";

export interface ShellAttrs {
  shell: ShellState;
  routePath: string;
  workspaceParam: string | null;
  content: m.Children;
}

export function Shell(): m.Component<ShellAttrs> {
  return {
    view(vnode) {
      const { shell, routePath, workspaceParam, content } = vnode.attrs;
      const isAgentSurface = workspaceParam !== null;
      // The visual-diff harness captures with ?visual-diff=1 and no live
      // channel; suppress the indicator so screenshots stay deterministic.
      const isCaptureMode = new URLSearchParams(window.location.search).has("visual-diff");
      const isReconnecting = (shell.channel?.isVisiblyReconnecting ?? false) && !isCaptureMode;

      return m("div", { style: "display: contents" }, [
        m(Titlebar, { shell, routePath }),
        m(SidebarMenu, { shell }),
        isReconnecting
          ? m(
              "div",
              {
                class:
                  "fixed top-[42px] right-2 z-[150] type-helper text-secondary bg-surface-primary border border-subtle rounded-md px-2 py-1 shadow-raised",
              },
              "Reconnecting…",
            )
          : null,
        m(
          "div#local-page-root",
          { style: "display: contents" },
          isAgentSurface
            ? workspaceParam !== null
              ? m(WorkspaceFrame, { shell, workspaceAnyId: workspaceParam })
              : null
            : m(
                "div#local-page-scroll",
                { class: "bg-surface-primary overflow-y-auto overflow-x-hidden [scrollbar-gutter:stable]" },
                content,
              ),
        ),
      ]);
    },
  };
}
