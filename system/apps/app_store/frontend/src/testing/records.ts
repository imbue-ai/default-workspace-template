/**
 * Record factories for the App Store tests: a store app, an app the workspace has, and the store's state. Each
 * takes overrides so a test spells only what it is about.
 */

import type { PendingRepoAdd, StoreApp, StoreSnapshot, WorkspaceApp } from "../models/Store";

/** A store app that fits and was never installed. */
export function storeAppRecord(slug: string, overrides: Partial<StoreApp> = {}): StoreApp {
  const title = slug.charAt(0).toUpperCase() + slug.slice(1);
  return {
    slug,
    is_added: false,
    added_with: null,
    build_disk_mb: null,
    build_memory_mb: null,
    resources_basis: "Measured on a workspace.",
    peak_memory_mb: null,
    tags: ["data"],
    title,
    description: `What ${title} does.`,
    about: `${title} in a paragraph.`,
    author: "Someone",
    license: "MIT",
    homepage_url: `https://example.test/${slug}`,
    source_url: `https://github.com/someone/${slug}`,
    version: "1.0.0",
    source_label: "Python package",
    package_spec: `${slug}==1.0.0`,
    icon_url: `/api/apps/${slug}/icon.svg`,
    disk_mb: 100,
    memory_idle_mb: 30,
    memory_in_use_mb: 50,
    memory_now_mb: null,
    verdict: "FITS",
    blocked_reason: "",
    status: null,
    job: null,
    failure_reason: "",
    measured_disk_mb: null,
    app_url: null,
    has_log: false,
    installed_version: null,
    update_kind: null,
    update_version: null,
    ...overrides,
  };
}

/** A built-in app with an icon. */
export function workspaceAppRecord(name: string, overrides: Partial<WorkspaceApp> = {}): WorkspaceApp {
  return {
    name,
    display_name: name.charAt(0).toUpperCase() + name.slice(1),
    description: `The ${name} app.`,
    icon_svg: "<svg/>",
    url: `http://localhost:9${name.length}00`,
    origin: "BUILT_IN",
    ...overrides,
  };
}

export function snapshotRecord(overrides: Partial<StoreSnapshot> = {}): StoreSnapshot {
  return {
    room: { disk_free_mb: 30_000, memory_available_mb: 4_000 },
    categories: [
      { key: "data", title: "Data and notebooks" },
      { key: "automation", title: "Automation" },
    ],
    apps: [],
    workspace_apps: [],
    busy_with: null,
    pending_repo_adds: [],
    ...overrides,
  };
}

export function pendingRepoAddRecord(id: string, overrides: Partial<PendingRepoAdd> = {}): PendingRepoAdd {
  return {
    id,
    slug: "datasettelatest",
    title: "Datasette Latest",
    description: "Explore data.",
    about: "Datasette explores data.",
    repository: "simonw/datasette",
    repository_url: "https://github.com/simonw/datasette",
    commit: "d1cc495a966cafaed3e50dad73cd88e790031704",
    commit_url: "https://github.com/simonw/datasette/commit/d1cc495a966cafaed3e50dad73cd88e790031704",
    source_label: "Python package from GitHub",
    install_commands: ["uv venv /env", "uv pip install git+https://github.com/simonw/datasette@d1cc"],
    start_command: "/env/bin/datasette serve --host 127.0.0.1 --port 8090",
    disk_mb: 30,
    memory_in_use_mb: 90,
    build_disk_mb: null,
    build_memory_mb: null,
    basis: "Installed in 5 seconds.",
    verdict: "FITS",
    blocked_reason: "",
    chat_id: "",
    submitted_at: "2026-11-05T00:00:00+00:00",
    replaces: "",
    installed_version: "",
    version: "d1cc495a966c",
    screenshot_url: null,
    ...overrides,
  };
}
