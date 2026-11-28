/**
 * Record factories for the Getting Started tests: a published template and a published app as the
 * catalog lists them. Each takes overrides so a test spells only what it is about.
 */

import type { CatalogApp, CatalogTemplate } from "../models/TemplateCatalog";

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

/** An app titled after its name, published by "someone" from a repository named after it, with an icon and no requirements. */
export function catalogAppRecord(name: string, overrides: Partial<CatalogApp> = {}): CatalogApp {
  return {
    name,
    title: capitalized(name),
    description: `What ${name} does.`,
    author: "someone",
    repository_url: `https://github.com/someone/${name}`,
    icon_url: `https://example.test/app-icons/${name}.svg`,
    version: "1.0.0",
    required_accounts: [],
    required_secrets: [],
    needs_ai: false,
    apt_packages: [],
    ...overrides,
  };
}
