// The signed-in account page: identity, verification state, password change,
// and session controls. Signed-out visitors are bounced to /login.

import m from "mithril";
import {
  changePassword,
  fetchIdentity,
  sendVerificationEmail,
  signOut,
  signOutAllDevices,
  type Identity,
} from "./api";
import {
  BTN_PRIMARY,
  BTN_SECONDARY,
  CenteredCard,
  ErrorBanner,
  INPUT_CLASS,
  MindsWordmark,
  Spinner,
  SuccessNote,
} from "./components";

interface PageState {
  identity: Identity | null;
  error: string;
  notice: string;
  passwordStatus: string;
  isBusy: boolean;
}

function VerificationRow(state: PageState): m.Vnode {
  const identity = state.identity;
  if (identity?.email_verified) {
    return m("span", { class: "type-helper text-success" }, "Verified");
  }
  return m(
    "button",
    {
      type: "button",
      class: "type-helper text-accent hover:underline cursor-pointer",
      id: "verify-email-btn",
      onclick: () => {
        state.error = "";
        state.notice = "";
        void sendVerificationEmail()
          .then((result) => {
            if (result.already_verified) {
              // Verified in the meantime (e.g. the emailed link was clicked
              // in another tab): flip the row instead of claiming an email
              // went out -- none was sent.
              if (state.identity) {
                state.identity = { ...state.identity, email_verified: true };
              }
              state.notice = "Your email is already verified.";
            } else if (result.sent) {
              state.notice = `We sent a verification link to ${identity?.email ?? "your email"}.`;
            } else {
              state.notice =
                "A verification email went out moments ago -- check your inbox (and spam).";
            }
          })
          .catch(() => {
            state.error =
              "Could not send the verification email. Please try again.";
          })
          .finally(() => m.redraw());
      },
    },
    "Not verified -- verify now",
  );
}

function ChangePasswordSection(state: PageState): m.Vnode {
  return m("div", { class: "border-t border-subtle pt-6 mt-6" }, [
    m("h2", { class: "type-label mb-3" }, "Change password"),
    state.passwordStatus ? SuccessNote(state.passwordStatus) : null,
    m(
      "form",
      {
        onsubmit(event: SubmitEvent) {
          event.preventDefault();
          const form = event.target as HTMLFormElement;
          const current = (
            form.elements.namedItem("current") as HTMLInputElement
          ).value;
          const fresh = (form.elements.namedItem("fresh") as HTMLInputElement)
            .value;
          state.error = "";
          state.passwordStatus = "";
          state.isBusy = true;
          void changePassword(current, fresh)
            .then((result) => {
              if (result.status === "OK") {
                state.passwordStatus = "Password updated.";
                form.reset();
              } else {
                state.error = result.message || "Password change failed.";
              }
            })
            .catch(() => {
              state.error = "Could not reach the service. Please try again.";
            })
            .finally(() => {
              state.isBusy = false;
              m.redraw();
            });
        },
      },
      [
        m(
          "label",
          { class: "block type-label mb-1", for: "current" },
          "Current password",
        ),
        m("input", {
          id: "current",
          name: "current",
          type: "password",
          autocomplete: "current-password",
          required: true,
          class: INPUT_CLASS + " mb-4",
        }),
        m(
          "label",
          { class: "block type-label mb-1", for: "fresh" },
          "New password",
        ),
        m("input", {
          id: "fresh",
          name: "fresh",
          type: "password",
          autocomplete: "new-password",
          required: true,
          minlength: 8,
          class: INPUT_CLASS + " mb-4",
          placeholder: "At least 8 characters",
        }),
        m(
          "button",
          { type: "submit", class: BTN_SECONDARY, disabled: state.isBusy },
          state.isBusy ? Spinner() : "Update password",
        ),
      ],
    ),
  ]);
}

function SessionsSection(state: PageState): m.Vnode {
  return m(
    "div",
    { class: "border-t border-subtle pt-6 mt-6 flex flex-col gap-3" },
    [
      m(
        "button",
        {
          type: "button",
          class: BTN_PRIMARY,
          id: "signout-btn",
          onclick: () => {
            void signOut()
              .then(() => window.location.assign("/login"))
              .catch(() => {
                state.error = "Could not sign out. Please try again.";
                m.redraw();
              });
          },
        },
        "Sign out",
      ),
      m(
        "button",
        {
          type: "button",
          class: BTN_SECONDARY,
          id: "signout-all-btn",
          onclick: () => {
            void signOutAllDevices()
              .then(() => window.location.assign("/login"))
              .catch(() => {
                state.error =
                  "Could not sign out of all devices. Please try again.";
                m.redraw();
              });
          },
        },
        "Sign out of all devices",
      ),
      m(
        "p",
        { class: "type-helper text-tertiary text-center" },
        '"All devices" includes the Minds desktop app anywhere you are signed in.',
      ),
    ],
  );
}

export function ManagePage(): m.Component {
  const state: PageState = {
    identity: null,
    error: "",
    notice: "",
    passwordStatus: "",
    isBusy: false,
  };

  async function initialize(): Promise<void> {
    try {
      state.identity = await fetchIdentity();
    } catch {
      state.identity = { signed_in: false };
    }
    if (!state.identity.signed_in) {
      // Carry the destination: without an explicit next the login page lands
      // on /web, not back here.
      window.location.assign("/login?next=%2Fmanage");
      return;
    }
    m.redraw();
  }

  return {
    oninit() {
      void initialize();
    },
    view() {
      const identity = state.identity;
      if (!identity || !identity.signed_in) {
        return m(
          "div",
          { class: "min-h-full flex items-center justify-center" },
          Spinner(),
        );
      }
      return CenteredCard(
        m(
          "div",
          { class: "flex justify-center mb-6 text-primary" },
          MindsWordmark(),
        ),
        m("h1", { class: "type-heading mb-6 text-center" }, "Your account"),
        ErrorBanner(state.error),
        SuccessNote(state.notice),
        m("div", { class: "flex items-center justify-between gap-3" }, [
          m("div", [
            m("div", { class: "type-body text-primary" }, identity.email ?? ""),
            m("div", { class: "type-helper text-tertiary" }, "Email address"),
          ]),
          VerificationRow(state),
        ]),
        ChangePasswordSection(state),
        SessionsSection(state),
        m("div", { class: "border-t border-subtle pt-6 mt-6 text-center" }, [
          m(
            "a",
            {
              href: "https://imbue.com",
              class: "type-helper text-accent hover:underline",
            },
            "Get the Minds app",
          ),
        ]),
      );
    },
  };
}
