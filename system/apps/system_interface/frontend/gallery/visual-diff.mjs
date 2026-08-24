#!/usr/bin/env node
/**
 * Visual before/after harness for the System Interface component gallery.
 *
 * It compiles a given `style.css` with the Tailwind CLI, renders the static
 * gallery (gallery.html) against it, screenshots every `[data-shot]` section,
 * and pixel-diffs two captures into an HTML report. This is how a token sweep
 * (see docs/design-system.md, phases P1/P2) is proven to change only what it
 * intends to.
 *
 * No dev server: each side is just "compile CSS -> render static file ->
 * screenshot". Because only the CSS varies between before/after, comparing two
 * git refs needs no worktree -- we extract each ref's style.css with
 * `git show <ref>:<path>` and compile that.
 *
 * Commands:
 *   build-css [--css <path>] [--out <path>]
 *       Compile a style.css to plain CSS (for authoring gallery.html directly).
 *
 *   capture --label <name> [--css <path>]
 *       Compile + render + screenshot each section into .visual-diff/<name>/.
 *
 *   compare <labelA> <labelB>
 *       Pixel-diff two captures -> .visual-diff/report-<A>-vs-<B>.html
 *
 *   diff-refs <before-ref> [<after-ref|WORKING>]
 *       One-command before/after across git refs (after defaults to WORKING,
 *       the on-disk style.css). Captures both sides and compares.
 *
 * Prior art (deliberately mirrored, not reused): mngr's
 * apps/minds/scripts/visual_diff.py -- same capture/compare/report shape. We
 * don't reuse it because it runs on Python Playwright + the monorepo root venv
 * (broken on macOS: the pcmflux dep is Linux-only) and targets the minds SPA.
 */

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, copyFileSync, writeFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const GALLERY_DIR = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = path.dirname(GALLERY_DIR);
const OUT_ROOT = path.join(GALLERY_DIR, ".visual-diff");
const TMP_DIR = path.join(GALLERY_DIR, ".vd-tmp");
const GALLERY_HTML = path.join(GALLERY_DIR, "gallery.html");
const DEFAULT_CSS = path.join(FRONTEND_DIR, "src", "style.css");

const VIEWPORT_WIDTH = 1200;
const VIEWPORT_HEIGHT = 900;
// pixelmatch per-pixel colour tolerance. 0.1 ignores sub-perceptual antialias
// jitter while still catching any real token/colour/spacing change.
const PIXEL_THRESHOLD = 0.1;

function log(msg) {
  process.stdout.write(`[visual-diff] ${msg}\n`);
}

function fail(msg) {
  process.stderr.write(`[visual-diff] ERROR: ${msg}\n`);
  process.exit(1);
}

function parseFlags(args) {
  const flags = {};
  const positional = [];
  for (let i = 0; i < args.length; i++) {
    const a = args[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = args[i + 1];
      if (next === undefined || next.startsWith("--")) {
        flags[key] = true;
      } else {
        flags[key] = next;
        i++;
      }
    } else {
      positional.push(a);
    }
  }
  return { flags, positional };
}

function slugifyRef(ref) {
  return ref.replace(/[^a-zA-Z0-9]+/g, "-").replace(/^-+|-+$/g, "") || "ref";
}

// -- CSS compilation ------------------------------------------------------

function tailwindBinCandidates() {
  return [
    path.join(FRONTEND_DIR, "node_modules", ".bin", "tailwindcss"),
    path.join(FRONTEND_DIR, "node_modules", ".bin", "tailwindcss.cmd"),
  ];
}

function buildCss(cssInputPath, outPath) {
  if (!existsSync(cssInputPath)) fail(`CSS input not found: ${cssInputPath}`);
  mkdirSync(path.dirname(outPath), { recursive: true });
  const bin = tailwindBinCandidates().find((p) => existsSync(p));
  const runFrom = FRONTEND_DIR; // so @import "tailwindcss"/dockview-core resolve from node_modules
  const args = ["-i", cssInputPath, "-o", outPath];
  try {
    if (bin) {
      execFileSync(bin, args, { cwd: runFrom, stdio: ["ignore", "ignore", "pipe"] });
    } else {
      // Fall back to the package runner if the bin symlink is absent.
      execFileSync("pnpm", ["exec", "tailwindcss", ...args], { cwd: runFrom, stdio: ["ignore", "ignore", "pipe"] });
    }
  } catch (e) {
    const stderr = e.stderr ? e.stderr.toString() : String(e);
    fail(
      `Tailwind CLI failed compiling ${cssInputPath}.\n` +
        `Is the frontend installed? Run: (cd ${FRONTEND_DIR} && pnpm install)\n` +
        stderr,
    );
  }
  if (!existsSync(outPath)) fail(`Tailwind produced no output at ${outPath}`);
  return outPath;
}

