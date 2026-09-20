/**
 * The frontend's layers (desktop-interface plan section 6.1), lowest first: theme, model,
 * geometry, reducers, store, pages, gestures, views, and the wiring at the root. A module
 * imports only from its own layer or a lower one; the root-level boundary modules (``relay.ts``,
 * ``reload.ts``) and the shared library are below every layer. Walks the relative imports of
 * every non-test source, the way ``lint-and-format.test.ts`` registers the other code checks.
 */
import { readFileSync, readdirSync, statSync } from "fs";
import { join, relative, resolve } from "path";
import { describe, expect, it } from "vitest";

const SRC = new URL(".", import.meta.url).pathname;

/** The layers in order; a module's layer is the first path segment under ``src/``. */
const LAYERS: readonly string[] = ["theme", "model", "geometry", "reducers", "store", "pages", "gestures", "views"];

// Root-level modules every layer may import: the message boundary and the interface reload.
const FOUNDATION_MODULES: ReadonlySet<string> = new Set(["relay.ts", "reload.ts"]);
// The wiring at the root, above every layer.
const ROOT_MODULES: ReadonlySet<string> = new Set(["index.ts"]);

const RELATIVE_IMPORT = /(?<![\w.])(?:from|import)\s*\(?\s*["'](\.{1,2}\/[^"']+)["']/g;

function sourceFiles(directory: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...sourceFiles(path));
    else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts") && !entry.endsWith(".d.ts")) files.push(path);
  }
  return files;
}

/** The layer index of a source path relative to ``src/``: -1 for the foundation, LAYERS.length for the root. */
function layerOf(relativePath: string): number {
  if (FOUNDATION_MODULES.has(relativePath)) return -1;
  if (ROOT_MODULES.has(relativePath)) return LAYERS.length;
  const segment = relativePath.split("/")[0];
  const index = LAYERS.indexOf(segment);
  if (index < 0) throw new Error(`${relativePath} is in no layer: put it under one of ${LAYERS.join(", ")}`);
  return index;
}

function importsOf(path: string): string[] {
  const source = readFileSync(path, "utf-8");
  const imports: string[] = [];
  for (const match of source.matchAll(RELATIVE_IMPORT)) imports.push(match[1]);
  return imports;
}

describe("the frontend's layers", () => {
  it("import only downward", () => {
    const violations: string[] = [];
    for (const path of sourceFiles(SRC)) {
      const relativePath = relative(SRC, path);
      // The test helpers stand outside the layering: they are imported by tests alone.
      if (relativePath.startsWith("testing/")) continue;
      const layer = layerOf(relativePath);
      for (const specifier of importsOf(path)) {
        // A stylesheet import (the root's style.css) is not a module of any layer.
        if (specifier.endsWith(".css")) continue;
        const target = `${relative(SRC, resolve(path, "..", specifier))}.ts`;
        if (target.startsWith("testing/")) {
          violations.push(`${relativePath} imports a test helper: ${specifier}`);
          continue;
        }
        const targetLayer = layerOf(target);
        if (targetLayer > layer) violations.push(`${relativePath} (${LAYERS[layer] ?? "root"}) imports ${target} (${LAYERS[targetLayer] ?? "root"})`);
      }
    }
    expect(violations).toEqual([]);
  });

  it("puts every source module in a layer", () => {
    for (const path of sourceFiles(SRC)) {
      const relativePath = relative(SRC, path);
      if (relativePath.startsWith("testing/")) continue;
      expect(() => layerOf(relativePath), relativePath).not.toThrow();
    }
  });
});
