// The sign-in / sign-up page, plus the "Continue as ..." interstitial shown
// whenever an authorize handoff (app login or share visit) arrives with a
// live browser session. No handoff ever proceeds silently.

import m from "mithril";
import {
  fetchConfig,
  fetchIdentity,
  requestPasswordReset,
  signIn,
  signOut,
  signUp,
  type AccountsConfig,
  type Identity,
  type SignupAttribution,
} from "./api";
import {
  BTN_PRIMARY,
  BTN_SECONDARY,
  CenteredCard,
  ErrorBanner,
  GoogleLogo,
  INPUT_CLASS,
  LINK_CLASS,
  MindsWordmark,
  Spinner,
  SuccessNote,
} from "./components";
import { describeNext, isAuthorizeNext, markNextConfirmed, sanitizeNextPath } from "./flow";
import { loadTurnstile } from "./turnstile";

type Tab = "signin" | "signup";
type View = "loading" | "form" | "interstitial" | "forgot";

// The server's login redirect carries short error codes (never free text) so
// the page controls its own copy -- reflecting arbitrary query text would let
// a crafted link display attacker-chosen prose on the official login page.
const LOGIN_ERROR_COPY: Record<string, string> = {
  invalid_state: "This sign-in link is invalid or has expired. Please try again.",
  provider_cancelled: "Google sign-in was cancelled. Please try again.",
  nonce_mismatch: "This sign-in attempt could not be verified. Please try again.",
  password_account: "An account with this email already signs in with a password. Use the email and password form.",
  oauth_failed: "Google sign-in failed. Please try again.",
  signup_blocked: "Sign-ups from this network are not accepted. Please try a different network connection.",
  terms_required:
    "Creating an account requires agreeing to the Terms of Service and Code of Conduct. " +
    "Please check the box below and try again.",
  account_suspended: "This account is suspended. If you believe this is a mistake, contact support@imbue.com.",
};

// The plans the signup form offers. Explorer is the recommended default; its
// description is the plain-language consent for the product-data sharing that
// comes with it, so it must always render alongside the selector.
const SIGNUP_PLANS: { value: string; label: string; description: string }[] = [
  {
    value: "explorer",
    label: "Explorer (2 free cloud workspaces)",
    description:
      "You agree to share product data from those workspaces with Imbue to help improve Minds.",
  },
  {
    value: "free",
    label: "Free (1 free cloud workspace)",
    description:
      "Your workspace may be temporarily paused when idle or when capacity is low. " +
      "Our goal is to make your data private and secure.",
  },
];
const DEFAULT_SIGNUP_PLAN = "explorer";
const TERMS_REQUIRED_COPY =
  "To create an account, please check the box agreeing to the Terms of Service and Code of Conduct.";

function loginErrorCopy(code: string | null): string {
  if (!code) return "";
  return LOGIN_ERROR_COPY[code] ?? "Sign-in failed. Please try again.";
}

interface PageState {
  view: View;
  tab: Tab;
  config: AccountsConfig | null;
  identity: Identity | null;
  next: string;
  error: string;
  notice: string;
  isBusy: boolean;
  // Whether the create-account tab shows the email/password fields. Google is
  // the visually dominant signup path, so with Google configured the fields
  // start collapsed behind a "Use email and password instead" link. Sticky
  // once revealed (a tab flip doesn't re-hide a form the user asked for).
  isEmailSignupFormRevealed: boolean;
  turnstileToken: string;
  turnstileWidgetId: string | null;
  // The signup form's plan selector and terms checkbox. They gate BOTH
  // creation paths (the Google button and the email/password form), so they
  // live in page state rather than the credentials form.
  selectedPlan: string;
  isTermsAccepted: boolean;
}

function navigateTo(path: string): void {
  window.location.assign(path);
}

// The page's own campaign context, forwarded with signups so the server can
// attribute the new account even when the marketing cookie is absent (the
// server extracts the allowlisted params; this just relays the raw values).
function signupAttribution(state: PageState): SignupAttribution {
  return {
    page_query: window.location.search.replace(/^\?/, ""),
    page_path: window.location.pathname,
    next: state.next,
  };
}