// -- Screenshot capture ---------------------------------------------------

async function launchBrowser() {
  let chromium;
  try {
    ({ chromium } = await import("playwright-core"));
  } catch {
    fail("playwright-core is not installed in the frontend.\n" + `Run: (cd ${FRONTEND_DIR} && pnpm install)`);
  }
  // playwright-core ships no bundled browser, so drive the system Google Chrome
  // via its channel (present on this machine). Fall back to any managed chromium
  // in the Playwright cache if the Chrome channel is unavailable (e.g. CI/Linux).
  try {
    return await chromium.launch({ channel: "chrome" });
  } catch (e) {
    log(`system Chrome channel unavailable (${e.name || "error"}); trying managed chromium`);
    return await chromium.launch();
  }
}

async function capture(label, cssInputPath) {
  const captureDir = path.join(OUT_ROOT, label);
  const pngDir = path.join(captureDir, "png");
  if (existsSync(captureDir)) rmSync(captureDir, { recursive: true, force: true });
  mkdirSync(pngDir, { recursive: true });

  log(`compiling CSS for '${label}' from ${path.relative(FRONTEND_DIR, cssInputPath)}`);
  buildCss(cssInputPath, path.join(captureDir, "compiled.css"));
  copyFileSync(GALLERY_HTML, path.join(captureDir, "gallery.html"));

  const browser = await launchBrowser();
  const shots = [];
  try {
    const context = await browser.newContext({
      viewport: { width: VIEWPORT_WIDTH, height: VIEWPORT_HEIGHT },
      deviceScaleFactor: 1,
      reducedMotion: "reduce",
    });
    const page = await context.newPage();
    const url = "file://" + path.join(captureDir, "gallery.html");
    await page.goto(url, { waitUntil: "load", timeout: 20000 });
    // The inline script builds the token grids after load; wait for it to run.
    await page.waitForFunction(() => document.querySelectorAll("[data-shot]").length > 0, { timeout: 10000 });
    await page.waitForTimeout(250);

    const sections = await page.$$("[data-shot]");
    for (const el of sections) {
      const slug = await el.getAttribute("data-shot");
      await el.scrollIntoViewIfNeeded();
      await el.screenshot({ path: path.join(pngDir, `${slug}.png`) });
      shots.push(slug);
      log(`shot ${slug}`);
    }
    await page.screenshot({ path: path.join(pngDir, "_full.png"), fullPage: true });
  } finally {
    await browser.close();
  }
  writeFileSync(path.join(captureDir, "manifest.json"), JSON.stringify({ label, shots }, null, 2));
  log(`captured ${shots.length} sections -> ${path.relative(FRONTEND_DIR, captureDir)}`);
  return captureDir;
}

// -- Compare --------------------------------------------------------------

async function loadPng(p) {
  const { PNG } = await import("pngjs");
  const { readFileSync } = await import("node:fs");
  return PNG.sync.read(readFileSync(p));
}

async function diffOne(pngA, pngB, diffOut) {
  const pixelmatch = (await import("pixelmatch")).default;
  const { PNG } = await import("pngjs");
  const { writeFileSync: wf } = await import("node:fs");
  const a = await loadPng(pngA);
  const b = await loadPng(pngB);
  if (a.width !== b.width || a.height !== b.height) {
    return { verdict: "differs", changed: -1, dims: `${a.width}x${a.height} vs ${b.width}x${b.height}` };
  }
  const diff = new PNG({ width: a.width, height: a.height });
  const changed = pixelmatch(a.data, b.data, diff.data, a.width, a.height, { threshold: PIXEL_THRESHOLD });
  wf(diffOut, PNG.sync.write(diff));
  return { verdict: changed === 0 ? "identical" : "differs", changed, dims: `${a.width}x${a.height}` };
}

function listPngSlugs(captureDir) {
  const pngDir = path.join(captureDir, "png");
  if (!existsSync(pngDir)) return [];
  return readdirSync(pngDir)
    .filter((f) => f.endsWith(".png") && f !== "_full.png")
    .map((f) => f.replace(/\.png$/, ""));
}

