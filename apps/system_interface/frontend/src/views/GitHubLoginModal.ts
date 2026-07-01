/**
 * Modal that walks the user through authenticating `gh` (the GitHub CLI)
 * inside a mind so that pushing an inspiration over HTTPS works with no
 * agent restart. Two sign-in paths:
 *
 * - Web / device flow: drive `gh auth login --web` via the PTY subprocess on
 *   the backend, show the printed user code + verification URL, and let the
 *   user confirm once they've entered the code in their browser.
 * - Paste a token: paste a `ghp_...` / `github_pat_...` personal access
 *   token; the backend pipes it to `gh auth login --with-token` over stdin
 *   and wires the git credential helper.
 *
 * The modal is a single app-level instance driven by global GitHub-auth state
 * (models/GitHubAuth.ts): the `github_auth_required` broadcast opens it when
 * the publish skill's own `gh auth status` check fails, and it closes only
 * when the user dismisses it. Like the Claude modal, it does not poll the
 * backend status endpoint -- it is push-driven and closed on dismiss/success.
 */

import m from "mithril";
import { apiUrl } from "../base-path";

const _GITHUB_HOST = "github.com";

interface GitHubAuthStatus {
  logged_in: boolean;
  username?: string | null;
  host?: string;
}

interface GitHubAuthStartResponse {
  session_id: string;
  user_code: string;
  verification_url: string;
}

type Mode = "select_method" | "raw_token_form" | "awaiting_device_code" | "verifying" | "success" | "error";

export interface GitHubLoginModalAttrs {
  // Called when the user closes the modal -- either after a successful
  // sign-in flow ("Done" button) or via the close affordance before signing
  // in. A subsequent `github_auth_required` event will reopen it.
  onDismiss: () => void;
}

function spinnerIcon(): m.Vnode {
  return m("svg.claude-login-spinner", { viewBox: "0 0 24 24", fill: "none", "aria-hidden": "true" }, [
    m("circle", {
      cx: 12,
      cy: 12,
      r: 10,
      stroke: "currentColor",
      "stroke-opacity": 0.18,
      "stroke-width": 3,
    }),
    m("path", {
      d: "M22 12a10 10 0 0 1-10 10",
      stroke: "currentColor",
      "stroke-width": 3,
      "stroke-linecap": "round",
    }),
  ]);
}

function checkIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 26,
      height: 26,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M5 12.5l4.5 4.5L19 7.5",
      stroke: "currentColor",
      "stroke-width": 2.5,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

function warningIcon(small = false): m.Vnode {
  const s = small ? 16 : 26;
  return m(
    "svg",
    {
      width: s,
      height: s,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("circle", {
        cx: 12,
        cy: 12,
        r: 10,
        stroke: "currentColor",
        "stroke-width": small ? 1.8 : 2,
      }),
      m("path", {
        d: "M12 8v4.5",
        stroke: "currentColor",
        "stroke-width": small ? 1.8 : 2.2,
        "stroke-linecap": "round",
      }),
      m("circle", { cx: 12, cy: 16, r: 0.9, fill: "currentColor" }),
    ],
  );
}

function closeIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 16,
      height: 16,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M6 6l12 12M18 6L6 18",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
    }),
  );
}

// The GitHub "Octocat mark" glyph, simplified to a single path. Marks the
// web/device flow as the recommended sign-in path.
function githubLogoIcon(): m.Vnode {
  return m(
    "svg.github-login-logo.claude-login-logo",
    { viewBox: "0 0 16 16", "aria-hidden": "true" },
    m("path", {
      "fill-rule": "evenodd",
      "clip-rule": "evenodd",
      fill: "currentColor",
      d: "M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0016 8c0-4.42-3.58-8-8-8z",
    }),
  );
}

function chevronRightIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 18,
      height: 18,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    m("path", {
      d: "M9 6l6 6-6 6",
      stroke: "currentColor",
      "stroke-width": 2,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
    }),
  );
}

function externalLinkIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 15,
      height: 15,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("path", {
        d: "M14 4h6v6",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
      m("path", {
        d: "M20 4l-9 9",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
      m("path", {
        d: "M19 13v6a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h6",
        stroke: "currentColor",
        "stroke-width": 2,
        "stroke-linecap": "round",
        "stroke-linejoin": "round",
      }),
    ],
  );
}

export function GitHubLoginModal(): m.Component<GitHubLoginModalAttrs> {
  let mode: Mode = "select_method";
  let sessionId: string | null = null;
  let userCode: string | null = null;
  let verificationUrl: string | null = null;
  let token = "";
  let tokenRevealed = false;
  let codeCopied = false;
  // Set when a clipboard write was attempted but rejected (insecure context,
  // denied permission). Drives the "Failed to copy" label so the user knows
  // to select and copy the code by hand from the block that's already shown.
  let codeCopyFailed = false;
  let codeCopiedResetHandle: ReturnType<typeof setTimeout> | null = null;
  let errorMessage: string | null = null;
  let verifyingTitle = "Working...";
  let verifyingDetail: string | null = null;
  let successStatus: GitHubAuthStatus | null = null;
  let attrsRef: GitHubLoginModalAttrs | null = null;

  function clearError(): void {
    errorMessage = null;
  }

  function setError(message: string): void {
    errorMessage = message;
    mode = "error";
    m.redraw();
  }

  // Surface a failure inline within the token form, where the user can simply
  // re-submit a corrected token in place, instead of swapping to the
  // full-screen `error` view. Device-flow failures do NOT use this: the
  // device session is single-use, so they route to `setError` (the full
  // "Start over" screen), which also covers `startWebLogin` failures.
  function setInlineTokenError(message: string): void {
    errorMessage = message;
    mode = "raw_token_form";
    m.redraw();
  }

  function startVerifying(title: string, detail: string | null): void {
    verifyingTitle = title;
    verifyingDetail = detail;
    mode = "verifying";
    m.redraw();
  }

  async function startWebLogin(): Promise<void> {
    clearError();
    startVerifying("Starting sign-in...", "Spawning the GitHub device login flow.");
    try {
      const response = await m.request<GitHubAuthStartResponse>({
        method: "POST",
        url: apiUrl("/api/github-auth/start"),
        body: { host: _GITHUB_HOST },
      });
      sessionId = response.session_id;
      userCode = response.user_code;
      verificationUrl = response.verification_url;
      mode = "awaiting_device_code";
      m.redraw();
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setError(errResp?.detail ?? "Failed to start GitHub login");
    }
  }

  async function submitDeviceCode(): Promise<void> {
    if (!sessionId) return;
    clearError();
    startVerifying("Verifying...", "Completing sign-in with GitHub.");
    const submittedSessionId = sessionId;
    // Submitting completes the device/web login subprocess (the backend waits
    // for EOF and then discards its in-flight session record), so the id we
    // just submitted is consumed regardless of the outcome. Clear it locally
    // too so a later modal-unmount (e.g. Done after a successful sign-in) does
    // not fire a spurious /abort against a session the backend already dropped.
    sessionId = null;
    try {
      const status = await m.request<GitHubAuthStatus>({
        method: "POST",
        url: apiUrl("/api/github-auth/submit-code"),
        body: { session_id: submittedSessionId },
      });
      if (status.logged_in) {
        successStatus = status;
        mode = "success";
        m.redraw();
      } else {
        // The device session is single-use -- the backend has torn down its
        // `gh auth login` subprocess and `sessionId` was cleared above -- so
        // there is nothing left to retry in place. Route to the full error
        // screen, whose only action is "Start over" (a fresh sign-in flow).
        setError("Authentication did not succeed.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      // Same single-use-session reasoning as the branch above: the device
      // session is already gone, so send the user to the "Start over" error
      // screen rather than back to the code screen.
      setError(errResp?.detail ?? "Failed to complete sign-in");
    }
  }

  async function submitRawToken(): Promise<void> {
    if (!token.trim()) return;
    clearError();
    startVerifying("Saving your token...", "Connecting this mind to GitHub.");
    try {
      const status = await m.request<GitHubAuthStatus>({
        method: "POST",
        url: apiUrl("/api/github-auth/submit-raw-token"),
        body: {
          token: token.trim(),
          host: _GITHUB_HOST,
        },
      });
      if (status.logged_in) {
        successStatus = status;
        mode = "success";
        m.redraw();
      } else {
        setInlineTokenError("GitHub did not accept the token. Double-check and try again.");
      }
    } catch (error) {
      const errResp = (error as { response?: { detail?: string } }).response;
      setInlineTokenError(errResp?.detail ?? "Failed to save token");
    }
  }

  function abortLoginIfActive(): void {
    if (sessionId !== null) {
      void m.request({ method: "POST", url: apiUrl("/api/github-auth/abort"), body: {} });
    }
    sessionId = null;
    userCode = null;
    verificationUrl = null;
    resetCodeCopied();
  }

  function resetCodeCopied(): void {
    codeCopied = false;
    codeCopyFailed = false;
    if (codeCopiedResetHandle !== null) {
      clearTimeout(codeCopiedResetHandle);
      codeCopiedResetHandle = null;
    }
  }

  async function copyUserCode(): Promise<void> {
    if (!userCode) return;
    try {
      await navigator.clipboard.writeText(userCode);
    } catch {
      // Clipboard access can be denied (insecure context, permissions). Tell
      // the user the copy failed; the code is already shown in a selectable
      // block, so they can copy it by hand. Clear any stale "Code copied"
      // state so the UI isn't contradictory.
      codeCopied = false;
      if (codeCopiedResetHandle !== null) {
        clearTimeout(codeCopiedResetHandle);
        codeCopiedResetHandle = null;
      }
      codeCopyFailed = true;
      m.redraw();
      return;
    }
    codeCopyFailed = false;
    codeCopied = true;
    if (codeCopiedResetHandle !== null) clearTimeout(codeCopiedResetHandle);
    codeCopiedResetHandle = setTimeout(() => {
      codeCopied = false;
      codeCopiedResetHandle = null;
      m.redraw();
    }, 2000);
    m.redraw();
  }

  function goBackToMethodSelection(): void {
    abortLoginIfActive();
    token = "";
    tokenRevealed = false;
    clearError();
    mode = "select_method";
    m.redraw();
  }

  // ----- Renderers -----

  // The method-selection screen leads with the web/device flow as the
  // recommended default -- a logo, headline, and full-width primary button --
  // and tucks the paste-a-token path behind a secondary option.
  function renderMethodSelection(): m.Vnode {
    return m("div.claude-login-select", [
      m("div.claude-login-primary", [
        githubLogoIcon(),
        m("h3.claude-login-primary-headline", "Sign in with GitHub"),
        m(
          "p.claude-login-primary-sub",
          "Connect this mind to GitHub in your browser so it can push and publish over HTTPS.",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary.claude-login-button--block",
          { type: "button", onclick: () => void startWebLogin() },
          "Continue with GitHub",
        ),
      ]),
      m("div.claude-login-alts", [
        m("div.claude-login-alts-list", [
          m(
            "button.claude-login-alt",
            {
              type: "button",
              onclick: () => {
                mode = "raw_token_form";
                m.redraw();
              },
            },
            [
              m("span.claude-login-alt-text", [
                m("span.claude-login-alt-name", "Paste a token"),
                m("span.claude-login-alt-desc", "Paste a personal access token (ghp_... or github_pat_...)."),
              ]),
              m("span.claude-login-alt-go", chevronRightIcon()),
            ],
          ),
        ]),
      ]),
    ]);
  }

  function renderRawTokenForm(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Paste a GitHub personal access token. It's saved to this mind's gh credential store."),
      m("div.claude-login-field", [
        m("label.claude-login-step-label", { for: "github-login-token-input" }, [
          m("span.claude-login-step-num", "1"),
          "Your GitHub token",
        ]),
        m("div.claude-login-input-wrap", [
          m("input.claude-login-input.claude-login-input--mono.claude-login-input--with-action", {
            id: "github-login-token-input",
            type: tokenRevealed ? "text" : "password",
            placeholder: "ghp_...",
            value: token,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              token = (event.target as HTMLInputElement).value;
            },
            onkeydown: (event: KeyboardEvent) => {
              if (event.key === "Enter" && token.trim()) {
                event.preventDefault();
                void submitRawToken();
              }
            },
          }),
          m(
            "button.claude-login-input-action",
            {
              type: "button",
              onclick: () => {
                tokenRevealed = !tokenRevealed;
                m.redraw();
              },
              "aria-label": tokenRevealed ? "Hide token" : "Show token",
            },
            tokenRevealed ? "Hide" : "Show",
          ),
        ]),
        m(
          "p.claude-login-helper",
          "You can create tokens at github.com/settings/tokens. The token needs repo scope to push.",
        ),
      ]),
    ];
  }

  function renderDeviceCodeEntry(): m.Vnode[] {
    return [
      m("p.claude-login-lead", "Open the sign-in page, enter the code below, then come back and finish."),
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "1"), "Open the sign-in page"]),
        m(
          "a.claude-login-button.claude-login-button--primary.claude-login-button--block.claude-login-button--link",
          {
            href: verificationUrl,
            target: "_blank",
            rel: "noopener noreferrer",
          },
          [m("span", "Open GitHub"), externalLinkIcon()],
        ),
      ]),
      m("div.claude-login-step", [
        m("div.claude-login-step-label", [m("span.claude-login-step-num", "2"), "Enter this code"]),
        // The user code is shown in a selectable monospace block so the user
        // can always copy it by hand even if the clipboard write is rejected.
        userCode !== null ? m("div.github-login-usercode.claude-login-rawurl", { tabindex: 0 }, userCode) : null,
        m("p.claude-login-copylink", [
          m(
            "button.claude-login-copylink-action",
            {
              type: "button",
              onclick: () => {
                void copyUserCode();
              },
            },
            codeCopied ? "Code copied" : codeCopyFailed ? "Failed to copy" : "Copy the code",
          ),
          codeCopied ? "" : codeCopyFailed ? " — copy it from the box above manually." : " and paste it into GitHub.",
        ]),
        m(
          "p.claude-login-helper",
          "Enter the code on the GitHub page and authorize access. Then come back and select “I've entered the code.”",
        ),
      ]),
    ];
  }

  function renderStatus(kind: "loading" | "success" | "error", title: string, detail: string | null): m.Vnode {
    const icon = kind === "loading" ? spinnerIcon() : kind === "success" ? checkIcon() : warningIcon();
    return m("div.claude-login-status", [
      m(`div.claude-login-status-icon.claude-login-status-icon--${kind}`, icon),
      m("p.claude-login-status-title", title),
      detail !== null ? m("p.claude-login-status-detail", detail) : null,
    ]);
  }

  function renderSuccess(): m.Vnode {
    const username = successStatus?.username ?? null;
    const detail = username ? `Signed in as ${username}.` : "You're signed in.";
    return renderStatus("success", "Connected to GitHub", detail);
  }

  function renderInlineError(): m.Vnode {
    return m("div.github-login-error-callout.claude-login-error-callout", [
      warningIcon(true),
      m("span", errorMessage ?? ""),
    ]);
  }

  // ----- Layout (header / body / footer) -----

  function titleForMode(): string {
    if (mode === "success") return "Signed in";
    if (mode === "error") return "Something went wrong";
    if (mode === "verifying") return "Just a moment";
    if (mode === "raw_token_form") return "Sign in with a token";
    if (mode === "awaiting_device_code") return "Finish signing in";
    return "Sign in to GitHub";
  }

  function renderBody(): m.Vnode | m.Vnode[] {
    if (mode === "success") return renderSuccess();
    if (mode === "error") {
      return renderStatus("error", "Couldn't complete sign-in", errorMessage ?? "An unexpected error occurred.");
    }
    if (mode === "verifying") return renderStatus("loading", verifyingTitle, verifyingDetail);
    if (mode === "awaiting_device_code") return renderDeviceCodeEntry();
    if (mode === "raw_token_form") return renderRawTokenForm();
    return renderMethodSelection();
  }

  function renderFooter(): m.Vnode | null {
    if (mode === "select_method" || mode === "verifying") return null;
    if (mode === "success") {
      return m("div.claude-login-footer", [
        m(
          "button.claude-login-button.claude-login-button--primary",
          { type: "button", onclick: () => attrsRef?.onDismiss() },
          "Done",
        ),
      ]);
    }
    if (mode === "error") {
      // A sign-in failure (failed device-flow start, or a submitted device
      // code that consumed the single-use session) leaves no live session to
      // retry against, so the only forward action is to start over. The header
      // close button and backdrop click still dismiss the modal, so a single
      // primary action here is not a dead end.
      return m("div.claude-login-footer", [
        m(
          "button.claude-login-button.claude-login-button--primary.claude-login-button--block",
          { type: "button", onclick: () => goBackToMethodSelection() },
          "Start over",
        ),
      ]);
    }
    if (mode === "raw_token_form") {
      return m("div.claude-login-footer.claude-login-footer--spread", [
        m(
          "button.claude-login-button.claude-login-button--ghost",
          { type: "button", onclick: () => goBackToMethodSelection() },
          "Back",
        ),
        m(
          "button.claude-login-button.claude-login-button--primary",
          {
            type: "button",
            disabled: !token.trim(),
            onclick: () => {
              void submitRawToken();
            },
          },
          "Save & finish",
        ),
      ]);
    }
    // awaiting_device_code
    return m("div.claude-login-footer.claude-login-footer--spread", [
      m(
        "button.claude-login-button.claude-login-button--ghost",
        { type: "button", onclick: () => goBackToMethodSelection() },
        "Back",
      ),
      m(
        "button.claude-login-button.claude-login-button--primary",
        {
          type: "button",
          onclick: () => {
            void submitDeviceCode();
          },
        },
        "I've entered the code",
      ),
    ]);
  }

  return {
    oncreate(vnode: m.VnodeDOM<GitHubLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onupdate(vnode: m.VnodeDOM<GitHubLoginModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      abortLoginIfActive();
    },

    view() {
      const onClose = (): void => attrsRef?.onDismiss();
      return m(
        "div.github-login-overlay.claude-login-overlay",
        {
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) onClose();
          },
        },
        m(
          "div.github-login-modal.claude-login-modal",
          {
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Sign in to GitHub",
          },
          [
            m("div.claude-login-header", [
              m("h2.claude-login-title", titleForMode()),
              m("button.claude-login-close", { type: "button", onclick: onClose, "aria-label": "Close" }, closeIcon()),
            ]),
            m(
              "div.claude-login-body",
              mode === "awaiting_device_code" || mode === "raw_token_form"
                ? [errorMessage !== null ? renderInlineError() : null, renderBody()]
                : renderBody(),
            ),
            renderFooter(),
          ],
        ),
      );
    },
  };
}
