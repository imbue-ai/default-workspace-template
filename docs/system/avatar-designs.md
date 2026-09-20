# Avatar designs

The workspace's avatar is the image the chat app's pinned entry wears in the `avatar` style (`docs/system/blueprint/pinned-taskbar-entries/plan-pinned-taskbar-entries.md`). Right-click the entry (long press on touch) and choose **Change avatar...** to open the chooser: every design's still preview, the workspace's current choice marked, a link to the chosen design's original SVG, and **Design your own...**. The choice belongs to the workspace: choosing a design changes every open window, on every device, and survives reloads. A missing or unreadable choice draws the gummy seal.

The image says whether any agent on the machine is working. The seven bundled designs rest with closed eyes while every agent is idle and wake with open eyes and their own motion (the puddle shuffles, the cube squishes, the cat kneads, the snail waves its feelers, the heart floats in a figure eight, the dragon hovers, the seal paddles its flippers) while any agent but the workspace's own services agent is running. When the status may be out of date (the agents event file is older than ten minutes, or absent) the entry's tooltip says so. Reduced motion keeps every animation still. The rendered images are derived; authored originals are never changed.

**Design your own...** starts a chat seeded with a prompt that asks the agent to draw a design, show a preview, and register it only once the user approves. The message is sent as soon as the chat opens. The chat app is found through the launch path that declares a `message` parameter; the shell names no app, and the button is disabled, with the reason as its tooltip, when no app declares one.

## Authoring a design through chat

Draw an SVG, show the user a preview, and wait for their approval before selecting it. Use the 100 by 100 coordinate system: the outer element must be `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">`. It fits the floating entry's 56px box and the taskbar entry's smaller one, so draw with enough margin for movement.

Supported elements: `svg`, `g`, `defs`, `title`, `desc`, `path`, `circle`, `ellipse`, `rect`, `line`, `polyline`, `polygon`, `linearGradient`, `radialGradient`, and `stop`. Use presentation attributes for colour, stroke, and geometry, including local `url(#gradient-id)` paints. Arbitrary CSS, `style` attributes, scripts, event attributes, external references, `image`, `use`, `foreignObject`, filters, SMIL, and processing instructions are refused. The small supported animation vocabulary is:

- `class="jelly-body"`: breathing while idle and a working tilt, around (50, 87).
- `class="jelly-eyes"`: blinking around the eyes' bounding box.
- `class="jelly-extra"`: a gentle sway for an appendage.

The shell supplies these animations; do not embed custom CSS (the one `<style>` allowed is the shell's own shared sheet, which the bundled originals carry). A rendered copy always animates through the current sheet, whatever its saved source embedded. Static designs are supported too. The validator uses defusedxml and an explicit drawing vocabulary; the shell always serves a design as an SVG image under a Content Security Policy that blocks scripts and external resources, never as markup inserted into its own page.

Once approved, register the file from the workspace root:

```bash
uv run python -m imbue.system_interface.avatar.register_avatar \
  --source data/avatar-designs/my-design.svg --id my-design --label "My design"
```

Append `--select` to make it the workspace's design at once, and `--shell-url http://127.0.0.1:<port>` to reach a shell other than the one `MINDS_WORKSPACE_SERVER_URL` names (the loopback port 8000 when that is unset).

Registration calls the loopback-only `POST /api/avatars` with `{id, label, svg, source_path}`. The full source and its provenance are kept together in `data/.apps/system_interface/avatars/catalog.json`; no shell edit, build, or restart is needed, and the chooser reads the catalog each time it opens. Re-registering an id replaces its source and provenance together; a bundled id cannot be replaced. The catalog holds at most 32 registered designs of at most 256 KiB each; at capacity, replace one by its id. A malformed or unsafe registration fails before anything is written, and selection is a separate validated step, so a failed registration keeps the old selection.

## HTTP contract

- `GET /api/avatars`: `{designs: [{id, label, source_path}], selected, default}`; bundled designs have `source_path: null`.
- `GET /api/avatars/<id>/image.svg?mood=idle|working&preview=1`: the passive image; a preview holds every pose still. Unknown ids answer 404, an unknown mood 400.
- `GET /api/avatars/<id>/source.svg`: the original, as an attachment.
- `POST /api/avatars`: loopback-only registration (201); 400 for invalid input, 403 for a caller off the loopback.
- `POST /api/avatar-selection` with `{design}`: writes the workspace's choice to `data/.state/system_interface/avatar_selection.json` and broadcasts `avatar_selection_changed {design}` to every window; an unknown id answers 400.

The mood is pushed, never fetched: every window receives `avatar_status {mood, is_stale}` on connect and whenever either changes. It is folded from the agents event file the mngr observer (run by the chat app) writes at `$MNGR_HOST_DIR/events/mngr/agents/events.jsonl` (`~/.mngr` when unset), with plain JSON parsing and no mngr import.