async function compare(labelA, labelB) {
  const dirA = path.join(OUT_ROOT, labelA);
  const dirB = path.join(OUT_ROOT, labelB);
  if (!existsSync(dirA)) fail(`capture not found: ${labelA} (run capture first)`);
  if (!existsSync(dirB)) fail(`capture not found: ${labelB} (run capture first)`);

  const diffDir = path.join(OUT_ROOT, `diff-${labelA}-vs-${labelB}`);
  if (existsSync(diffDir)) rmSync(diffDir, { recursive: true, force: true });
  mkdirSync(diffDir, { recursive: true });

  const slugsA = listPngSlugs(dirA);
  const slugsB = new Set(listPngSlugs(dirB));
  const rows = [];
  for (const slug of slugsA) {
    if (!slugsB.has(slug)) {
      rows.push({ slug, verdict: "missing_in_b", changed: 0 });
      continue;
    }
    const res = await diffOne(
      path.join(dirA, "png", `${slug}.png`),
      path.join(dirB, "png", `${slug}.png`),
      path.join(diffDir, `${slug}.png`),
    );
    rows.push({ slug, ...res });
    log(`${slug}: ${res.verdict}${res.changed > 0 ? ` (${res.changed}px)` : ""}`);
  }
  for (const slug of slugsB) {
    if (!slugsA.includes(slug)) rows.push({ slug, verdict: "missing_in_a", changed: 0 });
  }

  const reportPath = path.join(OUT_ROOT, `report-${labelA}-vs-${labelB}.html`);
  writeFileSync(reportPath, renderReport(labelA, labelB, rows, diffDir));
  const nIdentical = rows.filter((r) => r.verdict === "identical").length;
  const nDiffers = rows.filter((r) => r.verdict === "differs").length;
  const nMissing = rows.filter((r) => r.verdict.startsWith("missing")).length;
  log(`compare done: ${nIdentical} identical / ${nDiffers} differ / ${nMissing} missing`);
  log(`report: ${reportPath}`);
  return { reportPath, rows };
}

function renderReport(labelA, labelB, rows, diffDir) {
  const relFromReport = (p) => path.relative(OUT_ROOT, p);
  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const nIdentical = rows.filter((r) => r.verdict === "identical").length;
  const nDiffers = rows.filter((r) => r.verdict === "differs").length;
  const nMissing = rows.filter((r) => r.verdict.startsWith("missing")).length;
  const body = rows
    .map((r) => {
      const cls = r.verdict === "identical" ? "ok" : r.verdict.startsWith("missing") ? "missing" : "differs";
      const badge =
        r.verdict === "differs" && r.changed > 0
          ? `differs (${r.changed}px${r.dims ? `, ${esc(r.dims)}` : ""})`
          : r.verdict === "differs"
            ? `differs (${esc(r.dims || "size")})`
            : r.verdict;
      const pngA = `${labelA}/png/${r.slug}.png`;
      const pngB = `${labelB}/png/${r.slug}.png`;
      const pngDiff = `${relFromReport(diffDir)}/${r.slug}.png`;
      const cells = r.verdict.startsWith("missing")
        ? `<td colspan="3">(section only in one capture)</td>`
        : `<td><img src="${esc(pngA)}"></td>` +
          `<td><img src="${esc(pngB)}"></td>` +
          `<td>${r.verdict === "identical" ? '<span class="muted">no diff</span>' : `<img src="${esc(pngDiff)}">`}</td>`;
      return `<tr><td><code>${esc(r.slug)}</code></td><td class="v-${cls}">${esc(badge)}</td>${cells}</tr>`;
    })
    .join("\n");
  return `<!doctype html><html><head><meta charset="utf-8">
<title>visual diff: ${esc(labelA)} vs ${esc(labelB)}</title>
<style>
  body { font: 14px -apple-system, system-ui, sans-serif; margin: 24px; color: #18181b; }
  h1 { font-size: 18px; }
  .summary { margin: 8px 0 20px; font-size: 14px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #e4e4e7; padding: 8px; vertical-align: top; text-align: left; }
  th { background: #fafafa; position: sticky; top: 0; }
  td img { display: block; max-width: 320px; border: 1px solid #d4d4d8; }
  code { font-family: ui-monospace, monospace; }
  .muted { color: #a1a1aa; }
  .v-ok { color: #047857; font-weight: 600; }
  .v-differs { color: #b91c1c; font-weight: 600; }
  .v-missing { color: #92400e; font-weight: 600; }
</style></head><body>
<h1>visual diff: <code>${esc(labelA)}</code> vs <code>${esc(labelB)}</code></h1>
<div class="summary">
  <strong>${nIdentical}</strong> identical &middot;
  <strong style="color:#b91c1c">${nDiffers}</strong> differ &middot;
  ${nMissing} missing &middot; total ${rows.length}
  <div class="muted">Columns: ${esc(labelA)} (before) &middot; ${esc(labelB)} (after) &middot; diff overlay (magenta = changed pixels).</div>
</div>
<table><thead><tr><th>section</th><th>verdict</th><th>${esc(labelA)}</th><th>${esc(labelB)}</th><th>diff</th></tr></thead>
<tbody>
${body}
</tbody></table>
</body></html>`;
}

