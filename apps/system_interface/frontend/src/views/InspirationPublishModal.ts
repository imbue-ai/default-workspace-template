/**
 * Modal that previews an inspiration-publish proposal, lets the user edit the
 * title / description / repo name / visibility / SVG thumbnail, and confirms
 * (or aborts) the publish.
 *
 * Opened by the `inspiration_publish_requested` broadcast (dispatched in
 * models/AgentManager.ts, stored in models/InspirationPublish.ts). App.ts keys
 * this component by proposal slug, so a superseding proposal (a new slug)
 * tears the component down and remounts it, re-running the `oninit` prefill.
 *
 * The SVG thumbnail is untrusted: the live preview sanitizes with DOMPurify
 * before rendering via `m.trust`, and the backend independently re-sanitizes
 * on confirm before writing the value the skill commits -- so the preview
 * sanitize here is display-only defense-in-depth.
 *
 * Dismissal (Cancel / close / backdrop) fires `/api/inspiration/abort` so the
 * publish skill's poll unblocks; a confirmed publish suppresses that abort.
 */

import m from "mithril";
import DOMPurify from "dompurify";
import { apiUrl } from "../base-path";
import type { InspirationProposal } from "../models/InspirationPublish";

export interface InspirationPublishModalAttrs {
  // Prefill source; `slug` is the immutable identity/key.
  proposal: InspirationProposal;
  // App.ts wires this to the slug-guarded closeInspirationModal.
  onDismiss: () => void;
}

// Reply from /api/inspiration/publish-confirm (and the value written to the
// response file the skill polls). status is "confirmed" or "aborted".
interface InspirationPublishResponse {
  status: string;
  slug: string;
  title: string;
  description: string;
  repo_name: string;
  visibility: string;
  thumbnail_svg: string;
}

// Client-side hint validation only -- the backend re-validates authoritatively
// (this guards against argument-injection into `gh repo create`).
const REPO_NAME_REGEX = /^[A-Za-z0-9._-]+$/;

