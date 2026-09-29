# App icons

An app's icon is the tile the workspace draws wherever the app appears: its desktop
shortcut, the rail's shortcut and tab rows, the "All apps" popover, the launcher tables,
and a dock tab's leading glyph. The file sits beside the app's manifest as
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

- **216 by 216, with a corner radius of 32 percent** of the size (`rx="69.12"` at 216). The
  shell's `--desk-icon-radius` is the same 32 percent, so the tile keeps its shape
  wherever it is drawn and at whatever size. The tile is *drawn* small -- a desktop
  shortcut is around 48 CSS pixels -- but it is *authored* large, because the grid is what
  caps the detail: a stroke that swells and pinches, or an edge that frays, needs points
  placed between other points, and at 48 there is nowhere to put them.
- **Two layers, a background and a foreground, and nothing else.**
- The **background** covers the whole 216 by 216 and is a single flat colour.
- The **foreground** is one differently-coloured shape that fits a **144 by 144 box centred
  in the background** (`36..180` on both axes). It may be several paths, but it reads as one
  glyph.
- The foreground applies its colour with **fill only** -- no strokes anywhere in the file.
  Outline a stroked drawing rather than shipping the stroke.
- **Interior detail is cut, not painted**: a hole in the foreground path (a subpath wound
  the other way, or `fill-rule="evenodd"`) that lets the background show through. A shape
  in the background's colour laid over the glyph would be a third layer, and would stop
  being background the moment either colour changed.
- The mark is **one iconic object**, named as a noun -- an envelope, a clipboard, a
  funnel, a bell -- that carries the poetic idea of the app. Not a picture of its
  interface, and not an abstraction: the reader should be able to say what the thing is.
- The two colours are **a pair from the palette below**.

Sizing inside the 144 box is optical, not maximal: what should match from one icon to the
next is the weight the mark carries, not the fraction of the box it fills. A circular mark
runs large, reaching 144; a sparse one runs smaller.

## The look

The mark is a real, everyday object, and it is drawn by hand rather than constructed. The
object has to stay the object -- an envelope is a rectangle with a crease, a clipboard has
two long parallel sides, a checkbox is a square, a bullseye is rings around a dot -- and
everything about how it is *made* is loose.

Drawn by hand means:

- **No perfectly straight line.** What reads as straight still wanders a little along its
  run.
- **No machine-drawn curve.** A circle is not quite a circle, and the width of a stroke
  does not sit still along its length. A ring of even thickness is the surest sign
  software drew it.
- **No mirror symmetry.** Whatever is symmetric in the object comes out slightly off in
  the drawing: a square a touch taller on its left than its right, a bell whose two
  shoulders do not match. Nothing folds onto itself.

The deviation is slight, and it lives in the shape -- never in the quality of an edge,
which stays smooth however much the line under it moves. No fray, no chatter, no noise
along a boundary. Corners stay generous and of roughly one radius each; rounding that
varies corner to corner reads as sloppy rather than as hand-made. Every cut in one icon is
drawn to one nominal weight, and that weight is thin against the thing it crosses: a cut
wide enough to halve the body turns the object into two shapes. How thin is a judgement
rather than a measurement, and one icon running heavier than the next is no fault.

**Big, simple marks.** The glyph carries one or two kinds of detail at most -- a cut
stroke *or* a rim, rarely both, never three. When a mark is not reading, make it bigger
and simpler, not busier.

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
<svg xmlns="http://www.w3.org/2000/svg" width="216" height="216" viewBox="0 0 216 216" fill="none">
<rect width="216" height="216" rx="69.12" fill="BACKGROUND"/>
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
- **Clip only for a deliberate bleed.** A mark inside the 144 box needs no clip. A mark
  drawn to the tile's edge does, or the rounded corners will not cut it -- wrap it in a
  group with `clip-path="url(#<app>-tile)"` and give the `clipPath` the same rounded rect
  as the background. Put the clip on a group with no transform of its own, so the clip
  resolves in the icon's own `0 0 216 216` space.
- The markup is validated on the way into the registry and again in the browser: exactly
  one bare `<svg>` element (no XML declaration), nothing that executes, navigates, or
  loads a resource, only `#fragment` references into the icon itself, and at most 16384
  characters. `validate_icon` in `system/scripts/forward_port.py` is the first gate;
  `appIcon.ts`'s DOMPurify sanitizer is the last.

## Drawing one

The mark is drawn, not assembled:

1. Name the object, and pick the pair -- which of the two is the background.
2. Fix the object's geometry inside a 144 by 144 box, and place that box in the
   middle of the tile. Size it by eye, not by filling the box.
3. Fix the nominal weight for the cuts before drawing any of them, and hold that nominal
   for every cut in the icon.
4. Draw it, placing the points already uneven -- rather than laying down something regular
   and disturbing it afterwards.

**Draw it in one pass.** Place the points as you go, and stop when the mark is there. Do
not emit a path and then revise it: nudging it, re-balancing it, regularising it, or
tidying it up. Every pass over the same geometry averages out precisely the unevenness the
look depends on, so a twice-drawn mark reads flatter than a once-drawn one, and a
five-times-drawn one reads like a logo. If a mark comes out wrong, throw it away and draw
the whole thing again rather than correcting the one you have.

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
scaled into the centred 144 box rather than over the whole 216, since such a glyph was drawn
to fill its own frame edge to edge. An icon with a filled shape is left exactly as it is.
