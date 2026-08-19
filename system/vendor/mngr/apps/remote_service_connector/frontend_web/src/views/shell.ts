// The chrome's app shell: sign-in + unlock gating, top navigation, and the
// shared page frame every view renders inside.

import m from "mithril";
import {
  BUILD_DEPLOY_ID,
  type Identity,
  fetchIdentity,
  fetchServerVersion,
  loginUrl,
} from "../api";
import { currentDek, loadRememberedDek } from "../dekstore";
import { UnlockView } from "./unlock";

// Stale-open-tab detection: when the connector's live deploy_id no longer
// matches the id baked into this bundle, a newer chrome exists and this tab
// should reload before making further changes. Checked opportunistically on
// gate refreshes plus a slow interval; "dev" bundles (no baked id) and
// servers without a deploy id skip the nudge.
export const staleBundle = { isStale: false };

const STALE_BUNDLE_CHECK_INTERVAL_MS = 5 * 60 * 1000;
let staleBundleTimer: number | null = null;

export async function checkBundleFreshness(): Promise<void> {
  if (BUILD_DEPLOY_ID === "dev" || staleBundle.isStale) return;
  const server = await fetchServerVersion();
  const serverDeployId = server?.deploy_id ?? "";
  if (serverDeployId !== "" && serverDeployId !== BUILD_DEPLOY_ID) {
    staleBundle.isStale = true;
    // Staleness is terminal until the tab reloads, so stop polling.
    if (staleBundleTimer !== null) {
      window.clearInterval(staleBundleTimer);
      staleBundleTimer = null;
    }
    m.redraw();
  }
}

function ensureBundleFreshnessTimer(): void {
  // "dev" bundles have no baked deploy id to compare, so polling would be a
  // permanent no-op (checkBundleFreshness early-returns) -- skip the timer.
  if (BUILD_DEPLOY_ID === "dev" || staleBundle.isStale || staleBundleTimer !== null) return;
  staleBundleTimer = window.setInterval(() => {
    void checkBundleFreshness();
  }, STALE_BUNDLE_CHECK_INTERVAL_MS);
}

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
  void checkBundleFreshness();
  ensureBundleFreshnessTimer();
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
        staleBundle.isStale
          ? m(
              "div",
              {
                class:
                  "flex items-center justify-center gap-3 bg-amber-100 dark:bg-amber-900 " +
                  "text-amber-900 dark:text-amber-100 text-sm px-4 py-2",
              },
              "A new version of this page is available.",
              m(
                "button",
                {
                  class: "underline font-medium",
                  onclick: () => window.location.reload(),
                },
                "Reload",
              ),
            )
          : null,
        m(Nav),
        session.gate === "locked"
          ? m(UnlockView)
          : m("div", { class: "grow flex flex-col" }, m(page, vnode.attrs)),
      );
    },
  });
}
