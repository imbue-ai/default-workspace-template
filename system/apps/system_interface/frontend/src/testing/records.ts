/**
 * Record factories for the frontend tests: an app as the shell lists it, a desktop, a window, a
 * placement, a launch path, and a published template as the catalog lists it. Each takes
 * overrides so a test spells only what it is about.
 */

import type { AppRecord, Desktop, LaunchPath, Placement, WindowRecord } from "../model/records";
import type { CatalogTemplate } from "../model/TemplateCatalog";
import { cascadeFrame } from "../geometry/frames";
import type { ThemeMetrics } from "../theme/metrics";

function capitalized(name: string): string {
  return name.charAt(0).toUpperCase() + name.slice(1);
}

/** A launch path ``new`` at ``/new`` with no params. */
export function launchPathRecord(overrides: Partial<LaunchPath> = {}): LaunchPath {
  return { id: "new", label: "New", path: "/new", params: [], ...overrides };
}

/** A running, non-critical, supervised app with one ``new`` launch path at ``/new``. */
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
    launch_paths: [launchPathRecord({ label: `New ${name}` })],
    default_shortcut: { launch: "new", mode: "focus" },
    launcher_rank: null,
    is_running: true,
    ...overrides,
  };
}

/** A settled window of ``app`` at ``path`` with an empty title. */
export function windowRecord(
  id: string,
  app: string,
  path: string,
  overrides: Partial<WindowRecord> = {},
): WindowRecord {
  return { id, app, path, title: "", opened_at: "2026-09-19T00:00:00Z", is_settling: false, ...overrides };
}

/** A shared desktop named after its id, with no wallpaper, shortcuts, or windows. */
export function desktopRecord(id: string, overrides: Partial<Desktop> = {}): Desktop {
  return {
    id,
    name: capitalized(id),
    color: "#2f6b4f",
    glyph: 0,
    sharing: "shared",
    wallpaper: null,
    shortcuts: [],
    windows: [],
    ...overrides,
  };
}

/** A shown, normal placement at the first cascade frame. */
export function placementRecord(windowId: string, overrides: Partial<Placement> = {}): Placement {
  return { window_id: windowId, frame: cascadeFrame(0), state: "NORMAL", is_minimized: false, ...overrides };
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

/** The theme metrics at the contract's default (non-compact) values. */
export function themeMetricsRecord(overrides: Partial<ThemeMetrics> = {}): ThemeMetrics {
  return {
    titleBarHeight: 36,
    taskbarHeight: 48,
    cellWidth: 96,
    cellHeight: 112,
    gridInset: 16,
    windowMinWidth: 320,
    windowMinHeight: 240,
    titleMinVisible: 120,
    snapThreshold: 16,
    unsnapDistance: 12,
    dragThreshold: 4,
    touchTarget: 32,
    ...overrides,
  };
}
