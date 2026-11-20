import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { WORKSPACE_ACTION_IDS } from "./keybindings";

const APP_TOML = join(__dirname, "../../../app.toml");

/** The ids of the manifest's `[[keybindings]]` tables declaring `scope = "workspace"`, in manifest order. */
function manifestWorkspaceActionIds(): string[] {
  const tables = readFileSync(APP_TOML, "utf8").split("[[keybindings]]").slice(1);
  return tables.flatMap((table) => {
    const id = /^id\s*=\s*"([^"]+)"/m.exec(table)?.[1];
    if (id === undefined) throw new Error(`${APP_TOML} has a keybinding without an id`);
    return /^scope\s*=\s*"workspace"/m.test(table) ? [id] : [];
  });
}

describe("WORKSPACE_ACTION_IDS", () => {
  it("are the shell manifest's workspace-wide actions, so each one the screen lists also runs", () => {
    expect([...WORKSPACE_ACTION_IDS]).toEqual(manifestWorkspaceActionIds());
  });
});
