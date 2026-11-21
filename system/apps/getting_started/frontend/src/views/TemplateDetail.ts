/**
 * The detail page behind a catalog card: the picture large, the full write-up (the card shows
 * none of it), what the listing needs connected before it runs, a link to the repository it is
 * published from, and the two ways to take it on. A template is adopted into this workspace, or a
 * new workspace is made from it; a published app is added to this workspace, or a new workspace
 * is made with it installed. All four start a chat; the page owns what the chat is told. A back
 * control returns to the tiles and shelves; there is no dialog, since the page has nothing under
 * it to dim.
 */

import m from "mithril";
import { Button } from "@imbue/workspace-ui/src/components/Button";
import { icon } from "@imbue/workspace-ui/src/components/icons";
import type { CatalogApp, CatalogListing, CatalogRequiredAccount, CatalogTemplate } from "../models/TemplateCatalog";
import { writeUpParagraphs } from "../models/TemplateCatalog";
import { TemplateArt } from "./TemplateArt";
import { listingCaption, listingMarker } from "./TemplateShelves";

const ART_FALLBACK_GLYPH_SIZE = 32;
const REQUIREMENT_GLYPH_SIZE = 14;
const BACK_GLYPH_SIZE = 16;

/** One line of the "Needs" list: what the adopter has to have in hand before the template or app runs. */
export interface TemplateRequirement {
  label: string;
  detail: string;
}

/** What a template and an app both say they need. */
export interface CatalogNeeds {
  required_accounts: CatalogRequiredAccount[];
  required_secrets: string[];
  needs_ai: boolean;
  apt_packages: string[];
}

/**
 * Everything a template or an app needs, in the order it costs the adopter: accounts to connect
 * (one line per scope, its permissions behind it), then a model, then keys, then system packages.
 */
export function templateRequirements(template: CatalogNeeds): TemplateRequirement[] {
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

/** Adopt a template into this workspace: the first message of the chat "Make it mine" starts. */
export function adoptTemplateMessage(template: CatalogTemplate): string {
  return `/use-template ${template.repository_url}`;
}

// What both create-a-workspace requests end on: where the agent finds how, and what the user is owed.
const CREATE_WORKSPACE_REQUEST_TAIL =
  "(the imbue-studio skill can create one). Walk me through anything it needs from me, like permissions " +
  "or accounts, and tell me when it is ready.";

/** Have a new workspace made from a template: the first message of the chat that action starts. */
export function createMachineFromTemplateMessage(template: CatalogTemplate): string {
  return (
    `Please create a new Imbue Studio workspace for me from the template at ${template.repository_url} ` +
    CREATE_WORKSPACE_REQUEST_TAIL
  );
}

/** Add a published app to this workspace: the first message of the chat "Add to this workspace" starts. */
export function installAppMessage(app: CatalogApp): string {
  return `/install-app ${app.repository_url}`;
}

/**
 * Have a new workspace made with a published app installed: the template request above with the
 * app's repository in the template's place, and worded so the agent does not take it for one (a
 * workspace is never created from an app's repository; the app is installed into a new one).
 */
export function createWorkspaceWithAppMessage(app: CatalogApp): string {
  return (
    `Please create a new Imbue Studio workspace for me with the app at ${app.repository_url} installed in it ` +
    CREATE_WORKSPACE_REQUEST_TAIL
  );
}

/** One of the two ways to take a listing on: a button and the first message of the chat it starts. */
export interface ListingAction {
  label: string;
  /** The button's marker class. */
  marker: string;
  message: string;
}

/** The two actions of a detail page, the primary one first. */
export function listingActions(listing: CatalogListing): [ListingAction, ListingAction] {
  if (listing.kind === "template") {
    return [
      { label: "Make it mine", marker: "new-tab-template-adopt", message: adoptTemplateMessage(listing.template) },
      {
        label: "Create a new workspace from this",
        marker: "new-tab-template-create-machine",
        message: createMachineFromTemplateMessage(listing.template),
      },
    ];
  }
  return [
    { label: "Add to this workspace", marker: "new-tab-app-install", message: installAppMessage(listing.app) },
    {
      label: "Create a new workspace with this",
      marker: "new-tab-app-create-workspace",
      message: createWorkspaceWithAppMessage(listing.app),
    },
  ];
}

/** A listing's write-up: a template's paragraphs, falling back to its description; an app has only a description. */
export function listingParagraphs(listing: CatalogListing): string[] {
  if (listing.kind === "app") return listing.app.description === "" ? [] : [listing.app.description];
  const paragraphs = writeUpParagraphs(listing.template.what_it_is);
  return paragraphs.length > 0 ? paragraphs : [listing.template.description];
}

export interface TemplateDetailAttrs {
  listing: CatalogListing;
  /** Back to the tiles and shelves. */
  onBack: () => void;
  /** Start a chat whose first message is ``text``. */
  onStartWithText: (text: string) => void;
}

export function TemplateDetail(): m.Component<TemplateDetailAttrs> {
  return {
    view(vnode) {
      const { listing, onBack, onStartWithText } = vnode.attrs;
      const entry = listing.kind === "template" ? listing.template : listing.app;
      const caption = listingCaption(listing);
      const requirements = templateRequirements(entry);
      const [primaryAction, secondaryAction] = listingActions(listing);
      return m("section", { class: "new-tab-template-detail", ...listingMarker(listing) }, [
        m(
          Button,
          { variant: "ghost", sm: true, extra: "new-tab-template-back", "aria-label": "Back", onclick: onBack },
          [m.trust(icon("chevron-left", { size: BACK_GLYPH_SIZE })), m("span", "Back")],
        ),
        m("div", { class: "mt-4 flex items-start gap-6 max-md:flex-col" }, [
          m(
            "div",
            { class: "w-full shrink-0 md:w-80" },
            m(TemplateArt, { listing, frameClass: "w-full rounded-lg", glyphSize: ART_FALLBACK_GLYPH_SIZE }),
          ),
          m("div", { class: "min-w-0 flex-1" }, [
            m("h2", { class: "type-heading m-0 text-primary" }, caption.title),
            caption.author === ""
              ? null
              : m("p", { class: "type-helper m-0 mt-1 text-secondary" }, `by ${caption.author}`),
            listingParagraphs(listing).map((paragraph) => m("p", { class: "type-body mt-4 text-primary" }, paragraph)),
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
                href: entry.repository_url,
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
                  extra: primaryAction.marker,
                  onclick: () => onStartWithText(primaryAction.message),
                },
                primaryAction.label,
              ),
              m(
                Button,
                { extra: secondaryAction.marker, onclick: () => onStartWithText(secondaryAction.message) },
                secondaryAction.label,
              ),
            ]),
          ]),
        ]),
      ]);
    },
  };
}
