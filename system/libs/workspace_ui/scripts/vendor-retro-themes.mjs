#!/usr/bin/env node
// Regenerate src/themes/vendor/ from the two retro CSS libraries the themes are built on:
// Classic Mac on system.css (https://sakofchit.github.io/system.css/) and Windows 2000 on win95.css
// (https://alexbsoft.github.io/win95.css/).
//
// Usage (from system/libs/workspace_ui, after `npm install` at system/):
//
//   node scripts/vendor-retro-themes.mjs
//
// Both libraries style whole pages: system.css bare elements and its own classes, win95.css the
// Bootstrap classes it themes. Loaded as they are, they would restyle the desktop under every theme,
// so this script rewrites each into a copy where every rule is scoped under its theme's
// `:root[data-ui-theme="..."]`, and copies the fonts and images it references beside it. The desktop
// then opts into a library's components by carrying its class names (`btn`, `card-header`,
// `dropdown-menu`, ...), which do nothing outside the theme.
//
// Along the way it adapts the libraries to the desktop (each theme's `droppedRules` and
// `droppedDeclarations` say what and why), renames their font families ("Retro Chicago", ...) so a
// library face can never stand in for an installed font of the same name, and recolors win95.css's
// 3D face from Windows 95 silver to the Windows 2000 face gray.
//
// The sources are pinned and checked: system.css by npm version and tarball digest, win95.css by
// commit and file digest (it is not published to npm).

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import postcss from "postcss";

const libraryDir = dirname(dirname(fileURLToPath(import.meta.url)));
const vendorDir = join(libraryDir, "src", "themes", "vendor");

const WIN95_COMMIT = "faff13b2788e9b44e92ac0d51433d2d878907f50";
const WIN95_RAW = `https://raw.githubusercontent.com/AlexBSoft/win95.css/${WIN95_COMMIT}`;

