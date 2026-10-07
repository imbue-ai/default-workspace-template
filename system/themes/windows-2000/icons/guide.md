# Windows 2000 icons

Icons as Windows 2000 drew them: 32 by 32 pixels, at most 48 colors, no partial transparency.

- **Three-quarter view from slightly above**, light from the upper left: lit faces are lighter, the lower right
  side and a one-pixel drop shadow toward the lower right are darker.
- **A dark outline** (one pixel, near black or a dark shade of the object's own color) around the object.
- **Saturated but not neon** colors in a few flat shades each: a yellow folder (`#f8d870`, `#e0b030`, `#906818`),
  a white page, a blue screen (`#0a246a`, `#3a6ea5`, `#a6caf0`), gray metal and plastic (`#d4d0c8`, `#808080`,
  `#404040`).
- **One familiar desktop object per app**: a folder for files, a speech balloon for chat, a command prompt
  window for the terminal, a globe for the browser, a document with a star for getting started. The object
  fills roughly 26 to 30 pixels of the canvas, centred, with clear corners.
- **Avoid** gradients smoother than the 48 colors allow, glow, text inside the icon, and modern flat design.

`icons/files.png`, `icons/chat.png`, and `icons/terminal.png` show the style. `workspace-themes icon fit` scales
any drawing down to 32 pixels without smoothing, limits it to 48 colors, and clears its background.