function finishSignin(state: PageState): void {
  // A fresh explicit sign-in confirms the account choice, so a pending
  // authorize handoff proceeds directly.
  navigateTo(markNextConfirmed(state.next));
}

function googleStartHref(state: PageState): string {
  // pq/pp carry the page's campaign context through the OAuth round-trip
  // (the server folds them into attribution only when a NEW account is
  // created by the exchange). On the signup tab, plan/terms carry the plan
  // selector's choice and the checked agreement box the same way -- the
  // server consumes them only when the exchange creates a new account.
  const attribution = signupAttribution(state);
  const base =
    `/accounts/oauth/google/start?next=${encodeURIComponent(state.next)}` +
    `&pq=${encodeURIComponent(attribution.page_query)}&pp=${encodeURIComponent(attribution.page_path)}`;
  if (state.tab !== "signup") return base;
  return `${base}&plan=${encodeURIComponent(state.selectedPlan)}${state.isTermsAccepted ? "&terms=1" : ""}`;
}

function resetTurnstile(state: PageState): void {
  // Turnstile response tokens are single-use and short-lived, and a failed
  // submit may or may not have consumed the token (the server checks the IP
  // gate before verifying Turnstile), so unconditionally issue a fresh
  // challenge for the retry.
  state.turnstileToken = "";
  if (state.turnstileWidgetId !== null) {
    window.turnstile?.reset(state.turnstileWidgetId);
  }
}

async function submitCredentials(state: PageState, email: string, password: string): Promise<void> {
  state.error = "";
  if (!email || !password) {
    state.error = "Email and password are required.";
    return;
  }
  if (state.tab === "signup" && !state.isTermsAccepted) {
    state.error = TERMS_REQUIRED_COPY;
    return;
  }
  state.isBusy = true;
  m.redraw();
  try {
    const result =
      state.tab === "signin"
        ? await signIn(email, password)
        : await signUp(email, password, state.turnstileToken, signupAttribution(state), state.selectedPlan);
    if (result.status === "OK") {
      finishSignin(state);
      return;
    }
    state.error = result.message || "Something went wrong. Please try again.";
    // The server stepped this visitor up to OAuth-only: re-collapse the
    // email/password fields so Continue with Google is the visible path.
    if (result.status === "OAUTH_ONLY") state.isEmailSignupFormRevealed = false;
    if (state.tab === "signup") resetTurnstile(state);
  } catch {
    state.error = "Could not reach the sign-in service. Check your connection and try again.";
    if (state.tab === "signup") resetTurnstile(state);
  } finally {
    state.isBusy = false;
    m.redraw();
  }
}

function TabBar(state: PageState): m.Vnode {
  const tabClass = (tab: Tab) =>
    "flex-1 text-center pb-2 type-label cursor-pointer border-b-2 " +
    (state.tab === tab ? "border-stronger text-primary" : "border-transparent text-tertiary hover:text-secondary");
  // Create-account leads: most signed-out visitors are new users, so it is
  // both the first tab and the default-selected one (see AuthPage's state).
  return m("div", { class: "flex mb-6" }, [
    m(
      "button",
      { type: "button", class: tabClass("signup"), onclick: () => ((state.tab = "signup"), (state.error = "")) },
      "Create account",
    ),
    m(
      "button",
      { type: "button", class: tabClass("signin"), onclick: () => ((state.tab = "signin"), (state.error = "")) },
      "Sign in",
    ),
  ]);
}

function TurnstileWidget(state: PageState): m.Vnode | null {
  const siteKey = state.config?.turnstile_site_key || "";
  if (!siteKey || state.tab !== "signup") return null;
  return m("div", {
    class: "mb-4 min-h-[65px]",
    oncreate(vnode) {
      void loadTurnstile()
        .then((api) => {
          state.turnstileWidgetId = api.render(vnode.dom as HTMLElement, {
            sitekey: siteKey,
            theme: document.documentElement.classList.contains("dark") ? "dark" : "light",
            callback: (token: string) => {
              state.turnstileToken = token;
            },
            "error-callback": () => {
              state.turnstileToken = "";
            },
          });
        })
        .catch(() => {
          // Signup cannot succeed without the challenge (the server verifies
          // the token), so a failed script load must be surfaced, not silent.
          state.error = "Could not load the human-verification challenge. Check your connection and try again.";
          m.redraw();
        });
    },
  });
}

