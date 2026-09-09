/**
 * Record factories for the frontend tests: an app as the inventory lists it, one of its
 * instances, a project, a published template as the catalog lists it, and the update
 * notice as the shell sends it. Each takes overrides so a test spells only what it is
 * about.
 */

import type { AppRecord, InstanceRecord, ProjectInfo } from "../models/Inventory";
import type { CatalogTemplate } from "../models/TemplateCatalog";
import type { UpdateNoticeWire } from "../models/UpdateNotice";

function capitalized(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** A running, non-critical app with instances, one ``new`` action, and no instances listed yet. */
export function appRecord(name: string, overrides: Partial<AppRecord> = {}): AppRecord {
  return {
    name,
    display_name: capitalized(name),
    icon: "",
    label: "",
    url: `http://127.0.0.1:1/${name}`,
    internal: false,
    program: name,
    critical: false,
    instances_url: "",
    has_instances: true,
    actions: [{ id: "new", label: `New ${name}`, params: [] }],
    default_shortcut: null,
    launcher_rank: null,
    is_running: true,
    is_listed: true,
    instances: [],
    ...overrides,
  };
}

/** An idle, renameable, referenced instance at the app's root. */
export function instanceRecord(overrides: Partial<InstanceRecord> = {}): InstanceRecord {
  return {
    key: "terminal-1",
    url: "/",
    title: "Terminal 1",
    status: "idle",
    lifetime: "referenced",
    last_active: null,
    renameable: true,
    stoppable: false,
    ...overrides,
  };
}

/** An empty project named after its id. */
export function projectRecord(id: string, overrides: Partial<ProjectInfo> = {}): ProjectInfo {
  return {
    id,
    name: capitalized(id),
    color: "#123456",
    glyph: 0,
    tabs: [],
    shortcuts: [],
    ...overrides,
  };
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

/** An open notice (no rollback started) for an apply that touched the chat alone. */
export function noticeWire(overrides: Partial<UpdateNoticeWire> = {}): UpdateNoticeWire {
  return {
    merge_sha: "abc1234abc1234abc1234abc1234abc1234abc12",
    applied_at: 1_780_000_000,
    driven_by: "mngr/update-widgets",
    apps: ["chat"],
    programs: ["chat"],
    needs_services_restart: false,
    progress: null,
    outcome: null,
    ...overrides,
  };
}
