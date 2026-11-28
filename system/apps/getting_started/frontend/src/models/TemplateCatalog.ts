/**
 * The template catalog behind the page's "Start from a template" section, as the app serves it
 * at ``GET /api/templates-catalog`` (see ``src/getting_started/template_catalog.py`` and
 * ``catalog/README.md`` for the document itself).
 *
 * One fetch per page load: the page shows "Loading templates..." until the fetch answers, the
 * shelves when it does, and "Failed to load templates." when the app has nothing to give (a failed
 * fetch is retried the next time the page mounts). An app with no catalog URL configured answers a
 * null catalog, and the page omits the section.
 *
 * A catalog lists published apps beside its templates, and a shelf names one as ``app:<name>``
 * among its template slugs. A card, a rail, and the detail page show either, so the page works in
 * listings: a template or an app, tagged with which it is.
 *
 * The pure helpers below (shelf resolution, the "All apps" and "All templates" rows, the search,
 * the write-up's paragraphs) are exported so they can be tested without a DOM or a socket.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";
import { matchesQuery } from "@imbue/workspace-ui/src/search";

export interface CatalogRequiredAccount {
  scope: string;
  permission: string;
}

export interface CatalogTemplateChoice {
  summary: string;
  resolution: string;
}

/** One published template as the catalog lists it, its drawing already resolved to a URL. */
export interface CatalogTemplate {
  slug: string;
  title: string;
  description: string;
  what_it_is: string;
  author: string;
  repository_url: string;
  /** Absolute URL of the drawing, or "" when the template has none. */
  thumbnail_url: string;
  version: string;
  updated_at: string;
  required_accounts: CatalogRequiredAccount[];
  required_secrets: string[];
  needs_ai: boolean;
  apt_packages: string[];
  choices: CatalogTemplateChoice[];
}

/** One published app as the catalog lists it, its icon already resolved to a URL. */
export interface CatalogApp {
  name: string;
  title: string;
  description: string;
  author: string;
  repository_url: string;
  /** Absolute URL of the app's icon, or "" when the catalog has none for it. */
  icon_url: string;
  version: string;
  required_accounts: CatalogRequiredAccount[];
  required_secrets: string[];
  needs_ai: boolean;
  apt_packages: string[];
}

/** A browsing row as the catalog spells it: a template's slug, or an app's name behind ``app:``. */
export interface CatalogShelf {
  key: string;
  title: string;
  slugs: string[];
}

export interface TemplateCatalog {
  generated_at: string;
  templates: CatalogTemplate[];
  apps: CatalogApp[];
  shelves: CatalogShelf[];
}

/** What a card, a rail, and the detail page show: a template or an app. */
export type CatalogListing = { kind: "template"; template: CatalogTemplate } | { kind: "app"; app: CatalogApp };

/** A row with its entries resolved, ready to render. */
export interface ResolvedShelf {
  key: string;
  title: string;
  listings: CatalogListing[];
}

export type TemplateCatalogState =
  | { kind: "loading" }
  | { kind: "loaded"; catalog: TemplateCatalog; isStale: boolean }
  | { kind: "failed" }
  | { kind: "disabled" };

export const TEMPLATES_CATALOG_PATH = "/api/templates-catalog";

/** The synthesized last row: every template the catalog carries. */
export const ALL_TEMPLATES_SHELF_KEY = "all";
const ALL_TEMPLATES_SHELF_TITLE = "All templates";

/** The synthesized row before it, when the catalog lists any app: every app it carries. */
export const ALL_APPS_SHELF_KEY = "all-apps";
const ALL_APPS_SHELF_TITLE = "All apps";

/** What a shelf entry naming an app starts with; the app's name follows. A template's slug holds no colon. */
export const APP_SHELF_ENTRY_PREFIX = "app:";

/** The catalog as the route answers it; a server older than this page answers no ``apps``. */
export interface CatalogWire extends Omit<TemplateCatalog, "apps"> {
  apps?: CatalogApp[];
}

interface CatalogResponse {
  catalog: CatalogWire | null;
  is_stale?: boolean;
}

// pure helpers
export function templateListing(template: CatalogTemplate): CatalogListing {
  return { kind: "template", template };
}