const THEMES = [
  {
    name: "mac-classic",
    source: "system.css 0.1.11",
    files: {
      "package.tgz": {
        url: "https://registry.npmjs.org/@sakun/system.css/-/system.css-0.1.11.tgz",
        sha256: "287604b3a1e2e757d9049ac5cfc6f3d8c952e0218bb2a73e57760f7535814034",
      },
    },
    unpack: (downloadDir) => {
      execFileSync("tar", ["-xzf", join(downloadDir, "package.tgz"), "-C", downloadDir]);
      return {
        css: join(downloadDir, "package", "dist", "system.css"),
        license: join(downloadDir, "package", "LICENSE"),
      };
    },
    fontFamilies: {
      Chicago: "Retro Chicago",
      Chicago_12: "Retro Chicago 12",
      Monaco: "Retro Monaco",
      Geneva_9: "Retro Geneva 9",
    },
    droppedRules: [],
    droppedDeclarations: [
      // 18px type and fixed widths sized for the library's demo page; the desktop sizes its own.
      {
        selector: /^(input|select|\.btn|\.btn-default|\.btn:disabled)$/,
        property: /^(font-size|width|min-width|min-height)$/,
      },
      // A 20px side padding would push the icon out of the desktop's square icon buttons.
      { selector: /^(\.btn|\.btn-default)$/, property: /^padding$/ },
      // A window is positioned by the desktop: no margin, and no floor under its own minimum size.
      { selector: /^\.window$/, property: /^(margin|min-width|font-family)$/ },
      // The title bar controls are the desktop's own size; the library's are 40px boxes drawn at half scale.
      { selector: /^\.title-bar button$/, property: /^(width|height|margin|transform|border)$/ },
      { selector: /^\.title-bar \.title$/, property: /^font-size$/ },
      { selector: /^\.title-bar$/, property: /^(height|margin)$/ },
    ],
    colors: [],
  },
  {
    name: "windows-2000",
    source: `win95.css ${WIN95_COMMIT.slice(0, 7)}`,
    files: {
      "win95.css": {
        url: `${WIN95_RAW}/assets/win95.css`,
        sha256: "fd5489c42eb855bb2970a2bdeb27096bca62f2e1e6e6fad358901c20b1b79ce4",
      },
      LICENSE: {
        url: `${WIN95_RAW}/license.txt`,
        sha256: "227db7da09e8ce4b9bd8192083b742aa47f3dfa2f11f2b547b3ec16ef40310ab",
      },
      "combo.png": {
        url: `${WIN95_RAW}/assets/combo.png`,
        sha256: "c30a70fc49aaf0b03271fd792c8fd49f730257218c3c80ee86dfcc51496a4477",
      },
      "comboup.png": {
        url: `${WIN95_RAW}/assets/comboup.png`,
        sha256: "357a5aeb20cde90512a2b277b79a617e8cdb0dbacaeec77b01fd5ff314a94b55",
      },
      "comboright.png": {
        url: `${WIN95_RAW}/assets/comboright.png`,
        sha256: "b933a31eda85340371d7cc114112b7733695c8afa71838253c56530fb18b3fc7",
      },
      "comboleft.png": {
        url: `${WIN95_RAW}/assets/comboleft.png`,
        sha256: "77f0b4edcec796b84e5004f8403c83856e77fdad0e69e9ebe8b7577a38a854af",
      },
      "background.bmp": {
        url: `${WIN95_RAW}/assets/background.bmp`,
        sha256: "31dd25b2c1bbf93e4d77c535380eae4a21da563c3de1f57026becd74a1e8acde",
      },
    },
    unpack: (downloadDir) => ({ css: join(downloadDir, "win95.css"), license: join(downloadDir, "LICENSE") }),
    fontFamilies: {},
    droppedRules: [
      // The library's demo landing page: a full-screen video header, a cloud backdrop, a sticky footer.
      /^html$/,
      /^header\b/,
      /^\.bg-cloud$/,
      /^#page-content$/,
      /^\.navbar/,
      // Moves every checkbox and radio off screen, for Bootstrap's custom labels the desktop does not draw.
      /^input\[type="(radio|checkbox)"\]$/,
      // A one-colon typo of the `::-webkit-scrollbar-button` rule the library also spells correctly.
      /^:-webkit-scrollbar/,
    ],
    droppedDeclarations: [
      // White text and room for the demo's fixed footer; the desktop sets its own colors and fills the page.
      { selector: /^body$/, property: /^(color|padding-bottom|background)$/ },
      // The demo's footer placement; the desktop lays its taskbar out itself.
      { selector: /^\.taskbar$/, property: /^(margin|margin-right|position|bottom|width|z-index|padding)$/ },
      // Bootstrap opens its menus by toggling display; the desktop mounts a menu only while it is open.
      { selector: /^\.dropdown-menu$/, property: /^(display|margin-left|font-size)$/ },
      // Sized for Bootstrap's own button padding; the desktop's buttons bring theirs.
      { selector: /^\.btn$/, property: /^padding$/ },
      { selector: /^\.modal-header$/, property: /^height$/ },
      // The caption is the desktop's flex row of icon, title and controls; Bootstrap's is a block of text.
      { selector: /^\.card-header$/, property: /^(display|white-space|text-align|padding-top|padding-bottom)$/ },
    ],
    // Windows 95 silver to the Windows 2000 3D face.
    colors: [
      ["silver", "#d4d0c8"],
      ["#c0c0c0", "#d4d0c8"],
    ],
  },
];

function themeScope(themeName) {
  return `:root[data-ui-theme="${themeName}"]`;
}

