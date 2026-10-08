/**
 * Record factories for the Getting Started tests: a published template as the catalog lists it, and a
 * source's record in the chat import state. Each spells only what a test is about.
 */

import type { ChatImportSource, ChatImportSourceState } from "../models/ChatImport";
import type { CatalogTemplate } from "../models/TemplateCatalog";

/** A chat import source in ``state`` holding ``conversations`` pages, with no timestamp or detail. */
export function chatImportSourceRecord(state: ChatImportSourceState, conversations = 0): ChatImportSource {
  return { state, conversations, updated_at: "", detail: "" };
}

function capitalized(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** A template titled after its slug, published by "someone" from a repository named after it, with a drawing and no requirements. */
export function catalogTemplateRecord(slug: string, overrides: Partial<CatalogTemplate> = {}): CatalogTemplate {
  return {
    slug,
    title: capitalized(slug),
    description: `What ${slug} does.`,
    what_it_is: "",
    author: "someone",
    repository_url: `https://github.com/someone/${slug}`,
    thumbnail_url: `https://example.test/${slug}.svg`,
    version: "v1",
    updated_at: "",
    required_accounts: [],
    required_secrets: [],
    needs_ai: false,
    apt_packages: [],
    choices: [],
    ...overrides,
  };
}
