/**
 * The shell marks a theme's parts (docs/system/blueprint/workspace-themes/, section 4.2) only through the helpers in
 * `@imbue/workspace-ui/src/themes/parts.ts`, which type the part's name against the contract's list: a raw
 * `data-part` attribute can name a part the contract does not have, and no check would catch it. Walks every
 * non-test source.
 */
import { readFileSync, readdirSync, statSync } from "fs";
import { join, relative } from "path";
import { describe, expect, it } from "vitest";

const SRC = new URL(".", import.meta.url).pathname;

/** `"data-part":` in an attrs object, or `data-part=` in markup written as a string. */
const RAW_PART_MARKER = /["']data-part["']\s*:|data-part=/;

function sourceFiles(directory: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...sourceFiles(path));
    else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts")) files.push(path);
  }
  return files;
}

describe("the shell's part markers", () => {
  it("come from the parts helpers, never a raw data-part attribute", () => {
    const violations: string[] = [];
    for (const path of sourceFiles(SRC)) {
      readFileSync(path, "utf-8")
        .split("\n")
        .forEach((line, index) => {
          if (RAW_PART_MARKER.test(line)) violations.push(`${relative(SRC, path)}:${index + 1}: ${line.trim()}`);
        });
    }
    expect(violations).toEqual([]);
  });
});