export function appListing(app: CatalogApp): CatalogListing {
  return { kind: "app", app };
}

/** A listing's identity within the catalog, spelled as a shelf names it: a template's slug, or ``app:<name>``. */
export function listingKey(listing: CatalogListing): string {
  return listing.kind === "template" ? listing.template.slug : `${APP_SHELF_ENTRY_PREFIX}${listing.app.name}`;
}

/**
 * The catalog's shelves with their entries resolved (an entry that names no template and no app
 * the catalog carries is dropped, and a row left empty by that is dropped with it), followed by
 * the "All apps" row when there are apps and the "All templates" row.
 */
export function resolveShelves(catalog: TemplateCatalog): ResolvedShelf[] {
  const allListings = [...catalog.apps.map(appListing), ...catalog.templates.map(templateListing)];
  const listingByKey = new Map(allListings.map((listing) => [listingKey(listing), listing]));
  const shelves: ResolvedShelf[] = [];
  for (const shelf of catalog.shelves) {
    const listings = shelf.slugs.flatMap((entry) => {
      const listing = listingByKey.get(entry);
      return listing === undefined ? [] : [listing];
    });
    if (listings.length > 0) shelves.push({ key: shelf.key, title: shelf.title, listings });
  }
  if (catalog.apps.length > 0) {
    shelves.push({ key: ALL_APPS_SHELF_KEY, title: ALL_APPS_SHELF_TITLE, listings: catalog.apps.map(appListing) });
  }
  shelves.push({
    key: ALL_TEMPLATES_SHELF_KEY,
    title: ALL_TEMPLATES_SHELF_TITLE,
    listings: catalog.templates.map(templateListing),
  });
  return shelves;
}

/** The templates a query finds: by title, description, or author. */
export function searchTemplates(templates: readonly CatalogTemplate[], query: string): CatalogTemplate[] {
  return templates.filter((template) => matchesQuery(query, template.title, template.description, template.author));
}

/** Everything in the catalog a query finds, by title, description, or author: apps first, as on the shelves. */
export function searchListings(catalog: TemplateCatalog, query: string): CatalogListing[] {
  const apps = catalog.apps.filter((app) => matchesQuery(query, app.title, app.description, app.author));
  return [...apps.map(appListing), ...searchTemplates(catalog.templates, query).map(templateListing)];
}

/** The catalog a response carries, with the ``apps`` a server older than this page leaves out filled in. */
export function catalogFromWire(wire: CatalogWire): TemplateCatalog {
  return { ...wire, apps: wire.apps ?? [] };
}

/**
 * A template's write-up as paragraphs. The export hard-wraps its prose, so a lone newline is a wrap
 * and a blank line is a real break: the former is unwrapped, the latter splits.
 */
export function writeUpParagraphs(text: string): string[] {
  return text
    .split(/\n\s*\n/)
    .map((paragraph) => paragraph.replace(/\s+/g, " ").trim())
    .filter((paragraph) => paragraph !== "");
}

// the fetch, one per page load
let state: TemplateCatalogState = { kind: "loading" };
let isRequested = false;

export function getTemplateCatalogState(): TemplateCatalogState {
  return state;
}

/** Fetch the catalog once per page load; a failed fetch is tried again on the next call. */
export function ensureTemplateCatalogRequested(): void {
  if (isRequested && state.kind !== "failed") return;
  isRequested = true;
  state = { kind: "loading" };
  void fetchTemplateCatalog().then((next) => {
    state = next;
    m.redraw();
  });
}

async function fetchTemplateCatalog(): Promise<TemplateCatalogState> {
  try {
    const response = await fetch(apiUrl(TEMPLATES_CATALOG_PATH));
    if (!response.ok) {
      console.warn(`[getting-started] could not load the template catalog: HTTP ${response.status}`);
      return { kind: "failed" };
    }
    const data = (await response.json()) as CatalogResponse;
    if (data.catalog === null) return { kind: "disabled" };
    return { kind: "loaded", catalog: catalogFromWire(data.catalog), isStale: data.is_stale === true };
  } catch (e) {
    console.warn("[getting-started] could not load the template catalog", e);
    return { kind: "failed" };
  }
}
