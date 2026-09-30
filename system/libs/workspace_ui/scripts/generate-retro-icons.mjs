#!/usr/bin/env node
// Draw the retro themes' pixel-art icons with Retro Diffusion
// (https://retrodiffusion.ai) and write them to src/themes/icons/<theme>/.
//
// Usage (from system/libs/workspace_ui):
//
//   RETRODIFFUSION_API_KEY=rdpk-... node scripts/generate-retro-icons.mjs [--variants N] [--only name,name] [--out DIR]
//
// Each icon is drawn once per retro theme at 32x32 (the classic desktop icon
// size, which the desktop shows at its icon sizes). Classic Mac icons are held to a
// black-and-white palette, as on a System 6 screen; Windows 2000 icons get the
// full palette. With --variants N > 1 every icon is drawn N times as
// <name>.<i>.png so the best one can be picked by hand and renamed to
// <name>.png; the default writes <name>.png directly.
//
// To give a new app a retro icon, add it to ICONS below (its key is the app's
// registry name) and run the script with --only <name>. Every image costs
// Retro Diffusion credits, so --only keeps a rerun to what changed.

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";

const API_BASE = "https://api.retrodiffusion.ai/v2";
const ICON_SIZE_PX = 32;
const POLL_INTERVAL_MS = 2000;
const POLL_TIMEOUT_MS = 180_000;

// A 2x1 PNG holding pure black and pure white: the palette the Classic Mac
// icons are constrained to.
const ONE_BIT_PALETTE_PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAIAAAABCAIAAAB7QOjdAAAAD0lEQVR42mNgYGD4//8/AAYBAv67yYXpAAAAAElFTkSuQmCC";

const THEMES = {
  "mac-classic": {
    style: "rd_pro__simple",
    palette: ONE_BIT_PALETTE_PNG_BASE64,
    describe: (subject) =>
      `1-bit black and white classic Macintosh System 6 Finder icon of ${subject}, crisp black outline, white fill`,
  },
  "windows-2000": {
    style: "rd_pro__simple",
    palette: null,
    describe: (subject) => `Windows 2000 desktop icon of ${subject}, 256 colors, dark outline`,
  },
};

// Keyed by the app's registry name; "app" is the icon for any app without one of its own. A subject
// is one string for every theme, or an object naming a subject per theme where one era's picture of
// the thing differs from the other's.
const ICONS = {
  chat: "two overlapping speech bubbles",
  terminal: {
    "mac-classic": "an old CRT computer terminal whose screen shows lines of text",
    "windows-2000": "a command prompt console window showing a blinking cursor",
  },
  files: "an open file folder holding papers",
  browser: "a globe with a mouse pointer arrow",
  getting_started: "a small flag on a pole planted on a hill",
  app: {
    "mac-classic": "a diamond shape with a hand holding a pencil",
    "windows-2000": "a cardboard software box with a floppy disk in front of it",
  },
};

function subjectFor(iconName, themeName) {
  const subject = ICONS[iconName];
  return typeof subject === "string" ? subject : subject[themeName];
}

function parseArgs(argv) {
  const libraryDir = dirname(dirname(fileURLToPath(import.meta.url)));
  const options = {
    variants: 1,
    only: null,
    out: join(libraryDir, "src", "themes", "icons"),
  };
  for (let index = 0; index < argv.length; index += 1) {
    const flag = argv[index];
    const value = argv[index + 1];
    if ((flag === "--variants" || flag === "--only" || flag === "--out") && value === undefined) {
      throw new Error(`${flag} needs a value`);
    }
    if (flag === "--variants") {
      options.variants = Number.parseInt(value, 10);
      index += 1;
    } else if (flag === "--only") {
      options.only = new Set(value.split(","));
      index += 1;
    } else if (flag === "--out") {
      options.out = resolve(value);
      index += 1;
    } else {
      throw new Error(`Unknown argument ${flag}`);
    }
  }
  if (!Number.isInteger(options.variants) || options.variants < 1 || options.variants > 4) {
    throw new Error("--variants must be an integer from 1 to 4");
  }
  if (options.only !== null) {
    for (const name of options.only) {
      if (!(name in ICONS)) throw new Error(`--only names an icon not in ICONS: ${name}`);
    }
  }
  return options;
}

async function callApi(apiKey, method, path, body) {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    headers: {
      "X-RD-Token": apiKey,
      "Content-Type": "application/json",
      "Idempotency-Key": randomUUID(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  if (!response.ok) throw new Error(`${method} ${path} failed with HTTP ${response.status}: ${text}`);
  return JSON.parse(text);
}

async function drawIcon(apiKey, theme, subject, variants) {
  const payload = {
    prompt: theme.describe(subject),
    prompt_style: theme.style,
    width: ICON_SIZE_PX,
    height: ICON_SIZE_PX,
    num_images: variants,
    remove_bg: true,
  };
  if (theme.palette !== null) payload.input_palette = theme.palette;
  const { task_id: taskId } = await callApi(apiKey, "POST", "/inferences", payload);
  const deadline = Date.now() + POLL_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const task = await callApi(apiKey, "GET", `/inferences/tasks/${taskId}`);
    if (task.status === "succeeded") return task.result;
    if (task.status !== "pending" && task.status !== "running" && task.status !== "queued") {
      throw new Error(`Task ${taskId} ended as ${task.status}: ${JSON.stringify(task)}`);
    }
    await new Promise((resolveWait) => setTimeout(resolveWait, POLL_INTERVAL_MS));
  }
  throw new Error(`Task ${taskId} did not finish within ${POLL_TIMEOUT_MS / 1000}s`);
}

async function main() {
  const apiKey = process.env.RETRODIFFUSION_API_KEY;
  if (!apiKey) throw new Error("Set RETRODIFFUSION_API_KEY to a Retro Diffusion API key (rdpk-...)");
  const options = parseArgs(process.argv.slice(2));
  const jobs = [];
  for (const [themeName, theme] of Object.entries(THEMES)) {
    const themeDir = join(options.out, themeName);
    mkdirSync(themeDir, { recursive: true });
    for (const iconName of Object.keys(ICONS)) {
      if (options.only !== null && !options.only.has(iconName)) continue;
      jobs.push(
        drawIcon(apiKey, theme, subjectFor(iconName, themeName), options.variants).then((result) => {
          result.base64_images.forEach((image, index) => {
            const fileName = options.variants === 1 ? `${iconName}.png` : `${iconName}.${index}.png`;
            writeFileSync(join(themeDir, fileName), Buffer.from(image, "base64"));
          });
          console.log(`${themeName}/${iconName}: $${result.balance_cost} (balance $${result.remaining_balance})`);
        }),
      );
    }
  }
  const outcomes = await Promise.allSettled(jobs);
  const failures = outcomes.filter((outcome) => outcome.status === "rejected");
  for (const failure of failures) console.error(failure.reason);
  if (failures.length > 0) process.exit(1);
}

await main();