/** One selector of a comma list, placed under the theme's scope. */
function scopeSelector(selector, scope) {
  const trimmed = selector.trim();
  if (trimmed === ":root" || trimmed === "html") return scope;
  if (trimmed.startsWith(":root") || trimmed.startsWith("html")) {
    return scope + trimmed.replace(/^(:root|html)/, "");
  }
  return `${scope} ${trimmed}`;
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

/** Copy every file a url() names beside the output, and point the url() at the copy. */
function relocateUrls(value, sourceDir, assetDir, copied) {
  return value.replace(/url\(\s*(['"]?)([^'")]+)\1\s*\)/g, (match, _quote, target) => {
    if (target.startsWith("data:") || target.startsWith("#")) return match;
    const fileName = basename(target);
    if (!copied.has(fileName)) {
      writeFileSync(join(assetDir, fileName), readFileSync(join(sourceDir, target)));
      copied.add(fileName);
    }
    return `url("./assets/${fileName}")`;
  });
}

async function download(theme, downloadDir) {
  for (const [fileName, { url, sha256 }] of Object.entries(theme.files)) {
    const response = await fetch(url);
    if (!response.ok) throw new Error(`GET ${url} failed with HTTP ${response.status}`);
    const bytes = Buffer.from(await response.arrayBuffer());
    const digest = createHash("sha256").update(bytes).digest("hex");
    if (digest !== sha256) throw new Error(`${url} has sha256 ${digest}, expected ${sha256}`);
    writeFileSync(join(downloadDir, fileName), bytes);
  }
}

async function vendorTheme(theme) {
  const downloadDir = mkdtempSync(join(tmpdir(), `vendor-${theme.name}-`));
  try {
    await download(theme, downloadDir);
    const { css, license } = theme.unpack(downloadDir);
    const outDir = join(vendorDir, theme.name);
    const assetDir = join(outDir, "assets");
    rmSync(outDir, { recursive: true, force: true });
    mkdirSync(assetDir, { recursive: true });

    const root = postcss.parse(readFileSync(css, "utf8"), { from: css });
    const scope = themeScope(theme.name);
    const copied = new Set();

    root.walkComments((comment) => comment.remove());
    root.walkRules((rule) => {
      if (rule.parent?.type === "atrule" && /keyframes$/.test(rule.parent.name)) return;
      const selectors = rule.selectors.filter(
        (selector) => !theme.droppedRules.some((pattern) => pattern.test(selector.trim())),
      );
      if (selectors.length === 0) {
        rule.remove();
        return;
      }
      rule.walkDecls((decl) => {
        const isDropped = selectors.some((selector) =>
          theme.droppedDeclarations.some(
            (drop) => drop.selector.test(selector.trim()) && drop.property.test(decl.prop),
          ),
        );
        if (isDropped) decl.remove();
      });
      rule.selectors = selectors.map((selector) => scopeSelector(selector, scope));
      if (rule.nodes.length === 0) rule.remove();
    });
    root.walkDecls((decl) => {
      let value = decl.value;
      if (decl.prop === "font-family") value = renameFontFamilies(value, theme.fontFamilies);
      // win95.css draws its captions with the legacy `-webkit-linear-gradient(left, ...)`, which the
      // build's CSS optimizer turns into a top-to-bottom gradient; this is the standard spelling it means.
      value = value.replace(/-webkit-linear-gradient\(\s*left\s*,/g, "linear-gradient(to right,");
      value = recolor(value, theme.colors);
      value = relocateUrls(value, dirname(css), assetDir, copied);
      decl.value = value;
    });

    const header =
      `/* GENERATED by scripts/vendor-retro-themes.mjs from ${theme.source} (MIT; see LICENSE beside ` +
      `this file). Do not edit by hand: rerun the script. Every rule is scoped to ${scope}. */\n`;
    writeFileSync(join(outDir, "lib.css"), header + root.toString() + "\n");
    writeFileSync(join(outDir, "LICENSE"), readFileSync(license));
    console.log(`${theme.name}: ${theme.source}, ${copied.size} asset(s)`);
  } finally {
    rmSync(downloadDir, { recursive: true, force: true });
  }
}

for (const theme of THEMES) await vendorTheme(theme);
