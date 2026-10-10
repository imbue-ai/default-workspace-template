---
name: make-theme-icon
description: "Use when an app needs an icon drawn for a workspace theme (Classic Mac, Windows 2000, or a theme the user made) -- after building an app, after making a theme, when `workspace-themes icon missing` names one, when an app's own icon changed, or when the user asks for a new or better icon under a theme. Works with any image model, or none: the agent can draw a pixel grid or an SVG itself, and the workspace-themes tools fit any drawing onto the theme's limits."
metadata:
  author: imbue
---

# Making a theme's icon for an app

Every theme says how its icons look: in prose (`icons/guide.md` in the theme's folder) and as
limits a script checks (the `[icons]` table of its `theme.toml`: format, size, palette, color
count, background). Until an app has an icon for a theme, the shell derives one from the app's
standard icon, so a missing icon is never a broken screen -- this skill replaces the derived
icon with one drawn in the theme's style. The full design is
`docs/system/blueprint/workspace-themes/plan-workspace-themes.md`, section 6.

Run every command from the repo root. The standard theme's icons are not made here: they are
the app's own `icon.svg`, drawn to `docs/system/app-icons.md`.

## 1. Read what the theme wants

```bash
uv run workspace-themes icon missing
```

names every theme with icons of its own and the apps it has none for. For each theme:

```bash
uv run workspace-themes icon spec <theme-id>
```

It prints the limits, the guide's path, the reference icons, `draw_at_scale` (how many times
larger than the canvas to ask a model to draw), and `apps_without_icon`. Then:

- Read the guide in full, and look at each reference icon (open the png; they are small, so
  `icon fit <reference> --theme <theme-id> --out /tmp/x.png --preview /tmp/x-big.png` gives an
  enlarged copy to look at).
- Read the app's `app.toml` (`display_name`, `description`) and look at its standard
  `icon.svg`.
- Name **one familiar object** that stands for the app, the way the theme's own icons do (a
  folder for files, a speech balloon for chat). Keep the object the app's standard icon uses
  unless the theme's era would draw something else.

## 2. Draw candidates

Use the first of these you have. Draw two to four candidates and keep the best.

**An image tool the harness gives you.** Prompt it with the object, the guide's treatment
(outline, light, palette, view), "flat plain background", and "no text". Ask for a size of
`draw_at_scale` times the canvas when the tool takes one.

**Retro Diffusion**, for a pixelated theme, when the user has a Retro Diffusion key saved as
`data/.secrets/retrodiffusion.env` (`RETRODIFFUSION_API_KEY=...`). It draws pixel art at the
theme's own size, and keeps to the theme's palette:

```bash
python3 system/scripts/with_secrets.py data/.secrets/retrodiffusion.env -- uv run python .agents/skills/make-theme-icon/providers/retro_diffusion.py --prompt "<object and treatment>" --size <size> --palette "<the theme's palette, comma-separated, if it has one>" --variants 2 --out-dir /tmp/<app>-<theme>
```

**Any image model litellm reaches** (OpenAI, Vertex, Bedrock, ...), when the user has a key for
one saved under `data/.secrets/`:

```bash
python3 system/scripts/with_secrets.py data/.secrets/<provider>.env -- uv run python .agents/skills/make-theme-icon/providers/litellm_image.py --model <model> --prompt "<object and treatment>" --variants 2 --out-dir /tmp/<app>-<theme>
```

Each image costs the user money with these two: draw only the icons that are missing. Do not
ask the user for a key just to draw an icon; if they want one, the `connect-external-service`
skill's secret card is how it is saved. A background agent never asks; it draws by hand.

**By hand, with no image model.** For a `png` theme, write the icon as a text grid at the
theme's size, one character per pixel, `.` for clear, and a legend for the rest:

```text
......kkkkkkkkkkkkkkkkkk........
.....kwwwwwwwwwwwwwwwwwwk.......
...
```

```bash
uv run workspace-themes icon grid /tmp/<app>-<theme>.txt --theme <theme-id> --legend "k=#000000,w=#ffffff" --out /tmp/<app>-<theme>.png --preview /tmp/<app>-<theme>-big.png
```

Draw the outline first, then the fills, then the details; at 32 pixels a detail is one or two
pixels, and the object should fill about 24 to 30 of them. For an `svg` theme, write the SVG
yourself, following the theme's guide, to `/tmp/<app>-<theme>.svg`, and go to step 4.

## 3. Fit each candidate

```bash
uv run workspace-themes icon fit /tmp/<app>-<theme>/candidate-0.png --theme <theme-id> --out /tmp/<app>-<theme>-0.png --preview /tmp/<app>-<theme>-0-big.png
```

`fit` clears a flat background, trims and squares the drawing, scales it to the canvas
(nearest neighbour for a pixelated theme), and maps it to the palette or color count. This is
what lets a drawing from any model sit beside the theme's own icons. It prints `ok` or what
still misses the limits.

Look at each preview, next to the theme's reference icons, and pick the one that reads best
as the object at a glance. Draw again rather than accept one that does not: a blob, a lost
outline, text, or a different style from the references.

## 4. Check and install

```bash
uv run workspace-themes icon check /tmp/<app>-<theme>-0.png --theme <theme-id>
uv run workspace-themes icon install /tmp/<app>-<theme>-0.png --theme <theme-id> --app <registered app name>
```

`install` checks again and puts the icon where the shell looks: the theme's own `icons/` for a
theme made in this workspace, or `data/.themes/<theme-id>/icons/` for a built-in theme, which
stays as shipped. The shell picks it up at once, with no restart. `--app app` replaces the
generic program icon, shown for an app with no icon of its own, of a theme made in this
workspace.

A built-in theme's own icons come first: `install` refuses an icon for an app the theme ships
one for, and the generic program icon, of a built-in theme. To change one of those, make a
workspace theme based on it (the `create-theme` skill, `--base <theme-id>`) and install the
icon there.

## 5. See it in place

Open `/theme-gallery?theme=<theme-id>` on the shell's origin (with Playwright, privately): its
icon row shows every app's icon under the theme and where each came from
(`data-icon-source`: `theme` for a drawn icon, `derived`, or `fallback`). The new icon should
read as `theme`.
