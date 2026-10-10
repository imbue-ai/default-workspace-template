The browser no longer renders the pages of windows that are hidden or completely covered by other windows. An app that animates all the time (a spinning 3D globe, a canvas loop) kept rendering at full frame rate behind other windows, and a busy desktop could hold the whole renderer above 100% CPU.

- A page the windows in front of it completely cover is moved out of the viewport and made invisible, which stops the browser rendering it in Chromium, Safari and Firefox. It is still shown as far as the app is concerned: it gets no `shell:hidden`. Rounded corners are respected, so the notches two snapped windows leave over a third keep that page live. A page whose content is wholly outside the desktop is parked too. The focused window is never parked, a window still sliding into place covers nothing until it lands, one sliding away uncovers at once, and nothing is covered while a window is being dragged or resized.

- Minimized windows and windows on another desktop are hidden the same way instead of with `display: none`. Their apps keep their size and scroll position (`display: none` collapsed them to 0x0 and reset the page's scroll), and Safari now stops rendering them too.

- A hidden page whose contract connects after its load is told `shell:hidden` again, since it missed the one sent at load.
