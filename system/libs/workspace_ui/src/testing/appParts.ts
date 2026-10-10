/**
 * For an app in `parts` theming mode (docs/system/blueprint/workspace-themes/, section 7): the parts its manifest
 * declares that no frontend source marks. A theme styles a declared part by `data-part="<app>.<name>"`, so a part
 * declared and never marked is one a theme's rules can never reach. Reads the manifest and the sources from disk,
 * for a test.
 */
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";

/** The app's name and the part names its `[theming]` table declares. */
export function declaredParts(appTomlPath: string): { readonly app: string; readonly parts: readonly string[] } {
  const manifest = readFileSync(appTomlPath, "utf8");
  const app = /^name\s*=\s*"([^"]+)"/m.exec(manifest)?.[1];
  if (app === undefined) throw new Error(`${appTomlPath} names no app`);
  // The table runs from its header to the next table header.
  const theming = /^\[theming\]\s*$([\s\S]*?)(?=^\[)/m.exec(`${manifest}\n[`)?.[1] ?? "";
  const parts = [...theming.matchAll(/\bname\s*=\s*"([^"]+)"/g)].map((match) => match[1]);
  return { app, parts };
}

function sourceFiles(directory: string): string[] {
  const files: string[] = [];
  for (const entry of readdirSync(directory)) {
    const path = join(directory, entry);
    if (statSync(path).isDirectory()) files.push(...sourceFiles(path));
    else if (entry.endsWith(".ts") && !entry.endsWith(".test.ts")) files.push(path);
  }
  return files;
}

/** The declared parts no source under `sourceDirectory` marks with `appPartAttrs("<app>", "<name>")`. */
export function unmarkedDeclaredParts(appTomlPath: string, sourceDirectory: string): string[] {
  const { app, parts } = declaredParts(appTomlPath);
  const sources = sourceFiles(sourceDirectory).map((path) => readFileSync(path, "utf8"));
  return parts.filter((part) => !sources.some((source) => source.includes(`appPartAttrs("${app}", "${part}")`)));
}
