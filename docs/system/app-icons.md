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

Sizing inside the 144 box is optical, not maximal: scale a set of icons together so their
marks carry the same weight, with the largest reaching 144. A circular mark runs large, a
sparse one smaller.

## The look

The mark is a real, everyday object drawn true, and then inked. Those are the two halves
of it, and only the first is strict: the object has to stay the object, and everything
about how it is *made* is loose. The reference is the inked column on Gleb's Copyboard
("Clean vs hand-drawn"), and the bullseye there (node `191:688`) is the one to look at --
its rings are the clearest read on how much unevenness the look wants.

Two things that column settles. The *clean* side is not machine-drawn either: its rings
already vary about a fifth in thickness around their circumference. And the inking pass
does not move the line much -- it changes the *weight* of it. Measured on that bullseye,
the ring edges wander by one or two units in 120 while the ring thickness swings by two
fifths peak to peak. Drift is small; breathing is not.

**Draw it true.**

- **The object keeps its own proportions.** An envelope is a rectangle with a crease; a
  clipboard has two long parallel sides; a checkbox is a square; a bullseye is rings
  around a dot. What is straight reads as straight and what is round reads as round. What
  is *not* required is exactness -- lopsidedness is not the goal, but neither is
  correctness. A silhouette that bows on every edge stops reading as the object and starts
  reading as a cushion, and that is the only failure on this side.
- **Corners are generous and consistent.** A hand-drawn rectangle has four corners of
  roughly one radius, not four different ones. Rounding varying corner to corner reads as
  sloppy rather than as hand-made.
- **One nominal weight, and the set shares it.** Every cut-through stroke in one icon is
  drawn to the same nominal width, and that width carries across the whole set. Different
  *nominals* inside one mark, or from one icon to the next, is what makes a set look
  assembled by different people -- which is a separate thing from the variation around a
  nominal, and that one is wanted.
- **A stroke is thin against the thing it crosses** -- roughly a sixth to an eighth of it.
  The object has to stay the thing you read first; a cut wide enough to halve the body
  turns the object into two shapes.
- **A cut lands where it is aimed.** If the crease runs to the envelope's top corners, it
  reaches them. Stopping a few units short leaves a gap that reads as a mistake rather
  than as a hand.

**Then ink it.**

- **The whole mark sits a little off level.** Not each edge separately -- the object as a
  whole, turned about a degree off its axis. On the board's checkbox the square's right
  side sits a unit higher than its left over a sixty-unit run, where the clean version is
  dead level. Nothing symmetric stays symmetric through this, and it should not: this is
  the deviation that most makes a mark look placed by hand rather than snapped to a grid.
- **The width breathes, and it breathes a lot** -- about a fifth either side of the
  nominal along the run, which is a swing of two fifths from the thinnest place to the
  thickest. This is the big one, and it is the one most likely to be drawn too timidly.
  What it is not is a taper: it thins and fattens and thins again, rather than running
  from fat to thin.
- **The edge drifts off true and comes back**, but only a little -- one to three units in
  216, about one per cent of the tile. Small next to the breathing. The line knows where
  it is going; the pen's pressure is what does not.
- **Ends are round, and they lift** -- the cap sits a few degrees off square to the path,
  as a pen leaving the paper does.
- **Nothing is ragged.** No fray, no chatter, no noise along an edge. Unevenness lives in
  the *width* of a stroke and the *tilt* of the mark, never in the quality of its
  boundary, which stays smooth however much the width under it moves.
- **An outline is a ring of one nominal, breathing** -- a fifth either way. A ring of
  constant thickness is the surest sign a machine drew it.

**Big, simple marks.** The glyph carries one or two kinds of detail at most -- a cut
stroke *or* a rim, rarely both, never three. When a mark is not reading, make it bigger
and simpler, not busier.

You do not draw it true and then edit it: you place the inked points directly, knowing
what true was. That is what keeps this compatible with drawing in one pass.

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
2. Fix the object's true geometry inside a 144 by 144 box, and place that box in the
   middle of the tile. Size it by eye against the icons already drawn, not by filling the
   box.
3. Fix the nominal weight for the cuts before drawing any of them, and hold that nominal
   for every cut in the icon.
4. Turn the whole thing about a degree off its axis, so it sits off level as one piece
   rather than edge by edge.
5. Place the points inked: the line a unit or two either side of true, and the width a
   fifth either side of its nominal, thinning and fattening and thinning again along the
   run. The line is the thing that stays put; the width is the thing that moves.

**Draw it in one pass.** Place the points as you go, and stop when the mark is there. Do
not emit a path and then revise it: nudging it, re-balancing it, regularising it, or
tidying it up. Every pass over the same geometry averages out precisely the unevenness the
look depends on, so a twice-drawn mark reads flatter than a once-drawn one, and a
five-times-drawn one reads like a logo. If a mark comes out wrong, throw it away and draw
the whole thing again rather than correcting the one you have.

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
scaled into the centred 144 box rather than over the whole 216, since such a glyph was drawn
to fill its own frame edge to edge. An icon with a filled shape is left exactly as it is.
