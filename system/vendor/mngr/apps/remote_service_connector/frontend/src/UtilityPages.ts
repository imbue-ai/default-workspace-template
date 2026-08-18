// The small utility pages linked from emails and the share flow: the
// password-reset form, the verify-email result, and the share flow's
// check-your-inbox page. Same bundle, same look as the auth pages.

import m from "mithril";
import { fetchIdentity, resetPassword, verifyEmailToken } from "./api";
import {
  BTN_PRIMARY,
  CenteredCard,
  ErrorBanner,
  INPUT_CLASS,
  LINK_CLASS,
  MindsWordmark,
  Spinner,
  SuccessNote,
} from "./components";

function wordmarkHeader(): m.Vnode {
  return m(
    "div",
    { class: "flex justify-center mb-6 text-primary" },
    MindsWordmark(),
  );
}

interface ResetState {
  isBusy: boolean;
  isDone: boolean;
  error: string;
}

/** The password-reset form linked from reset emails (?token=...). */
export function ResetPasswordPage(): m.Component {
  const token = new URLSearchParams(window.location.search).get("token") ?? "";
  const state: ResetState = { isBusy: false, isDone: false, error: "" };

  async function submit(
    newPassword: string,
    confirmPassword: string,
  ): Promise<void> {
    state.error = "";
    if (newPassword !== confirmPassword) {
      state.error = "The passwords do not match.";
      return;
    }
    state.isBusy = true;
    m.redraw();
    try {
      const result = await resetPassword(token, newPassword);
      if (result.status === "OK") {
        state.isDone = true;
      } else if (result.status === "INVALID_TOKEN") {
        state.error =
          "This reset link is invalid or has expired. Request a new one from the sign-in page.";
      } else {
        state.error =
          result.message || "Could not reset the password. Please try again.";
      }
    } catch {
      state.error =
        "Could not reach the sign-in service. Check your connection and try again.";
    } finally {
      state.isBusy = false;
      m.redraw();
    }
  }

  return {
    view() {
      if (!token) {
        return CenteredCard(
          wordmarkHeader(),
          m("h1", { class: "type-heading mb-2" }, "Reset link is incomplete"),
          m(
            "p",
            { class: "type-body text-secondary" },
            "This page needs the token from your reset email. Open the link from the email again, or request a new one from the sign-in page.",
          ),
        );
      }
      if (state.isDone) {
        return CenteredCard(
          wordmarkHeader(),
          SuccessNote("Your password has been reset."),
          m(
            "div",
            { class: "text-center" },
            m(
              "a",
              { class: LINK_CLASS + " type-body", href: "/login" },
              "Go to sign in",
            ),
          ),
        );
      }
      return CenteredCard(
        wordmarkHeader(),
        m("h1", { class: "type-heading mb-2" }, "Set a new password"),
        ErrorBanner(state.error),
        m(
          "form",
          {
            onsubmit(event: SubmitEvent) {
              event.preventDefault();
              const form = event.target as HTMLFormElement;
              const newPassword = (
                form.elements.namedItem("new-password") as HTMLInputElement
              ).value;
              const confirmPassword = (
                form.elements.namedItem("confirm-password") as HTMLInputElement
              ).value;
              void submit(newPassword, confirmPassword);
            },
          },
          [
            m(
              "label",
              { class: "block type-label mb-1", for: "new-password" },
              "New password",
            ),
            m("input", {
              id: "new-password",
              name: "new-password",
              type: "password",
              autocomplete: "new-password",
              required: true,
              minlength: 8,
              class: INPUT_CLASS + " mb-4",
              placeholder: "At least 8 characters",
            }),
            m(
              "label",
              { class: "block type-label mb-1", for: "confirm-password" },
              "Confirm password",
            ),
            m("input", {
              id: "confirm-password",
              name: "confirm-password",
              type: "password",
              autocomplete: "new-password",
              required: true,
              class: INPUT_CLASS + " mb-4",
            }),
            m(
              "button",
              {
                type: "submit",
                class: BTN_PRIMARY,
                disabled: state.isBusy,
                id: "reset-submit-btn",
              },
              state.isBusy ? Spinner() : "Reset password",
            ),
          ],
        ),
      );
    },
  };
}

// Only a local share-authorization path may be continued to: the value
// arrives via a URL parameter, so anything else (a foreign host, another
// path) is discarded rather than becoming an open redirect.
function shareContinuePath(): string | null {
  const next = new URLSearchParams(window.location.search).get("next") ?? "";
  return next.startsWith("/share/authorize?") ? next : null;
}

