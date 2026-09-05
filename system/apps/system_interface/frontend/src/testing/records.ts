/**
 * Record factories for the frontend tests: an app as the inventory lists it, one of its
 * instances, and a project. Each takes overrides so a test spells only what it is about.
 */

import type { AppRecord, InstanceRecord, ProjectInfo } from "../models/Inventory";

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
    actions: [{ id: "new", label: `New ${name}` }],
    default_shortcut: null,
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
