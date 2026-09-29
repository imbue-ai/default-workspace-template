# App icons

An app's icon is the 48 by 48 tile the workspace draws wherever the app appears: its
desktop shortcut, the rail's shortcut and tab rows, the "All apps" popover, the launcher
tables, and a dock tab's leading glyph. The file sits beside the app's manifest as
`system/apps/<package>/icon.svg`, and `app.toml`'s `icon` names it. `forward_port.py`
reads the file at registration and stores the *markup* on the registry row, so an icon
changes on the app's next registration -- normally its next restart -- and never needs a
rebuild of the shell.

What follows is this workspace's default look, not a law of nature: the user can have a
different one whenever they want it, and this doc is where that change starts. Rewrite the
rules here, redraw the icons already drawn so the set stays one set, and carry the change
into the one-line restatements that point here (`AGENTS.md`, the `build-app` skill,
`forward_port.py`'s module docs).

## The rules

These hold for every icon: the built-in ones and any new app's alike.

- **48 by 48, with a corner radius of 32 percent** of the size (`rx="15.36"` at 48). The
  shell's `--desk-icon-radius` is the same 32 percent, so the tile keeps its shape
  wherever it is drawn and at whatever size.
- **Two layers, a background and a foreground, and nothing else.**
- The **background** covers the whole 48 by 48 and is a single flat colour.
- The **foreground** is one differently-coloured shape that fits a **32 by 32 box centred
  in the background** (`8..40` on both axes). It may be several paths, but it reads as one
  glyph.
- The foreground applies its colour with **fill only** -- no strokes anywhere in the file.
  Outline a stroked drawing rather than shipping the stroke.
- The mark is a **single iconic glyph that carries the poetic idea of the app**, not a
  picture of its interface. Its line quality is hand-drawn: no ruler-straight edges and no
  machine-perfect curves, but not noisy either.
- The two colours are **a pair from the palette below**.

Sizing inside the 32 box is optical, not maximal: scale a set of icons together so their
marks carry the same weight, with the largest reaching 32. A circular mark runs large, a
sparse one smaller.

## The palette

Each line is one deeper colour and the lighter ones it may sit with. A pair works either
way round, so either of the two can be the background.

```
#0B292B    #CECD0C  #E4999A  #F5D6A0
#492222    #8EAFCB  #CFC7B3  #E4999A  #E9ECD9  #F5D6A0  #FCEFD4
#4B4C08    #CECD0C  #E4999A  #E9ECD9  #F5D6A0  #FCEFD4
#97630C    #E9ECD9  #FCEFD4
#D26645    #E9ECD9  #FCEFD4
#F50D00    #E9ECD9  #F5D6A0  #FCEFD4
```

## The file

```svg
<svg xmlns="http://www.w3.org/2000/svg" width="48" height="48" viewBox="0 0 48 48" fill="none">
<rect width="48" height="48" rx="15.36" fill="BACKGROUND"/>
<path d="..." fill="FOREGROUND"/>
</svg>
```

- **The root keeps `fill="none"` and every shape names its own fill.** An icon whose root
  says nothing about fill is inked with `currentColor` by the shell -- that is how the
  workspace's built-in line glyphs take the colour of the text beside them -- which would
  flatten a two-colour tile into one.
- **No shadow in the file.** Every surface that draws an icon casts its own
  (`--desk-icon-shadow`), so a shadow baked into the markup doubles it. Drop any
  `<filter>` a drawing tool's export brings along.
- **Clip only for a deliberate bleed.** A mark inside the 32 box needs no clip. A mark
  drawn to the tile's edge does, or the rounded corners will not cut it -- wrap it in a
  group with `clip-path="url(#<app>-tile)"` and give the `clipPath` the same rounded rect
  as the background. Put the clip on a group with no transform of its own, so the clip
  resolves in the icon's own `0 0 48 48` space.
- The markup is validated on the way into the registry and again in the browser: exactly
  one bare `<svg>` element (no XML declaration), nothing that executes, navigates, or
  loads a resource, only `#fragment` references into the icon itself, and at most 16384
  characters. `validate_icon` in `system/scripts/forward_port.py` is the first gate;
  `appIcon.ts`'s DOMPurify sanitizer is the last.

## Drawing one

The mark is drawn, not assembled:

1. Pick the pair, and decide which of the two is the background.
2. Draw the glyph inside a 32 by 32 box and place that box in the middle of the tile.
   Size it by eye against the icons already drawn, not by filling the box.
3. Let the line stay hand-made -- points placed by eye, edges that are not ruler-straight,
   curves that are not machine-perfect -- without adding noise to fake it.

**Changing one is a move, not a redraw.** To adjust an icon that already exists, transform
the paths that are there -- a `translate`/`scale` on a wrapping group, or the points
themselves -- and never re-emit the path data from parsed geometry or redraw it to "clean
it up". The points are placed by hand, and that is what keeps the line from looking
machine-made.

## What the workspace draws behind one

Nothing. A desktop shortcut hands the icon its whole `--desk-icon-size` box and paints no
surface and no padding behind it: the tile in the file is the whole picture. What the
chrome still owns is the shadow (`--desk-icon-shadow`) and the hover, which is why the box
keeps the matching rounding even though it paints nothing.

That is also why an app which registered **no** icon is given a tile rather than a line
glyph. `appMonogramMarkup` draws its initial on `#F5D6A0` under `#492222`, to these same
rules -- with nothing painted behind it, a transparent letter would be left floating on the
wallpaper, and a tile in the wallpaper's own colour would be no better.

An icon that brings no tile of its own gets the same one. A glyph drawn before these rules
has no filled shape and takes `currentColor` from the text beside it, which is nothing to
take once the surface paints nothing: `sanitizeIconMarkup` puts it on the tile and inks it,
scaled into the centred 32 box rather than over the whole 48, since such a glyph was drawn
to fill its own frame edge to edge. An icon with a filled shape is left exactly as it is.
