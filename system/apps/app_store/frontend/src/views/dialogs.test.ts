// @vitest-environment jsdom
import "@imbue/workspace-ui/src/testing/dom";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import m from "mithril";

import { pendingRepoAddRecord, storeAppRecord } from "../testing/records";
import { UNREVIEWED_WARNING } from "./AddAppDialog";
import type { InstallDialogAttrs, RepoAddDialogAttrs, UninstallDialogAttrs } from "./dialogs";
import { InstallDialog, RepoAddDialog, THIRD_PARTY_WARNING, UninstallDialog, UpdateDialog } from "./dialogs";

let root: HTMLElement;

beforeEach(() => {
  root = document.createElement("div");
  document.body.appendChild(root);
});

afterEach(() => {
  m.mount(root, null);
  root.remove();
});

describe("InstallDialog", () => {
  function mountInstall(overrides: Partial<InstallDialogAttrs>): InstallDialogAttrs {
    const attrs: InstallDialogAttrs = {
      app: storeAppRecord("datasette", { license: "Apache-2.0" }),
      room: { disk_free_mb: 2048, memory_available_mb: 512 },
      error: null,
      isSubmitting: false,
      onCancel: vi.fn(),
      onConfirm: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(InstallDialog, attrs) });
    return attrs;
  }

  it("says what is installed, from where, its license, its cost against the room, and that it is third-party", () => {
    const attrs = mountInstall({});
    const dialog = root.querySelector<HTMLElement>("[data-dialog=install]")!;
    expect(dialog.textContent).toContain("Install Datasette?");
    expect(dialog.textContent).toContain("datasette==1.0.0");
    expect(dialog.querySelector("a")!.getAttribute("href")).toBe("https://github.com/someone/datasette");
    expect(dialog.textContent).toContain("Apache-2.0");
    expect(dialog.textContent).toContain("about 100 MB (2.0 GB free)");
    expect(dialog.textContent).toContain("about 50 MB in use, 30 MB idle (512 MB available)");
    expect(dialog.querySelector("[data-verdict]")!.textContent).toBe("Fits");
    expect(dialog.querySelector("[data-warning]")!.textContent).toBe(THIRD_PARTY_WARNING);
    root.querySelector<HTMLElement>(".app-store-confirm-install")!.click();
    expect(attrs.onConfirm).toHaveBeenCalledTimes(1);
  });

  it("shows the store's refusal and blocks an app that won't fit", () => {
    mountInstall({
      app: storeAppRecord("nodered", { verdict: "WONT_FIT", blocked_reason: "Not enough memory." }),
    });
    expect(root.querySelector("[data-refusal]")!.textContent).toBe("Not enough memory.");
    expect(root.querySelector<HTMLButtonElement>(".app-store-confirm-install")!.disabled).toBe(true);
  });

  it("shows a refusal that came back from the store", () => {
    mountInstall({ error: "Datasette is already installed or being worked on." });
    expect(root.querySelector("[data-refusal]")!.textContent).toBe(
      "Datasette is already installed or being worked on.",
    );
  });
});

describe("UninstallDialog", () => {
  function mountUninstall(): UninstallDialogAttrs {
    const attrs: UninstallDialogAttrs = {
      app: storeAppRecord("datasette", { status: "INSTALLED", measured_disk_mb: 21 }),
      error: null,
      isSubmitting: false,
      onCancel: vi.fn(),
      onConfirm: vi.fn(),
    };
    m.mount(root, { view: () => m(UninstallDialog, attrs) });
    return attrs;
  }

  it("keeps the app's files unless asked to delete them", () => {
    const attrs = mountUninstall();
    expect(root.textContent).toContain("freeing about 21 MB");
    root.querySelector<HTMLElement>(".app-store-confirm-uninstall")!.click();
    expect(attrs.onConfirm).toHaveBeenLastCalledWith(false);
    const checkbox = root.querySelector<HTMLInputElement>("[data-delete-data]")!;
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event("change"));
    root.querySelector<HTMLElement>(".app-store-confirm-uninstall")!.click();
    expect(attrs.onConfirm).toHaveBeenLastCalledWith(true);
  });
});

