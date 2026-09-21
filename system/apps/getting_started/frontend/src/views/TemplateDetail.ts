/**
 * The detail page behind a template card: the drawing large, the full write-up (the card shows
 * none of it), what the template needs connected before it runs, a link to the repository it is
 * published from, and the two ways to take it on -- adopt it into this machine, or have a new
 * machine made from it. Both start a chat; the page owns what the chat is told. A back control
 * returns to the tiles and shelves; there is no dialog, since the page has nothing under it to
 * dim.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { CatalogTemplate } from "../models/TemplateCatalog";
import { writeUpParagraphs } from "../models/TemplateCatalog";
import { TemplateArt } from "./TemplateArt";

const ART_FALLBACK_GLYPH_SIZE = 32;
const REQUIREMENT_GLYPH_SIZE = 14;
const BACK_GLYPH_SIZE = 16;

/** One line of the "Needs" list: what the adopter has to have in hand before the template runs. */
export interface TemplateRequirement {
  label: string;
  detail: string;
}

/**
 * Everything a template needs, in the order it costs the adopter: accounts to connect (one line
 * per scope, its permissions behind it), then a model, then keys, then system packages.
 */
export function templateRequirements(template: CatalogTemplate): TemplateRequirement[] {
  const permissionsByScope = new Map<string, string[]>();
  for (const account of template.required_accounts) {
    const permissions = permissionsByScope.get(account.scope) ?? [];
    if (!permissions.includes(account.permission)) permissions.push(account.permission);
    permissionsByScope.set(account.scope, permissions);
  }
  const requirements: TemplateRequirement[] = [];
  for (const [scope, permissions] of permissionsByScope) {
    requirements.push({ label: `Connect ${scope}`, detail: permissions.join(", ") });
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

/** Adopt a template into this machine: the first message of the chat "Make it mine" starts. */
export function adoptTemplateMessage(template: CatalogTemplate): string {
  return `/use-template ${template.repository_url}`;
}

/** Have a new machine made from a template: the first message of the chat that action starts. */
export function createMachineFromTemplateMessage(template: CatalogTemplate): string {
  return (
    `Please create a new Mind machine for me from the template at ${template.repository_url} ` +
    "(the minds-api skill can create one). Walk me through anything it needs from me, like permissions " +
    "or accounts, and tell me when it is ready."
  );
}

export interface TemplateDetailAttrs {
  template: CatalogTemplate;
  /** Back to the tiles and shelves. */
  onBack: () => void;
  /** Start a chat whose first message is ``text``. */
  onStartWithText: (text: string) => void;
}

export function TemplateDetail(): m.Component<TemplateDetailAttrs> {
  return {
    view(vnode) {
      const { template, onBack, onStartWithText } = vnode.attrs;
      const paragraphs = writeUpParagraphs(template.what_it_is);
      const requirements = templateRequirements(template);
      return m("section", { class: "new-tab-template-detail", "data-template": template.slug }, [
        m(
          Button,
          { variant: "ghost", sm: true, extra: "new-tab-template-back", "aria-label": "Back", onclick: onBack },
          [m.trust(icon("chevron-left", { size: BACK_GLYPH_SIZE })), m("span", "Back")],
        ),
        m("div", { class: "mt-4 flex items-start gap-6 max-md:flex-col" }, [
          m(
            "div",
            { class: "w-full shrink-0 md:w-80" },
            m(TemplateArt, { template, frameClass: "w-full rounded-lg", glyphSize: ART_FALLBACK_GLYPH_SIZE }),
          ),
          m("div", { class: "min-w-0 flex-1" }, [
            m("h2", { class: "type-heading m-0 text-primary" }, template.title),
            template.author === ""
              ? null
              : m("p", { class: "type-helper m-0 mt-1 text-secondary" }, `by ${template.author}`),
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
            m("div", { class: "mt-6 flex flex-wrap gap-2" }, [
              m(
                Button,
                {
                  variant: "primary",
                  extra: "new-tab-template-adopt",
                  onclick: () => onStartWithText(adoptTemplateMessage(template)),
                },
                "Make it mine",
              ),
              m(
                Button,
                {
                  extra: "new-tab-template-create-machine",
                  onclick: () => onStartWithText(createMachineFromTemplateMessage(template)),
                },
                "Create a new machine from this",
              ),
            ]),
          ]),
        ]),
      ]);
    },
  };
}
