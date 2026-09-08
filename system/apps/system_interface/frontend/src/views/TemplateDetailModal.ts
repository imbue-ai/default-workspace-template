/**
 * The detail view behind a template card: the drawing large, the full write-up (the card shows
 * none of it), what the template needs connected before it runs, a link to the repository it is
 * published from, and the two ways to take it on -- adopt it into this machine, or have a new
 * machine made from it. Both start a chat; the launcher owns what the chat is told.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { Modal, MODAL_TITLE_CLASS } from "@imbue/workspace-ui/src/components/Modal";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { CatalogTemplate } from "../models/TemplateCatalog";
import { writeUpParagraphs } from "../models/TemplateCatalog";

const DETAIL_WIDTH_PX = 640;
const ART_FALLBACK_GLYPH_SIZE = 32;
const REQUIREMENT_GLYPH_SIZE = 14;

/** One line of the "Needs" list: what the adopter has to have in hand before the template runs. */
export interface TemplateRequirement {
  label: string;
  detail: string;
}

/**
 * Everything a template needs, in the order it costs the adopter: accounts to connect (one line
 * per service, its permissions behind it), then a model, then keys, then system packages.
 */
export function templateRequirements(template: CatalogTemplate): TemplateRequirement[] {
  const permissionsByService = new Map<string, string[]>();
  for (const account of template.required_accounts) {
    const permissions = permissionsByService.get(account.service) ?? [];
    if (!permissions.includes(account.permission)) permissions.push(account.permission);
    permissionsByService.set(account.service, permissions);
  }
  const requirements: TemplateRequirement[] = [];
  for (const [service, permissions] of permissionsByService) {
    requirements.push({ label: `Connect ${service}`, detail: permissions.join(", ") });
  }
  if (template.needs_ai) {
    requirements.push({ label: "An AI model", detail: "it reasons over what it reads" });
  }
  for (const secret of template.required_secrets) {
    requirements.push({ label: secret, detail: "a key you supply" });
  }
  if (template.apt_packages.length > 0) {
    requirements.push({ label: "System packages", detail: template.apt_packages.join(", ") });
  }
  return requirements;
}

export interface TemplateDetailModalAttrs {
  template: CatalogTemplate;
  onClose: () => void;
  /** "Make it mine": adopt the template into this machine. */
  onAdopt: (template: CatalogTemplate) => void;
  /** "Create a new machine from this": have a fresh machine made from it. */
  onCreateMachine: (template: CatalogTemplate) => void;
}

export function TemplateDetailModal(): m.Component<TemplateDetailModalAttrs> {
  return {
    view(vnode) {
      const { template, onClose, onAdopt, onCreateMachine } = vnode.attrs;
      const paragraphs = writeUpParagraphs(template.what_it_is);
      const requirements = templateRequirements(template);
      return m(
        Modal,
        {
          onDismiss: onClose,
          onEscape: onClose,
          width: DETAIL_WIDTH_PX,
          // The marker rides the body wrapper below: the Modal drops a caller's ``class`` from the
          // card, since a class there would replace the card's own recipe.
          card: { role: "dialog", "aria-modal": "true", "aria-label": template.title, "data-template": template.slug },
          header: [
            m("div", { class: "min-w-0 flex-1" }, [
              m("h3", { class: MODAL_TITLE_CLASS }, template.title),
              template.author === ""
                ? null
                : m("p", { class: "type-helper m-0 text-secondary" }, `by ${template.author}`),
            ]),
            m(
              Button,
              { variant: "ghost", icon: true, sm: true, "aria-label": "Close", onclick: onClose },
              m.trust(icon("close", { size: 16 })),
            ),
          ],
          actions: [
            m(
              Button,
              { extra: "new-tab-template-create-machine", onclick: () => onCreateMachine(template) },
              "Create a new machine from this",
            ),
            m(
              Button,
              { variant: "primary", extra: "new-tab-template-adopt", onclick: () => onAdopt(template) },
              "Make it mine",
            ),
          ],
        },
        [
          m("div", { class: "new-tab-template-detail max-h-[60vh] overflow-y-auto pr-1" }, [
            m(
              "div",
              { class: "aspect-[3/2] w-full overflow-hidden rounded-lg bg-page" },
              template.thumbnail_url === ""
                ? m(
                    "span",
                    { class: "flex h-full w-full items-center justify-center text-faint" },
                    m.trust(icon("box", { size: ART_FALLBACK_GLYPH_SIZE })),
                  )
                : m("img", { src: template.thumbnail_url, alt: "", class: "h-full w-full object-cover" }),
            ),
            (paragraphs.length > 0 ? paragraphs : [template.description]).map((paragraph) =>
              m("p", { class: "type-body mt-4 text-primary" }, paragraph),
            ),
            requirements.length === 0
              ? null
              : m("section", { class: "mt-5" }, [
                  m("h4", { class: "type-section m-0 text-faint" }, "Needs"),
                  m(
                    "ul",
                    { class: "m-0 mt-2 list-none p-0" },
                    requirements.map((requirement) =>
                      m("li", { class: "flex items-baseline gap-2 py-1 type-body text-primary" }, [
                        m(
                          "span",
                          { class: "flex shrink-0 items-center self-center text-faint" },
                          m.trust(icon("key", { size: REQUIREMENT_GLYPH_SIZE })),
                        ),
                        m("span", requirement.label),
                        m("span", { class: "type-helper text-secondary" }, requirement.detail),
                      ]),
                    ),
                  ),
                ]),
            m(
              "a",
              {
                class: "mt-5 inline-flex items-center gap-1.5 type-helper text-secondary hover:text-primary",
                href: template.repository_url,
                target: "_blank",
                rel: "noopener noreferrer",
              },
              [m("span", "View the repository"), m.trust(icon("external-link", { size: REQUIREMENT_GLYPH_SIZE }))],
            ),
          ]),
        ],
      );
    },
  };
}
