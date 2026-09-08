/**
 * The template catalog behind the New Tab page's "Start from a template" section, as the shell
 * serves it at ``GET /api/templates-catalog`` (see ``imbue/system_interface/template_catalog.py``
 * and ``catalog/README.md`` for the document itself).
 *
 * One fetch per page load, shared by every launcher panel: the page shows "Loading templates..."
 * until it answers, the shelves when it does, and "Failed to load templates." when the shell has
 * nothing to give (a failed fetch is retried the next time a launcher mounts). A shell with no
 * catalog URL configured answers a null catalog, and the page omits the section.
 *
 * The pure helpers below (shelf resolution, the "All templates" row, the search match) are
 * exported so they can be tested without a DOM or a socket.
 */

import m from "mithril";
import { apiUrl } from "@imbue/workspace-ui/src/base-path";

export interface CatalogRequiredAccount {
  service: string;
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

/** A browsing row as the catalog spells it: slugs, not templates. */
export interface CatalogShelf {
  key: string;
  title: string;
  slugs: string[];
}

export interface TemplateCatalog {
  generated_at: string;
  templates: CatalogTemplate[];
  shelves: CatalogShelf[];
}

/** A row with its templates resolved, ready to render. */
export interface ResolvedShelf {
  key: string;
  title: string;
  templates: CatalogTemplate[];
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

interface CatalogResponse {
  catalog: TemplateCatalog | null;
  is_stale?: boolean;
}

// ---------- pure helpers ----------

/**
 * The catalog's shelves with their slugs resolved (a slug no template carries is dropped, and a
 * row left empty by that is dropped with it), followed by the "All templates" row.
 */
export function resolveShelves(catalog: TemplateCatalog): ResolvedShelf[] {
  const templateBySlug = new Map(catalog.templates.map((template) => [template.slug, template]));
  const shelves: ResolvedShelf[] = [];
  for (const shelf of catalog.shelves) {
    const templates = shelf.slugs.flatMap((slug) => {
      const template = templateBySlug.get(slug);
      return template === undefined ? [] : [template];
    });
    if (templates.length > 0) shelves.push({ key: shelf.key, title: shelf.title, templates });
  }
  shelves.push({ key: ALL_TEMPLATES_SHELF_KEY, title: ALL_TEMPLATES_SHELF_TITLE, templates: [...catalog.templates] });
  return shelves;
}

/**
 * Whether every whitespace-separated token of ``query`` appears somewhere in ``fields``, case-
 * insensitively -- so "open term" finds "Open new terminal" and word order does not matter. Plain
 * substrings, no fuzz: at this catalog's size a near miss confuses more than the extra typing costs.
 */
export function matchesQuery(query: string, ...fields: readonly string[]): boolean {
  const haystack = fields.join(" ").toLowerCase();
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter((token) => token !== "")
    .every((token) => haystack.includes(token));
}

/** The templates a query finds: by title, description, or author. */
export function searchTemplates(templates: readonly CatalogTemplate[], query: string): CatalogTemplate[] {
  return templates.filter((template) => matchesQuery(query, template.title, template.description, template.author));
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

// ---------- the fetch, shared by every launcher panel ----------

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
      console.warn(`[si] could not load the template catalog: HTTP ${response.status}`);
      return { kind: "failed" };
    }
    const data = (await response.json()) as CatalogResponse;
    if (data.catalog === null) return { kind: "disabled" };
    return { kind: "loaded", catalog: data.catalog, isStale: data.is_stale === true };
  } catch (e) {
    console.warn("[si] could not load the template catalog", e);
    return { kind: "failed" };
  }
}

export function resetTemplateCatalogForTesting(): void {
  state = { kind: "loading" };
  isRequested = false;
}
