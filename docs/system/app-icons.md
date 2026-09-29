# App icons

An app's icon is the 48 by 48 tile the workspace draws wherever the app appears: its
desktop shortcut, the rail's shortcut and tab rows, the "All apps" popover, the launcher
tables, and a dock tab's leading glyph. The file sits beside the app's manifest as
`system/apps/<package>/icon.svg`, and `app.toml`'s `icon` names it. `forward_port.py`
reads the file at registration and stores the *markup* on the registry row, so an icon
changes on the app's next registration -- normally its next restart -- and never needs a
rebuild of the shell.

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
  Outline a stroked drawing before exporting it.
- The mark is a **single iconic glyph that carries the poetic idea of the app**, not a
  picture of its interface. Its line quality is hand-drawn: no ruler-straight edges and no
  machine-perfect curves, but not noisy either.
- The two colours are **a pair from the palette below**.

Sizing inside the 32 box is optical, not maximal: scale a set of icons together so their
marks carry the same weight, with the largest reaching 32. A circular mark runs large, a
sparse one smaller.

## The palette

The colours and the pairings are Gleb's contrast board, Figma file
`9xCvSTxPMbiN2VAHqadTyA` node `195:1070` (the colours themselves are Figma variables, so
they carry these names in the file). Each line is a background with the foregrounds that
may sit on it, and the contrast ratio the board gives the pair.

| Colour | Hex | Colour | Hex |
| --- | --- | --- | --- |
| Confusion | `#0B292B` | Belonging | `#E4999A` |
| Courage | `#492222` | Strength | `#CFC7B3` |
| Envy | `#4B4C08` | Inspiration | `#E9ECD9` |
| Peace | `#8EAFCB` | Comfort | `#F5D6A0` |
| Curiousity | `#97630C` | Clarity | `#FCEFD4` |
| Confidence | `#F50D00` | Energy | `#CECD0C` |
| Respect | `#D26645` | | |

```
Energy #CECD0C        Confusion #0B292B    9.04 AAA
Respect #D26645       Clarity #FCEFD4      3.21 AA
                      Inspiration #E9ECD9  3.04 AA
Strength #CFC7B3      Courage #492222      8.12 AAA
Belonging #E4999A     Courage #492222      6.07 AAA
                      Confusion #0B292B    6.82 AAA
Confusion #0B292B     Energy #CECD0C       9.04 AAA
                      Comfort #F5D6A0     10.98 AAA
                      Belonging #E4999A    6.82 AAA
Inspiration #E9ECD9   Confidence #F50D00   3.53 AA
                      Curiousity #97630C   4.25 AA
                      Envy #4B4C08         7.47 AAA
                      Courage #492222     11.37 AAA
Clarity #FCEFD4       Confidence #F50D00   3.73 AA
                      Curiousity #97630C   4.49 AA
                      Envy #4B4C08         7.89 AAA
                      Courage #492222     12.00 AAA
Comfort #F5D6A0       Confidence #F50D00   3.03 AA
                      Envy #4B4C08         6.42 AAA
                      Confusion #0B292B   10.98 AAA
                      Courage #492222      9.77 AAA
Curiousity #97630C    Clarity #FCEFD4      4.49 AA
                      Inspiration #E9ECD9  4.25 AA
Envy #4B4C08          Energy #CECD0C       5.29 AAA
                      Belonging #E4999A    3.98 AA
                      Comfort #F5D6A0      6.42 AAA
                      Clarity #FCEFD4      7.89 AAA
                      Inspiration #E9ECD9  7.47 AAA
Courage #492222       Belonging #E4999A    6.07 AAA
                      Comfort #F5D6A0      9.77 AAA
                      Strength #CFC7B3     8.12 AAA
                      Inspiration #E9ECD9 11.37 AAA
                      Peace #8EAFCB        5.95 AAA
```

Contrast is direction-independent, so a pair works either way round; the grouping is how
the board draws it. The board grades on WCAG's large-text scale (AA at 3:1, AAA at 4.5:1),
which is the right scale for a mark this size -- but it means the pairs graded AA are only
safe as a solid shape, never as small detail or text.

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
  (`--desk-icon-shadow`), so a shadow baked into the markup doubles it. Figma's tiles carry
  the same shadow as an effect for the same reason: drop the `<filter>` the export brings.
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

The glyphs are drawn by hand in Figma (the tiles live in file `9xCvSTxPMbiN2VAHqadTyA`),
and the way into the repo is an export, not a redraw:

1. `download_assets` on the tile's node with `defaultFormat: "svg"` gives the node's SVG.
   The export nests the tile inside the surrounding canvas, so it carries the page's own
   background rects and the shadow filter.
2. Keep the tile's `<rect ... rx="15.36">` and the glyph's `<path>` elements; drop
   everything else.
3. Move the glyph onto the icon's own grid with a `transform="translate(...)"` on a
   wrapping group -- the export places the tile wherever it sat on the canvas.

**Never re-emit the path data.** Move and clip the paths that were exported; do not
rebuild them from parsed geometry, and do not redraw them to "clean them up". The points
are hand-placed, and that is what keeps the line from looking machine-made.

## The icons we have

| App | Background | Foreground | Contrast | Figma node |
| --- | --- | --- | --- | --- |
| chat | Comfort `#F5D6A0` | Confidence `#F50D00` | 3.04 | `195:416` |
| terminal | Envy `#4B4C08` | Comfort `#F5D6A0` | 6.42 | `195:345` |
| files | Inspiration `#E9ECD9` | Courage `#492222` | 11.37 | `195:349` |
| browser | Confusion `#0B292B` | Peace `#8EAFCB` | 6.69 | `221:516` |
| getting_started | Energy `#CECD0C` | Confusion `#0B292B` | 9.04 | drawn here |

Three of the four sit inside the 32 box. The chat icon's figure is the exception: it
stands 38.4 tall on the tile's bottom edge and is clipped by the corner radius, which is
why it is the one icon in the set that carries a `clipPath`. The browser's pair is not one
the board draws, though at 6.69 it clears the board's AAA.

`getting_started`'s flag is the one glyph in the set not drawn on the Copyboard: it was
drawn to these rules in the repo, as a planted banner -- a bowed pole and a cloth that
droops unevenly, so the line reads as drawn rather than ruled. Its pair is the board's
Confusion on Energy, the only yellow tile in the set.

## What the workspace draws behind one

Nothing. A desktop shortcut hands the icon its whole `--desk-icon-size` box and paints no
surface and no padding behind it: the tile in the file is the whole picture. What the
chrome still owns is the shadow (`--desk-icon-shadow`) and the hover, which is why the box
keeps the matching rounding even though it paints nothing.

That is also why an app which registered **no** icon is given a tile rather than a line
glyph. `appMonogramMarkup` draws its initial on the palette's most neutral pair (Strength
under Courage), to these same rules -- with nothing painted behind it, a transparent letter
would be left floating on the wallpaper.