type VerifyState = "pending" | "success" | "failure";

/** The verify-email result page linked from verification emails (?token=...). */
export function VerifyEmailPage(): m.Component {
  const params = new URLSearchParams(window.location.search);
  const token = params.get("token") ?? "";
  // SuperTokens camel-cases tenantId in the links it emits.
  const tenantId = params.get("tenantId") ?? "";
  // A share visitor's verification link carries the way back to the
  // workspace they were opening (appended by the connector at send time).
  const continuePath = shareContinuePath();
  let state: VerifyState = "pending";

  async function consume(): Promise<void> {
    if (!token) {
      state = "failure";
      m.redraw();
      return;
    }
    try {
      const result = await verifyEmailToken(token, tenantId);
      state = result.status === "OK" ? "success" : "failure";
    } catch {
      state = "failure";
    }
    m.redraw();
  }

  return {
    oninit() {
      void consume();
    },
    view() {
      if (state === "pending") {
        return m(
          "div",
          { class: "min-h-full flex items-center justify-center" },
          Spinner(),
        );
      }
      if (state === "success") {
        return CenteredCard(
          wordmarkHeader(),
          m(
            "h1",
            { class: "type-heading mb-2", id: "verify-result" },
            "Email verified",
          ),
          m(
            "p",
            { class: "type-body text-secondary" },
            continuePath !== null
              ? "You're all set. Continue to the workspace that was shared with you."
              : "You're all set. You can close this tab and return to what you were doing.",
          ),
          continuePath !== null
            ? m(
                "div",
                { class: "mt-4" },
                m(
                  "a",
                  {
                    id: "verify-continue-btn",
                    class:
                      BTN_PRIMARY + " inline-block text-center no-underline",
                    href: continuePath,
                  },
                  "Continue to the shared workspace",
                ),
              )
            : null,
        );
      }
      return CenteredCard(
        wordmarkHeader(),
        m(
          "h1",
          { class: "type-heading mb-2", id: "verify-result" },
          "Verification failed",
        ),
        m(
          "p",
          { class: "type-body text-secondary" },
          "This verification link is invalid or has expired. Request a new one from where you were asked to verify, then try again.",
        ),
      );
    },
  };
}

const CHECK_INBOX_POLL_MS = 3000;

/** The share flow's check-your-inbox page: an unverified visitor was just sent a verification link.
 *
 * When the URL carries the share-authorization continue path, the page polls
 * the session's verification state and routes the visitor onward the moment
 * the link is clicked (in this tab or any other), plus offers a manual
 * continue button as the fallback.
 */
export function CheckInboxPage(): m.Component {
  const continuePath = shareContinuePath();
  let pollTimer: number | null = null;
  // Also guards the in-flight poll: onremove can only clear a SCHEDULED
  // timer, so a poll awaiting fetchIdentity when the page is removed must
  // see this flag instead of rescheduling itself forever.
  let isDisposed = false;

  async function pollUntilVerified(): Promise<void> {
    pollTimer = null;
    if (continuePath === null || isDisposed) return;
    try {
      const identity = await fetchIdentity();
      if (identity.signed_in && identity.email_verified === true) {
        window.location.assign(continuePath);
        return;
      }
    } catch {
      // A transient failure just means we poll again.
    }
    if (isDisposed) return;
    pollTimer = window.setTimeout(
      () => void pollUntilVerified(),
      CHECK_INBOX_POLL_MS,
    );
  }

  return {
    oninit() {
      if (continuePath !== null) {
        pollTimer = window.setTimeout(
          () => void pollUntilVerified(),
          CHECK_INBOX_POLL_MS,
        );
      }
    },
    onremove() {
      isDisposed = true;
      if (pollTimer !== null) window.clearTimeout(pollTimer);
    },
    view() {
      return CenteredCard(
        wordmarkHeader(),
        m("h1", { class: "type-heading mb-2" }, "Check your inbox"),
        m(
          "p",
          { class: "type-body text-secondary" },
          "Opening a workspace that was shared with you requires a verified email. " +
            "We sent a verification link to your address -- click it" +
            (continuePath !== null
              ? ", and this page will take you to the shared workspace automatically."
              : ", then reload the shared workspace link you were given."),
        ),
        continuePath !== null
          ? m(
              "div",
              { class: "mt-4" },
              m(
                "a",
                {
                  id: "check-inbox-continue-btn",
                  class: LINK_CLASS + " type-body",
                  href: continuePath,
                },
                "I've clicked the link -- continue to the workspace",
              ),
            )
          : null,
      );
    },
  };
}