// -- diff-refs ------------------------------------------------------------

function repoRelStyleCssPath() {
  // Path of style.css relative to the repo root, for `git show <ref>:<path>`.
  const prefix = execFileSync("git", ["-C", FRONTEND_DIR, "rev-parse", "--show-prefix"], { encoding: "utf8" }).trim();
  return path.posix.join(prefix, "src/style.css");
}

function extractCssAtRef(ref, outPath) {
  const repoRel = repoRelStyleCssPath();
  let content;
  try {
    content = execFileSync("git", ["-C", FRONTEND_DIR, "show", `${ref}:${repoRel}`], {
      encoding: "utf8",
      maxBuffer: 32 * 1024 * 1024,
    });
  } catch {
    fail(`could not read ${repoRel} at ref '${ref}' (does the ref exist and contain that file?)`);
  }
  mkdirSync(path.dirname(outPath), { recursive: true });
  writeFileSync(outPath, content);
  return outPath;
}

async function diffRefs(beforeRef, afterRef) {
  const beforeLabel = slugifyRef(beforeRef);
  const beforeCss = extractCssAtRef(beforeRef, path.join(TMP_DIR, `${beforeLabel}.css`));
  await capture(beforeLabel, beforeCss);

  let afterLabel;
  if (!afterRef || afterRef === "WORKING") {
    afterLabel = "WORKING";
    await capture(afterLabel, DEFAULT_CSS);
  } else {
    afterLabel = slugifyRef(afterRef);
    const afterCss = extractCssAtRef(afterRef, path.join(TMP_DIR, `${afterLabel}.css`));
    await capture(afterLabel, afterCss);
  }
  const { reportPath, rows } = await compare(beforeLabel, afterLabel);
  const nDiffers = rows.filter((r) => r.verdict === "differs").length;
  log(nDiffers === 0 ? "RESULT: no visual differences." : `RESULT: ${nDiffers} section(s) differ. Open the report.`);
  log(`open ${reportPath}`);
}

// -- main -----------------------------------------------------------------

async function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const { flags, positional } = parseFlags(rest);

  if (cmd === "build-css") {
    const css = flags.css ? path.resolve(flags.css) : DEFAULT_CSS;
    const out = flags.out ? path.resolve(flags.out) : path.join(GALLERY_DIR, "compiled.css");
    buildCss(css, out);
    log(`compiled ${path.relative(FRONTEND_DIR, css)} -> ${path.relative(FRONTEND_DIR, out)}`);
    return;
  }
  if (cmd === "capture") {
    if (!flags.label) fail("capture requires --label <name>");
    const css = flags.css ? path.resolve(flags.css) : DEFAULT_CSS;
    await capture(String(flags.label), css);
    return;
  }
  if (cmd === "compare") {
    if (positional.length < 2) fail("compare requires <labelA> <labelB>");
    await compare(positional[0], positional[1]);
    return;
  }
  if (cmd === "diff-refs") {
    if (positional.length < 1) fail("diff-refs requires <before-ref> [<after-ref|WORKING>]");
    await diffRefs(positional[0], positional[1]);
    return;
  }
  process.stdout.write(
    "System Interface visual-diff harness\n\n" +
      "  node gallery/visual-diff.mjs build-css [--css <path>] [--out <path>]\n" +
      "  node gallery/visual-diff.mjs capture --label <name> [--css <path>]\n" +
      "  node gallery/visual-diff.mjs compare <labelA> <labelB>\n" +
      "  node gallery/visual-diff.mjs diff-refs <before-ref> [<after-ref|WORKING>]\n\n" +
      "Typical: node gallery/visual-diff.mjs diff-refs main\n",
  );
}

main().catch((e) => fail(e.stack || String(e)));
