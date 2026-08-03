/**
 * Workspace Settings overlay, opened from the header's gear button.
 *
 * This is the single persistent entry point into mind-global settings. The
 * system_interface previously had no settings surface at all -- Claude auth
 * was only reachable reactively when an agent hit an auth-error, and sharing
 * pointed at a desktop-app page that does not exist in the hosted deployment.
 *
 * Sections:
 * - Claude authentication: reads `/api/claude-auth/status` and lets the user
 *   proactively (re-)authenticate by opening the existing `ClaudeLoginModal`
 *   (models/ClaudeAuth.ts), rather than waiting for an auth failure.
 * - Connected services & sharing: informational, since third-party access is
 *   granted per-request through latchkey consent and external sharing is
 *   configured outside this UI.
 */

import m from "mithril";
import { apiUrl } from "../base-path";
import { openLoginModal } from "../models/ClaudeAuth";
import { icon } from "./icons";

interface ClaudeAuthStatus {
  logged_in: boolean;
  auth_method?: string | null;
  api_provider?: string | null;
  email?: string | null;
  org_name?: string | null;
  subscription_type?: string | null;
}

interface SettingsModalAttrs {
  onClose: () => void;
}

// Module-scoped so a redraw mid-fetch does not restart the request; the modal
// is a single app-level instance, so one slot is enough.
let authStatus: ClaudeAuthStatus | null = null;
let authError: string | null = null;
let isLoadingAuth = false;

async function fetchAuthStatus(): Promise<void> {
  isLoadingAuth = true;
  authError = null;
  m.redraw();
  try {
    const response = await fetch(apiUrl("/api/claude-auth/status"));
    if (!response.ok) {
      throw new Error(`status ${response.status}`);
    }
    authStatus = (await response.json()) as ClaudeAuthStatus;
  } catch (error) {
    authError = error instanceof Error ? error.message : String(error);
    authStatus = null;
  } finally {
    isLoadingAuth = false;
    m.redraw();
  }
}

function describeAuth(status: ClaudeAuthStatus): string {
  const detail = status.email || status.org_name || status.subscription_type || status.api_provider;
  const method = status.auth_method ? ` (${status.auth_method})` : "";
  return detail ? `Signed in as ${detail}${method}` : `Signed in${method}`;
}

function renderAuthSection(onClose: () => void): m.Vnode {
  let body: m.Children;
  if (isLoadingAuth) {
    body = m("div.settings-row-muted", "Checking sign-in status...");
  } else if (authError) {
    body = m("div.settings-row-muted", `Could not read sign-in status (${authError}).`);
  } else if (authStatus && authStatus.logged_in) {
    body = m("div.settings-status.settings-status--ok", describeAuth(authStatus));
  } else {
    body = m(
      "div.settings-status.settings-status--warn",
      "Not signed in -- agents cannot reach a model until you sign in.",
    );
  }

  return m("section.settings-section", [
    m("h4.settings-section-title", "Claude authentication"),
    body,
    m("div.settings-actions", [
      m(
        "button.settings-btn.settings-btn-primary",
        {
          onclick: () => {
            // Close settings first so the two overlays don't stack.
            onClose();
            openLoginModal();
          },
        },
        authStatus && authStatus.logged_in ? "Change credentials" : "Sign in",
      ),
      m("button.settings-btn.settings-btn-secondary", { onclick: () => void fetchAuthStatus() }, "Refresh"),
    ]),
  ]);
}

function renderServicesSection(): m.Vnode {
  return m("section.settings-section", [
    m("h4.settings-section-title", "Connected services & sharing"),
    m("p.settings-row-muted", [
      "Third-party API access is granted per request through the connected-services consent flow. ",
      "External sharing of a running service is configured outside this workspace UI.",
    ]),
  ]);
}

export const SettingsModal: m.Component<SettingsModalAttrs> = {
  oninit() {
    void fetchAuthStatus();
  },
  view(vnode) {
    const { onClose } = vnode.attrs;
    return m(
      "div.settings-overlay",
      {
        onclick: (e: Event) => {
          if (e.target === e.currentTarget) onClose();
        },
      },
      [
        m("div.settings-modal", [
          m("div.settings-modal-header", [
            m("h3.settings-modal-title", "Workspace settings"),
            m(
              "button.settings-modal-close-x",
              { onclick: onClose, title: "Close", "aria-label": "Close settings" },
              m.trust(icon("close", { size: 18 })),
            ),
          ]),
          m("div.settings-modal-body", [renderAuthSection(onClose), renderServicesSection()]),
        ]),
      ],
    );
  },
};
