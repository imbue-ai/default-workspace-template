#!/usr/bin/env node
// Vendor a CSS library into a theme folder, adapted to the theme contract
// (docs/system/blueprint/workspace-themes/plan-workspace-themes.md, section 4.5).
//
// Usage (from system/, after `npm ci`):
//
//   node libs/workspace_themes/scripts/vendor-css-library.mjs themes/<id>/vendor/<library>.vendor.json
//
// A library styles a whole page with its own class names (system.css's `.window`, win95.css's Bootstrap
// `.card`). The interface never carries those names, so this script rewrites the library onto the
// contract's parts: each class the config's `selectorMap` names becomes its part selector (`.btn` becomes
// `[data-part="button"]`), and every selector that names a class the map does not is dropped, since it could
// only match markup the interface does not have. Element selectors (scrollbars, `input`, `select`) stay.
//
// Along the way it applies the config's adaptations, each with its reason in the config: `droppedRules`
// and `droppedDeclarations` (matched against the library's own selectors, or an at-rule's `@name`), `fontFamilies` (renaming a
// library face so it never stands in for an installed font of the same name), and `colors`. It copies every
// file a url() names into `assets/` beside the output and points the url() at the copy.
//
// The sources are pinned and checked by sha256: an npm tarball (`"unpack": "npm-tarball"`) or plain files.

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join, resolve } from "node:path";
import postcss from "postcss";

const CLASS_PATTERN = /\.(-?[_a-zA-Z][\w-]*)/g;

function loadConfig(configPath) {
  const config = JSON.parse(readFileSync(configPath, "utf8"));
  for (const key of ["source", "files", "css", "licenseFile", "output", "selectorMap"]) {
    if (config[key] === undefined) throw new Error(`${configPath} has no "${key}"`);
  }
  return {
    unpack: "files",
    droppedRules: [],
    droppedDeclarations: [],
    fontFamilies: {},
    colors: [],
    ...config,
    droppedRulePatterns: (config.droppedRules ?? []).map((rule) => new RegExp(rule.pattern)),
    droppedDeclarationPatterns: (config.droppedDeclarations ?? []).map((rule) => ({
      selector: new RegExp(rule.selector),
      property: new RegExp(rule.property),
    })),
  };
}

async function download(config, downloadDir) {
  for (const [fileName, { url, sha256 }] of Object.entries(config.files)) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`GET ${url} failed with HTTP ${response.status}`);
    const bytes = Buffer.from(await response.arrayBuffer());
    const digest = createHash("sha256").update(bytes).digest("hex");
    if (digest !== sha256) throw new Error(`${url} has sha256 ${digest}, expected ${sha256}`);
    writeFileSync(join(downloadDir, fileName), bytes);
    if (config.unpack === "npm-tarball" && fileName.endsWith(".tgz")) {
      execFileSync("tar", ["-xzf", join(downloadDir, fileName), "-C", downloadDir]);
    }
  }
}

/** Where the attribute selector or string starting at `start` ends, just past its closing character. */
function quotedEnd(selector, start) {
  const closer = selector[start] === "[" ? "]" : selector[start];
  let quote = null;
  for (let index = start + 1; index < selector.length; index += 1) {
    const char = selector[index];
    if (char === "\\") {
      index += 1;
    } else if (quote !== null) {
      if (char === quote) quote = null;
    } else if (closer === "]" && (char === '"' || char === "'")) {
      quote = char;
    } else if (char === closer) {
      return index + 1;
    }
  }
  return selector.length;
}

/**
 * The selector in pieces, each marked whether a class can be named in it: an attribute selector or a string
 * (`[href$=".pdf"]`) holds text that only looks like one.
 */
function selectorPieces(selector) {
  const pieces = [];
  let start = 0;
  let index = 0;
  while (index < selector.length) {
    const char = selector[index];
    if (char === "\\") {
      index += 2;
    } else if (char === "[" || char === '"' || char === "'") {
      const end = quotedEnd(selector, index);
      pieces.push({ text: selector.slice(start, index), canNameClass: true });
      pieces.push({ text: selector.slice(index, end), canNameClass: false });
      start = end;
      index = end;
    } else {
      index += 1;
    }
  }
  pieces.push({ text: selector.slice(start), canNameClass: true });
  return pieces;
}

/** A library selector on the contract's parts, or null when it names a class the map does not. */
function mapSelector(selector, selectorMap) {
  let isMappable = true;
  const mapped = selectorPieces(selector)
    .map(({ text, canNameClass }) =>
      canNameClass
        ? text.replace(CLASS_PATTERN, (match, className) => {
            const part = selectorMap[className];
            if (part === undefined) {
              isMappable = false;
              return match;
            }
            return part;
          })
        : text,
    )
    .join("");
  return isMappable ? mapped : null;
}

function renameFontFamilies(value, fontFamilies) {
  let renamed = value;
  for (const [from, to] of Object.entries(fontFamilies)) {
    const escaped = from.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    renamed = renamed.replace(new RegExp(`(^|[\\s,])["']?${escaped}["']?(?=$|[\\s,])`, "g"), `$1"${to}"`);
  }
  return renamed;
}