function CredentialsForm(state: PageState): m.Vnode {
  let email = "";
  let password = "";
  return m(
    "form",
    {
      onsubmit(event: SubmitEvent) {
        event.preventDefault();
        const form = event.target as HTMLFormElement;
        email = (form.elements.namedItem("email") as HTMLInputElement).value.trim();
        password = (form.elements.namedItem("password") as HTMLInputElement).value;
        if (state.tab === "signup") {
          const confirm = (form.elements.namedItem("confirm-password") as HTMLInputElement).value;
          if (password !== confirm) {
            state.error = "The passwords do not match.";
            return;
          }
        }
        void submitCredentials(state, email, password);
      },
    },
    [
      m("label", { class: "block type-label mb-1", for: "email" }, "Email"),
      m("input", {
        id: "email",
        name: "email",
        type: "email",
        autocomplete: "email",
        required: true,
        class: INPUT_CLASS + " mb-4",
        placeholder: "you@example.com",
      }),
      m("label", { class: "block type-label mb-1", for: "password" }, "Password"),
      m("input", {
        id: "password",
        name: "password",
        type: "password",
        autocomplete: state.tab === "signup" ? "new-password" : "current-password",
        required: true,
        minlength: state.tab === "signup" ? 8 : undefined,
        class: INPUT_CLASS + " mb-2",
        placeholder: state.tab === "signup" ? "At least 8 characters" : "Your password",
      }),
      state.tab === "signup"
        ? [
            m(
              "label",
              { class: "block type-label mb-1 mt-2", for: "confirm-password" },
              "Confirm password",
            ),
            m("input", {
              id: "confirm-password",
              name: "confirm-password",
              type: "password",
              autocomplete: "new-password",
              required: true,
              class: INPUT_CLASS + " mb-2",
              placeholder: "Type it again",
            }),
          ]
        : null,
      state.tab === "signin"
        ? m("div", { class: "mb-4 text-right" }, [
            // A button (not an href-less anchor) so it is keyboard-focusable;
            // type="button" keeps it from submitting the surrounding form.
            m(
              "button",
              {
                type: "button",
                class: LINK_CLASS + " type-helper",
                onclick: () => ((state.view = "forgot"), (state.error = "")),
              },
              "Forgot password?",
            ),
          ])
        : m("div", { class: "mb-2" }),
      TurnstileWidget(state),
      m(
        "button",
        { type: "submit", class: BTN_PRIMARY, disabled: state.isBusy, id: "auth-submit-btn" },
        state.isBusy ? Spinner() : state.tab === "signin" ? "Sign in" : "Create account",
      ),
    ],
  );
}

function DocLink(href: string, label: string): m.Vnode {
  return m("a", { href, target: "_blank", rel: "noopener", class: LINK_CLASS }, label);
}

function PlanSelector(state: PageState): m.Vnode {
  const selected = SIGNUP_PLANS.find((plan) => plan.value === state.selectedPlan) ?? SIGNUP_PLANS[0];
  return m("div", { class: "mb-4" }, [
    m("label", { class: "block type-label mb-1", for: "plan-select" }, "Plan"),
    m(
      "select",
      {
        id: "plan-select",
        name: "plan",
        class: INPUT_CLASS + " mb-2",
        onchange: (event: Event) => {
          state.selectedPlan = (event.target as HTMLSelectElement).value;
        },
      },
      SIGNUP_PLANS.map((plan) =>
        m("option", { value: plan.value, selected: plan.value === state.selectedPlan }, plan.label),
      ),
    ),
    m("p", { class: "type-helper text-tertiary" }, [
      selected.description,
      " ",
      DocLink("/privacy-policy", "Learn more."),
    ]),
  ]);
}

function TermsCheckbox(state: PageState): m.Vnode {
  // Starts unchecked on purpose: agreement must be an affirmative action.
  return m("label", { class: "flex items-start gap-2 mb-4 type-helper text-secondary cursor-pointer" }, [
    m("input", {
      id: "terms-checkbox",
      type: "checkbox",
      checked: state.isTermsAccepted,
      class: "mt-0.5 accent-current cursor-pointer",
      onchange: (event: Event) => {
        state.isTermsAccepted = (event.target as HTMLInputElement).checked;
        if (state.isTermsAccepted) state.error = "";
      },
    }),
    m("span", [
      "I have read and agree to the ",
      DocLink("/terms-of-service", "Terms of Service"),
      " and the ",
      DocLink("/code-of-conduct", "Code of Conduct"),
      ".",
    ]),
  ]);
}

