// The chrome's app shell: sign-in + unlock gating, top navigation, and the
// shared page frame every view renders inside.

import m from "mithril";
import { type Identity, fetchIdentity, loginUrl } from "../api";
import { currentDek, loadRememberedDek } from "../dekstore";
import { UnlockView } from "./unlock";

export type GateState = "loading" | "signed_out" | "locked" | "ready";

export const session = {
  identity: null as Identity | null,
  gate: "loading" as GateState,
};

export async function refreshGate(): Promise<void> {
  try {
    session.identity = await fetchIdentity();
  } catch {
    session.identity = null;
  }
  if (!session.identity?.signed_in) {
    session.gate = "signed_out";
  } else if (currentDek() === null && (await loadRememberedDek()) === null) {
    session.gate = "locked";
  } else {
    session.gate = "ready";
  }
  m.redraw();
}

export function markUnlocked(): void {
  session.gate = "ready";
}

const NAV_LINKS: Array<{ href: string; label: string }> = [
  { href: "/", label: "Workspaces" },
  { href: "/create", label: "New workspace" },
  { href: "/settings", label: "Settings" },
];

function Nav(): m.Component {
  return {
    view() {
      return m(
        "nav",
        {
          class:
            "flex items-center gap-4 border-b border-slate-200 dark:border-slate-800 px-6 py-3",
        },
        m("span", { class: "font-semibold text-lg" }, "minds"),
        NAV_LINKS.map((link) =>
          m(
            m.route.Link,
            {
              href: link.href,
              class:
                "text-sm text-slate-600 dark:text-slate-300 hover:text-slate-900 dark:hover:text-white",
            },
            link.label,
          ),
        ),
        m("div", { class: "grow" }),
        session.identity?.email
          ? m(
              "span",
              { class: "text-xs text-slate-500" },
              session.identity.email,
            )
          : null,
      );
    },
  };
}

// Wrap a page component with the sign-in + unlock gates and the nav frame.
// Route params (vnode.attrs) are forwarded to the page component.
export function gated(
  page: () => m.Component<Record<string, string>>,
): () => m.Component<Record<string, string>> {
  return () => ({
    oninit() {
      if (session.gate === "loading") {
        void refreshGate();
      }
    },
    view(vnode) {
      if (session.gate === "loading") {
        return m("div", { class: "p-8 text-slate-500" }, "Loading...");
      }
      if (session.gate === "signed_out") {
        window.location.href = loginUrl();
        return m(
          "div",
          { class: "p-8 text-slate-500" },
          "Redirecting to sign in...",
        );
      }
      // Pass the closure components by reference (never invoke them here):
      // mithril diffs component vnodes by identity, so a freshly created
      // component object per redraw would tear down and recreate the page
      // (re-running oninit and wiping closure state) on every redraw.
      return m(
        "div",
        { class: "min-h-screen flex flex-col" },
        m(Nav),
        session.gate === "locked"
          ? m(UnlockView)
          : m("div", { class: "grow flex flex-col" }, m(page, vnode.attrs)),
      );
    },
  });
}
