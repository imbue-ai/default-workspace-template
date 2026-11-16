// @vitest-environment jsdom
// Pins the browser-side chord vocabulary against the vendored desktop contract fixture, the
// way the two Python mirrors are pinned: a named key added on either Python side must land
// here too, or the recorder and matcher would silently answer null for it. The vendor
// directory is fetched by the npm pre-build/pre-test hooks; a checkout that never ran them
// skips rather than fails, like the template's own snapshot contract test.
import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { chordFromKeydown } from "./keybindings";

// Resolved through node:path rather than new URL(): under the jsdom environment the DOM URL
// class resolves relative URLs against the page's origin, not this module's file URL.
const FIXTURE_PATH = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../../vendor/mngr-assets/apps/minds/imbue/minds/desktop_client/workspace_profile_contract.json",
);

describe.skipIf(!existsSync(FIXTURE_PATH))("the chord vocabulary contract", () => {
  it("spells every named key and modifier the fixture pins", () => {
    const contract = JSON.parse(readFileSync(FIXTURE_PATH, "utf-8")) as {
      chord_modifier_order: string[];
      chord_named_keys: string[];
    };

    expect(contract.chord_modifier_order).toEqual(["ctrl", "alt", "shift", "meta"]);
    for (const namedKey of contract.chord_named_keys) {
      // "plus" and "space" stand in for keys the separator and JSON make awkward; the
      // recorder reaches them from their KeyboardEvent spellings.
      const eventKey = namedKey === "plus" ? "+" : namedKey === "space" ? " " : namedKey;
      expect(chordFromKeydown(new KeyboardEvent("keydown", { key: eventKey })), namedKey).toBe(namedKey);
    }
  });
});