function GoogleButton(state: PageState): m.Vnode {
  // Google leads on both tabs: it is the visually dominant way to create an
  // account and the topmost sign-in option. On the signup tab it is gated on
  // the terms checkbox like the email/password form (account creation must
  // never proceed without the agreement).
  return m(
    "a",
    {
      href: googleStartHref(state),
      class: BTN_SECONDARY,
      id: "google-signin-btn",
      onclick: (event: MouseEvent) => {
        if (state.tab === "signup" && !state.isTermsAccepted) {
          event.preventDefault();
          state.error = TERMS_REQUIRED_COPY;
        }
      },
    },
    [GoogleLogo(), "Continue with Google"],
  );
}

function OrDivider(): m.Vnode {
  return m("div", { class: "flex items-center gap-2 my-4 text-tertiary type-helper" }, [
    m("div", { class: "flex-1 border-t border-subtle" }),
    "or",
    m("div", { class: "flex-1 border-t border-subtle" }),
  ]);
}

function RevealEmailFormLink(state: PageState): m.Vnode {
  return m("div", { class: "text-center mt-4" }, [
    m(
      "button",
      {
        type: "button",
        id: "reveal-email-form-btn",
        class: LINK_CLASS + " type-body",
        onclick: () => {
          state.isEmailSignupFormRevealed = true;
          state.error = "";
        },
      },
      "Use email and password instead",
    ),
  ]);
}

function FormView(state: PageState): m.Vnode {
  const purpose = isAuthorizeNext(state.next)
    ? m(
        "p",
        { class: "type-body text-secondary mb-6" },
        `Sign in or create a Minds account to ${describeNext(state.next)}.`,
      )
    : null;
  // Sign-in always shows the credentials form (Google on top). Sign-up leads
  // with Google alone and keeps email/password collapsed behind the reveal
  // link; without Google configured (some dev tiers) the form is the only
  // option, so it renders expanded. The plan selector and terms checkbox sit
  // above both creation paths because they gate both.
  const isGoogleShown = !!state.config?.google_enabled;
  const isEmailFormShown = !isGoogleShown || state.tab === "signin" || state.isEmailSignupFormRevealed;
  return CenteredCard(
    m("div", { class: "flex justify-center mb-6 text-primary" }, MindsWordmark()),
    purpose,
    ErrorBanner(state.error),
    SuccessNote(state.notice),
    TabBar(state),
    state.tab === "signup" ? PlanSelector(state) : null,
    state.tab === "signup" ? TermsCheckbox(state) : null,
    isGoogleShown ? GoogleButton(state) : null,
    isGoogleShown && isEmailFormShown ? OrDivider() : null,
    isEmailFormShown ? CredentialsForm(state) : RevealEmailFormLink(state),
  );
}

function InterstitialView(state: PageState): m.Vnode {
  const email = state.identity?.email ?? "";
  return CenteredCard(
    m("div", { class: "flex justify-center mb-6 text-primary" }, MindsWordmark()),
    ErrorBanner(state.error),
    m("p", { class: "type-body text-secondary mb-6 text-center" }, `Choose an account to ${describeNext(state.next)}.`),
    m(
      "button",
      {
        type: "button",
        class: BTN_PRIMARY + " mb-3",
        id: "continue-as-btn",
        onclick: () => navigateTo(markNextConfirmed(state.next)),
      },
      `Continue as ${email}`,
    ),
    m(
      "button",
      {
        type: "button",
        class: BTN_SECONDARY,
        id: "use-different-account-btn",
        onclick: () => {
          void signOut()
            .then(() => {
              state.identity = { signed_in: false };
              state.view = "form";
            })
            .catch(() => {
              state.error = "Could not sign out. Please try again.";
            })
            .finally(() => m.redraw());
        },
      },
      "Use a different account",
    ),
  );
}

