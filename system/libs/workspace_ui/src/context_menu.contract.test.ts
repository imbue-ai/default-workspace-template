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
  type HostMenuItem,
  type HostMenuItemSections,
} from "./context_menu";

const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../vendor/mngr-assets/apps/minds/electron/page-menu-bridge.json",
);

interface PageMenuBridgeContract {
  global: string;
  sections: string[];
  row_kinds: string[];
  submenu_row_kinds: string[];
  max_rows_per_section: number;
  max_rows_per_submenu: number;
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

function readContract(): PageMenuBridgeContract {
  return JSON.parse(readFileSync(FIXTURE_PATH, "utf-8")) as PageMenuBridgeContract;
}

let uninstall: (() => void) | null = null;

afterEach(() => {
  uninstall?.();
  uninstall = null;
  document.body.innerHTML = "";
});

describe.skipIf(!existsSync(FIXTURE_PATH))("the desktop app's page menu bridge contract", () => {
  it("installs the bridge under the global the desktop app reads", () => {
    const contract = readContract();
    uninstall = installElementContextMenu({
      connection: { hasShell: () => true, draftText: () => undefined },
      handshake: () => HOST_DRAWN_HANDSHAKE,
    });
    expect(HOST_MENU_GLOBAL).toBe(contract.global);
    expect(Reflect.has(window, contract.global)).toBe(true);
  });

  it("answers a right-click with the sections the desktop app places, every row within its bounds", () => {
    const contract = readContract();
    document.body.innerHTML =
      '<a id="docs" href="https://example.com/docs"><img id="pic" src="https://example.com/a.png"></a>';
    uninstall = installElementContextMenu({
      connection: { hasShell: () => false, draftText: () => undefined },
      handshake: () => HOST_DRAWN_HANDSHAKE,
      extraRows: () => [
        { kind: "action", key: "rename", label: "Rename", symbol: "pencil.line", onSelect: () => undefined },
        { kind: "check", key: "pinned", label: "Pinned", isChecked: true, onToggle: () => undefined },
        {
          kind: "submenu",
          key: "move",
          label: "Move to",
          symbol: "folder",
          rows: () => [
            { kind: "action", key: "move-inbox", label: "Inbox", onSelect: () => undefined },
            { kind: "divider" },
            { kind: "check", key: "move-done", label: "Done", isChecked: false, onToggle: () => undefined },
          ],
        },
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
    const kindsSeen = new Set<string>();
    const expectWithinBounds = (items: HostMenuItem[], where: string, kinds: string[], maxRows: number): void => {
      expect(items.length, where).toBeGreaterThan(0);
      expect(items.length, where).toBeLessThanOrEqual(maxRows);
      for (const item of items) {
        kindsSeen.add(item.kind);
        expect(kinds, where).toContain(item.kind);
        if (item.kind === "divider") continue;
        expect(item.label.trim(), where).not.toBe("");
        expect(item.label.length, item.label).toBeLessThanOrEqual(contract.max_label_length);
        if (item.symbol !== null) expect(item.symbol, item.label).toMatch(symbolPattern);
        if (item.kind === "submenu") {
          expectWithinBounds(item.items, item.label, contract.submenu_row_kinds, contract.max_rows_per_submenu);
        } else {
          expect(item.key, item.label).not.toBe("");
        }
      }
    };
    for (const name of contract.sections) {
      expectWithinBounds(
        sections[name as keyof HostMenuItemSections],
        name,
        contract.row_kinds,
        contract.max_rows_per_section,
      );
    }
    expect([...kindsSeen].sort()).toEqual([...contract.row_kinds].sort());
  });
});
