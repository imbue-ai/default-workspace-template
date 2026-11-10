// @vitest-environment jsdom
// Pins the element menu's bridge to the Imbue Studio desktop app against the vendored page-menu-bridge.json, the
// contract the app's Electron main reads the bridge by: the global's name, the sections it places, and the bounds
// past which it drops a row. A row the app drops would vanish from the menu with nothing failing. The vendor
// directory is fetched by the npm pre-build/pre-test hooks; a checkout that never ran them skips rather than fails.
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import {
  HOST_MENU_GLOBAL,
  installElementContextMenu,
  type HostMenuBridge,
  type HostMenuItemSections,
} from "./context_menu";

const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../vendor/mngr-assets/apps/minds/electron/page-menu-bridge.json",
);

interface PageMenuBridgeContract {
  global: string;
  sections: string[];
  max_rows_per_section: number;
  max_label_length: number;
  symbol_pattern: string;
}

const HOST_DRAWN_HANDSHAKE = {
  clientId: "client-1",
  windowId: "win-1",
  desktopId: "home",
  app: "docs",
  drawsContextMenus: true,
};

let uninstall: (() => void) | null = null;

afterEach(() => {
  uninstall?.();
  uninstall = null;
  document.body.innerHTML = "";
});

describe.skipIf(!existsSync(FIXTURE_PATH))("the desktop app's page menu bridge contract", () => {
  const contract = JSON.parse(readFileSync(FIXTURE_PATH, "utf-8")) as PageMenuBridgeContract;

  it("installs the bridge under the global the desktop app reads", () => {
    uninstall = installElementContextMenu({
      connection: { hasShell: () => true, draftText: () => undefined },
      handshake: () => HOST_DRAWN_HANDSHAKE,
    });
    expect(HOST_MENU_GLOBAL).toBe(contract.global);
    expect(Reflect.has(window, contract.global)).toBe(true);
  });

  it("answers a right-click with the sections the desktop app places, every row within its bounds", () => {
    document.body.innerHTML =
      '<a id="docs" href="https://example.com/docs"><img id="pic" src="https://example.com/a.png"></a>';
    uninstall = installElementContextMenu({
      connection: { hasShell: () => false, draftText: () => undefined },
      handshake: () => HOST_DRAWN_HANDSHAKE,
      extraRows: () => [
        { kind: "action", key: "rename", label: "Rename", symbol: "pencil.line", onSelect: () => undefined },
        { kind: "divider" },
        {
          kind: "action",
          key: "delete",
          label: "Delete",
          isDisabled: true,
          tooltip: "Read-only",
          onSelect: () => undefined,
        },
      ],
    });
    document.getElementById("pic")!.dispatchEvent(new MouseEvent("contextmenu", { bubbles: true, cancelable: true }));
    const answer = (Reflect.get(window, contract.global) as HostMenuBridge).sections();
    expect(answer).not.toBeNull();
    const sections = answer as HostMenuItemSections;
    expect(Object.keys(sections).sort()).toEqual([...contract.sections].sort());
    const symbolPattern = new RegExp(contract.symbol_pattern);
    for (const name of contract.sections) {
      const items = sections[name as keyof HostMenuItemSections];
      expect(items.length, name).toBeGreaterThan(0);
      expect(items.length, name).toBeLessThanOrEqual(contract.max_rows_per_section);
      for (const item of items) {
        if (item.kind === "divider") continue;
        expect(item.key, name).not.toBe("");
        expect(item.label.trim(), item.key).not.toBe("");
        expect(item.label.length, item.key).toBeLessThanOrEqual(contract.max_label_length);
        if (item.symbol !== null) expect(item.symbol, item.key).toMatch(symbolPattern);
      }
    }
  });
});