describe("RepoAddDialog", () => {
  const pending = pendingRepoAddRecord("abc");

  function mountRepoAdd(overrides: Partial<RepoAddDialogAttrs>): RepoAddDialogAttrs {
    const attrs: RepoAddDialogAttrs = {
      pending,
      room: { disk_free_mb: 2048, memory_available_mb: 512 },
      error: null,
      isSubmitting: false,
      onLater: vi.fn(),
      onDiscard: vi.fn(),
      onConfirm: vi.fn(),
      ...overrides,
    };
    m.mount(root, { view: () => m(RepoAddDialog, attrs) });
    return attrs;
  }

  it("shows where it comes from, the exact commands, its cost, and that nobody reviewed it", () => {
    const attrs = mountRepoAdd({});
    const dialog = root.querySelector<HTMLElement>("[data-dialog=repo-add]")!;
    expect(dialog.textContent).toContain("Add Datasette Latest to the store?");
    expect(dialog.querySelector("[data-commit-link]")!.getAttribute("href")).toBe(pending.commit_url);
    expect(dialog.querySelector("[data-commit-link]")!.textContent).toBe("simonw/datasette at d1cc495a966c");
    expect(dialog.querySelector("[data-install-commands]")!.textContent).toBe(pending.install_commands.join("\n"));
    expect(dialog.querySelector("[data-start-command]")!.textContent).toBe(pending.start_command);
    expect(dialog.textContent).toContain("about 90 MB in use (512 MB available)");
    expect(dialog.textContent).not.toContain("Building");
    expect(dialog.querySelector("[data-warning]")!.textContent).toBe(UNREVIEWED_WARNING);
    root.querySelector<HTMLElement>(".app-store-confirm-repo-add")!.click();
    expect(attrs.onConfirm).toHaveBeenCalledOnce();
    root.querySelector<HTMLElement>(".app-store-discard-repo-add")!.click();
    expect(attrs.onDiscard).toHaveBeenCalledOnce();
  });

  it("names what a build needs, and cannot be added when it will not fit", () => {
    mountRepoAdd({
      pending: { ...pending, build_memory_mb: 3000, verdict: "WONT_FIT", blocked_reason: "Not enough memory." },
    });
    const dialog = root.querySelector<HTMLElement>("[data-dialog=repo-add]")!;
    expect(dialog.textContent).toContain("needs about 2.9 GB of memory while it installs");
    expect(dialog.querySelector("[data-refusal]")!.textContent).toBe("Not enough memory.");
    expect(root.querySelector<HTMLButtonElement>(".app-store-confirm-repo-add")!.disabled).toBe(true);
  });
});

describe("RepoAddDialog for an update", () => {
  it("names the versions and updates instead of adding", () => {
    const onConfirm = vi.fn();
    const pending = pendingRepoAddRecord("upd", {
      replaces: "datasettelatest",
      installed_version: "d1cc495a966c",
      version: "abc123def456",
    });
    m.mount(root, {
      view: () =>
        m(RepoAddDialog, {
          pending,
          room: { disk_free_mb: 2048, memory_available_mb: 512 },
          error: null,
          isSubmitting: false,
          onLater: vi.fn(),
          onDiscard: vi.fn(),
          onConfirm,
        }),
    });
    const dialog = root.querySelector<HTMLElement>("[data-dialog=repo-add]")!;
    expect(dialog.textContent).toContain("Update Datasette Latest to abc123def456?");
    expect(dialog.textContent).toContain("you have d1cc495a966c");
    const button = root.querySelector<HTMLButtonElement>(".app-store-confirm-repo-add")!;
    expect(button.textContent).toBe("Update");
    button.click();
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});

describe("UpdateDialog", () => {
  it("says what changes and that the old version comes back if the new one fails", () => {
    const onConfirm = vi.fn();
    m.mount(root, {
      view: () =>
        m(UpdateDialog, {
          app: storeAppRecord("demo", { is_added: true, installed_version: "1.0" }),
          version: "1.1",
          error: null,
          isSubmitting: false,
          onCancel: vi.fn(),
          onConfirm,
        }),
    });
    const dialog = root.querySelector<HTMLElement>("[data-dialog=update]")!;
    expect(dialog.textContent).toContain("Update Demo to 1.1?");
    expect(dialog.textContent).toContain("goes from 1.0 to 1.1");
    expect(dialog.textContent).toContain("the version you have now is put back");
    expect(dialog.querySelector("[data-warning]")!.textContent).toBe(UNREVIEWED_WARNING);
    root.querySelector<HTMLElement>(".app-store-confirm-update")!.click();
    expect(onConfirm).toHaveBeenCalledOnce();
  });
});

describe("RepoAddDialog's screenshot", () => {
  function mountWith(screenshotUrl: string | null): void {
    m.mount(root, {
      view: () =>
        m(RepoAddDialog, {
          pending: pendingRepoAddRecord("abc", { screenshot_url: screenshotUrl }),
          room: { disk_free_mb: 2048, memory_available_mb: 512 },
          error: null,
          isSubmitting: false,
          onLater: vi.fn(),
          onDiscard: vi.fn(),
          onConfirm: vi.fn(),
        }),
    });
  }

  it("shows how the app looked when it was tried out", () => {
    mountWith("/api/repo-adds/abc/screenshot.png");
    const image = root.querySelector<HTMLImageElement>("[data-screenshot]")!;
    expect(image.getAttribute("src")).toContain("/api/repo-adds/abc/screenshot.png");
    expect(image.getAttribute("alt")).toBe("Datasette Latest running when it was tried out");
    expect(root.querySelector("[data-no-screenshot]")).toBeNull();
  });

  it("says so when there is no picture", () => {
    mountWith(null);
    expect(root.querySelector("[data-screenshot]")).toBeNull();
    expect(root.querySelector("[data-no-screenshot]")).not.toBeNull();
  });
});