function isValidRepoName(value: string): boolean {
  return REPO_NAME_REGEX.test(value) && !value.startsWith("-");
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

function warningIcon(): m.Vnode {
  return m(
    "svg",
    {
      width: 16,
      height: 16,
      viewBox: "0 0 24 24",
      fill: "none",
      "aria-hidden": "true",
    },
    [
      m("circle", { cx: 12, cy: 12, r: 10, stroke: "currentColor", "stroke-width": 1.8 }),
      m("path", { d: "M12 8v4.5", stroke: "currentColor", "stroke-width": 1.8, "stroke-linecap": "round" }),
      m("circle", { cx: 12, cy: 16, r: 0.9, fill: "currentColor" }),
    ],
  );
}

export function InspirationPublishModal(): m.Component<InspirationPublishModalAttrs> {
  // Editable form fields, prefilled from attrs.proposal in oninit. Because
  // App.ts keys the vnode by slug, a superseding proposal remounts the
  // component and re-runs this prefill.
  let title = "";
  let description = "";
  let repoName = "";
  let visibility: "private" | "public" = "private";
  let thumbnailSvg = "";
  let submitting = false;
  let errorMessage: string | null = null;
  // Set once a publish is confirmed so the onremove teardown does NOT fire a
  // spurious abort against a proposal the backend already resolved. Mirrors
  // the ClaudeLoginModal `sessionId`-nulling guard.
  let confirmed = false;
  let attrsRef: InspirationPublishModalAttrs | null = null;

  function prefillFrom(proposal: InspirationProposal): void {
    title = proposal.title;
    description = proposal.description;
    repoName = proposal.repo_name;
    visibility = proposal.visibility === "public" ? "public" : "private";
    thumbnailSvg = proposal.thumbnail_svg;
  }

  // Sanitize the (untrusted, user-editable) SVG for the live preview. DOMPurify
  // under the svg profile strips <script>, on* handlers, and <foreignObject>;
  // FORBID_TAGS keeps foreignObject/script out even if a profile shift would
  // otherwise let them back in.
  function sanitizedSvg(): string {
    return DOMPurify.sanitize(thumbnailSvg, {
      USE_PROFILES: { svg: true, svgFilters: true },
      FORBID_TAGS: ["foreignObject", "script"],
    });
  }

  // Fire-and-forget: unblock the skill's poll. Never blocks dismissal.
  function abortPublish(): void {
    void m.request({
      method: "POST",
      url: apiUrl("/api/inspiration/abort"),
      body: { slug: attrsRef?.proposal.slug ?? null },
    });
  }

  function dismiss(): void {
    abortPublish();
    attrsRef?.onDismiss();
  }

  async function submitConfirm(): Promise<void> {
    if (submitting) return;
    submitting = true;
    errorMessage = null;
    m.redraw();
    try {
      const resp = await m.request<InspirationPublishResponse>({
        method: "POST",
        url: apiUrl("/api/inspiration/publish-confirm"),
        body: {
          // slug is the immutable identity, NOT editable.
          slug: attrsRef!.proposal.slug,
          title: title.trim(),
          description: description.trim(),
          repo_name: repoName.trim(),
          visibility,
          // Backend re-sanitizes server-side before writing the response.
          thumbnail_svg: thumbnailSvg,
        },
      });
      if (resp.status === "confirmed") {
        confirmed = true;
        attrsRef!.onDismiss();
      } else {
        errorMessage = "Publish was not confirmed.";
        submitting = false;
        m.redraw();
      }
    } catch (error) {
      errorMessage = (error as { response?: { detail?: string } }).response?.detail ?? "Failed to publish inspiration";
      submitting = false;
      m.redraw();
    }
  }

  function renderInlineError(): m.Vnode | null {
    if (errorMessage === null) return null;
    return m("div.claude-login-error-callout", [warningIcon(), m("span", errorMessage)]);
  }

  function renderForm(): m.Vnode[] {
    const repoNameInvalid = repoName.trim().length > 0 && !isValidRepoName(repoName.trim());
    return [
      m(
        "p.claude-login-lead",
        "Review this inspiration before publishing it. Edit any field, then publish to a GitHub repo.",
      ),
      m("div.inspiration-publish-grid", [
        m("div.claude-login-field", [
          m("label.claude-login-step-label", { for: "inspiration-publish-title" }, "Title"),
          m("input.claude-login-input", {
            id: "inspiration-publish-title",
            type: "text",
            value: title,
            spellcheck: false,
            oninput: (event: InputEvent) => {
              title = (event.target as HTMLInputElement).value;
            },
          }),
        ]),
        m("div.claude-login-field", [
          m("label.claude-login-step-label", { for: "inspiration-publish-description" }, "Description"),
          m("textarea.claude-login-input.inspiration-publish-textarea", {
            id: "inspiration-publish-description",
            rows: 3,
            value: description,
            spellcheck: false,
            oninput: (event: InputEvent) => {
              description = (event.target as HTMLTextAreaElement).value;
            },
          }),
        ]),
        m("div.claude-login-field", [
          m("label.claude-login-step-label", { for: "inspiration-publish-repo-name" }, "Repository name"),
          m("input.claude-login-input.claude-login-input--mono", {
            id: "inspiration-publish-repo-name",
            type: "text",
            value: repoName,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              repoName = (event.target as HTMLInputElement).value;
            },
          }),
          repoNameInvalid
            ? m(
                "p.claude-login-helper.inspiration-publish-helper--warn",
                "Use only letters, numbers, dot, underscore, or hyphen, and don't start with a hyphen.",
              )
            : null,
        ]),
        m("div.claude-login-field", [
          m("label.claude-login-step-label", { for: "inspiration-publish-visibility" }, "Visibility"),
          m(
            "select.claude-login-input",
            {
              id: "inspiration-publish-visibility",
              value: visibility,
              onchange: (event: Event) => {
                const chosen = (event.target as HTMLSelectElement).value;
                visibility = chosen === "public" ? "public" : "private";
              },
            },
            [m("option", { value: "private" }, "Private"), m("option", { value: "public" }, "Public")],
          ),
        ]),
        m("div.claude-login-field", [
          m("label.claude-login-step-label", { for: "inspiration-publish-svg" }, "Thumbnail SVG"),
          m("textarea.claude-login-input.claude-login-input--mono.inspiration-publish-textarea", {
            id: "inspiration-publish-svg",
            rows: 5,
            value: thumbnailSvg,
            spellcheck: false,
            autocomplete: "off",
            oninput: (event: InputEvent) => {
              thumbnailSvg = (event.target as HTMLTextAreaElement).value;
            },
          }),
          m("p.claude-login-helper", "The preview updates as you edit. The markup is sanitized before it's rendered."),
        ]),
        m("div.claude-login-field", [
          m("span.claude-login-step-label", "Preview"),
          m("div.inspiration-publish-preview", m.trust(sanitizedSvg())),
        ]),
      ]),
    ];
  }

  return {
    oninit(vnode: m.Vnode<InspirationPublishModalAttrs>) {
      attrsRef = vnode.attrs;
      prefillFrom(vnode.attrs.proposal);
    },

    oncreate(vnode: m.VnodeDOM<InspirationPublishModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onupdate(vnode: m.VnodeDOM<InspirationPublishModalAttrs>) {
      attrsRef = vnode.attrs;
    },

    onremove() {
      // Only abort if the modal is being torn down WITHOUT a confirmed publish.
      // A successful publish already resolved the proposal server-side, so a
      // trailing abort would clobber the confirmed response file.
      if (!confirmed) {
        abortPublish();
      }
    },

    view() {
      const onClose = (): void => dismiss();
      const canPublish =
        !submitting && title.trim().length > 0 && repoName.trim().length > 0 && isValidRepoName(repoName.trim());
      return m(
        "div.claude-login-overlay.inspiration-publish-overlay",
        {
          onclick: (event: MouseEvent) => {
            if (event.target === event.currentTarget) onClose();
          },
        },
        m(
          "div.claude-login-modal.inspiration-publish-modal",
          {
            role: "dialog",
            "aria-modal": "true",
            "aria-label": "Publish inspiration",
          },
          [
            m("div.claude-login-header", [
              m("h2.claude-login-title", "Publish inspiration"),
              m("button.claude-login-close", { type: "button", onclick: onClose, "aria-label": "Close" }, closeIcon()),
            ]),
            m("div.claude-login-body", [renderInlineError(), ...renderForm()]),
            m("div.claude-login-footer.claude-login-footer--spread", [
              m(
                "button.claude-login-button.claude-login-button--ghost",
                { type: "button", onclick: onClose },
                "Cancel",
              ),
              m(
                "button.claude-login-button.claude-login-button--primary",
                {
                  type: "button",
                  disabled: !canPublish,
                  onclick: () => {
                    void submitConfirm();
                  },
                },
                submitting ? "Publishing..." : "Publish",
              ),
            ]),
          ],
        ),
      );
    },
  };
}