function ForgotPasswordView(state: PageState): m.Vnode {
  return CenteredCard(
    m("div", { class: "flex justify-center mb-6 text-primary" }, MindsWordmark()),
    m("h1", { class: "type-heading mb-2" }, "Reset your password"),
    m(
      "p",
      { class: "type-body text-secondary mb-6" },
      "Enter your account's email and we'll send you a link to set a new password.",
    ),
    ErrorBanner(state.error),
    SuccessNote(state.notice),
    m(
      "form",
      {
        onsubmit(event: SubmitEvent) {
          event.preventDefault();
          const form = event.target as HTMLFormElement;
          const email = (form.elements.namedItem("email") as HTMLInputElement).value.trim();
          if (!email) return;
          state.isBusy = true;
          state.error = "";
          state.notice = "";
          void requestPasswordReset(email)
            .then(() => {
              // Fixed anti-enumeration copy: the server answers OK whether or
              // not the account exists.
              state.notice = "If an account exists for that address, a reset email is on its way.";
            })
            .catch(() => {
              state.error = "Could not reach the sign-in service. Check your connection and try again.";
            })
            .finally(() => {
              state.isBusy = false;
              m.redraw();
            });
        },
      },
      [
        m("label", { class: "block type-label mb-1", for: "email" }, "Email"),
        m("input", {
          id: "email",
          name: "email",
          type: "email",
          autocomplete: "email",
          required: true,
          class: INPUT_CLASS + " mb-4",
          placeholder: "you@example.com",
        }),
        m(
          "button",
          { type: "submit", class: BTN_PRIMARY + " mb-3", disabled: state.isBusy },
          state.isBusy ? Spinner() : "Send reset link",
        ),
      ],
    ),
    m("div", { class: "text-center" }, [
      // A button (not an href-less anchor) so it is keyboard-focusable.
      m(
        "button",
        {
          type: "button",
          class: LINK_CLASS + " type-body",
          onclick: () => ((state.view = "form"), (state.notice = ""), (state.error = "")),
        },
        "Back to sign in",
      ),
    ]),
  );
}

export function AuthPage(): m.Component {
  const params = new URLSearchParams(window.location.search);
  const errorCode = params.get("error");
  const state: PageState = {
    view: "loading",
    // Create-account is the default everywhere: most signed-out visitors are
    // new users, sign-in is one tab-click away, and returning users with a
    // live session never see the form (they get the continue-as interstitial).
    // The one exception is the password_account bounce (a Google attempt on an
    // email that signs in with a password): its remedy IS the sign-in form,
    // which the signup tab keeps collapsed behind the reveal link.
    tab: errorCode === "password_account" ? "signin" : "signup",
    config: null,
    identity: null,
    next: sanitizeNextPath(params.get("next")),
    error: loginErrorCopy(errorCode),
    notice: "",
    isBusy: false,
    isEmailSignupFormRevealed: false,
    turnstileToken: "",
    turnstileWidgetId: null,
    selectedPlan: DEFAULT_SIGNUP_PLAN,
    isTermsAccepted: false,
  };

  async function initialize(): Promise<void> {
    try {
      const [config, identity] = await Promise.all([fetchConfig(), fetchIdentity()]);
      state.config = config;
      state.identity = identity;
    } catch {
      state.config = { turnstile_site_key: "", google_enabled: false };
      state.identity = { signed_in: false };
      state.error = state.error || "Could not reach the sign-in service. Some options may be unavailable.";
    }
    if (state.identity?.signed_in) {
      if (isAuthorizeNext(state.next)) {
        // A pre-existing session requires the explicit confirmation.
        state.view = "interstitial";
      } else {
        // Already signed in: land wherever the pending next points (the web
        // client when none was given), same as a fresh sign-in would.
        navigateTo(state.next);
        return;
      }
    } else {
      state.view = "form";
    }
    m.redraw();
  }

  return {
    oninit() {
      void initialize();
    },
    view() {
      if (state.view === "loading") {
        return m("div", { class: "min-h-full flex items-center justify-center" }, Spinner());
      }
      if (state.view === "interstitial") return InterstitialView(state);
      if (state.view === "forgot") return ForgotPasswordView(state);
      return FormView(state);
    },
  };
}