function recolor(value, colors) {
  let recolored = value;
  for (const [from, to] of colors) {
    recolored = from.startsWith("#")
      ? recolored.replace(new RegExp(`${from}(?![0-9a-f])`, "gi"), to)
      : recolored.replace(new RegExp(`(^|[\\s,(:])${from}(?=$|[\\s,);])`, "g"), `$1${to}`);
  }
  return recolored;
}

/**
 * Copy every file a url() names into assets/ beside the output, and point the url() at the copy. `copied` maps
 * each copy's file name to the library file it came from; two library files with one name are refused rather
 * than merged into one copy.
 */
function relocateUrls(value, sourceDir, assetDir, copied) {
  return value.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (match, _quote, target) => {
    if (target.startsWith("data:") || target.startsWith("#")) return match;
    const sourcePath = resolve(join(sourceDir, target.split(/[?#]/)[0]));
    const fileName = basename(sourcePath);
    const copiedFrom = copied.get(fileName);
    if (copiedFrom === undefined) {
      writeFileSync(join(assetDir, fileName), readFileSync(sourcePath));
      copied.set(fileName, sourcePath);
    } else if (copiedFrom !== sourcePath) {
      throw new Error(
        `the library's ${copiedFrom} and ${sourcePath} would both be copied to assets/${fileName}; ` +
          "the script keeps one assets/ folder with no subfolders, so one of them must be renamed or dropped",
      );
    }
    return `url("assets/${fileName}")`;
  });
}

function adapt(root, config, sourceDir, assetDir) {
  const copied = new Map();
  root.walkComments((comment) => comment.remove());
  // An at-rule is dropped by its `@name` (`^@font-face$`), before any url() it holds is copied into assets/.
  root.walkAtRules((atRule) => {
    if (config.droppedRulePatterns.some((pattern) => pattern.test(`@${atRule.name}`))) atRule.remove();
  });
  root.walkRules((rule) => {
    if (rule.parent?.type === "atrule" && /keyframes$/.test(rule.parent.name)) return;
    const kept = rule.selectors
      .map((selector) => selector.trim())
      .filter((selector) => !config.droppedRulePatterns.some((pattern) => pattern.test(selector)));
    rule.walkDecls((decl) => {
      const isDropped = kept.some((selector) =>
        config.droppedDeclarationPatterns.some(
          (drop) => drop.selector.test(selector) && drop.property.test(decl.prop),
        ),
      );
      if (isDropped) decl.remove();
    });
    const mapped = kept.map((selector) => mapSelector(selector, config.selectorMap)).filter((s) => s !== null);
    if (mapped.length === 0 || rule.nodes.length === 0) {
      rule.remove();
      return;
    }
    rule.selectors = mapped;
  });
  root.walkAtRules((atRule) => {
    if (atRule.nodes !== undefined && atRule.nodes.length === 0) atRule.remove();
  });
  root.walkDecls((decl) => {
    let value = decl.value;
    if (decl.prop === "font-family") value = renameFontFamilies(value, config.fontFamilies);
    // The legacy `-webkit-linear-gradient(left, ...)` is turned top-to-bottom by modern CSS optimizers;
    // this is the standard spelling it means.
    value = value.replace(/-webkit-linear-gradient\(\s*left\s*,/g, "linear-gradient(to right,");
    value = recolor(value, config.colors);
    value = relocateUrls(value, sourceDir, assetDir, copied);
    decl.value = value;
  });
  return copied.size;
}

async function vendor(configPath) {
  const config = loadConfig(configPath);
  const vendorDir = dirname(configPath);
  const assetDir = join(vendorDir, "assets");
  const downloadDir = mkdtempSync(join(tmpdir(), "vendor-css-library-"));
  try {
    await download(config, downloadDir);
    const css = join(downloadDir, config.css);
    rmSync(assetDir, { recursive: true, force: true });
    mkdirSync(assetDir, { recursive: true });
    const root = postcss.parse(readFileSync(css, "utf8"), { from: css });
    const assetCount = adapt(root, config, dirname(css), assetDir);
    const header =
      `/* GENERATED from ${config.source} (${config.license}; see LICENSE-${basename(config.output, ".css")} ` +
      `beside this file) by system/libs/workspace_themes/scripts/vendor-css-library.mjs with ` +
      `${basename(configPath)}. Do not edit by hand: change the config and rerun the script. */\n`;
    writeFileSync(join(vendorDir, config.output), header + root.toString().trim() + "\n");
    writeFileSync(
      join(vendorDir, `LICENSE-${basename(config.output, ".css")}`),
      readFileSync(join(downloadDir, config.licenseFile)),
    );
    console.log(`${config.output}: ${config.source}, ${assetCount} asset(s)`);
  } finally {
    rmSync(downloadDir, { recursive: true, force: true });
  }
}

const configPaths = process.argv.slice(2);
if (configPaths.length === 0) {
  console.error("usage: vendor-css-library.mjs <theme>/vendor/<library>.vendor.json [...]");
  process.exit(2);
}
for (const configPath of configPaths) await vendor(resolve(configPath));
