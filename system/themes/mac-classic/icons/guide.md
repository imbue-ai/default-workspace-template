# Classic Mac icons

Icons as the Macintosh drew them in System 6 and 7: 32 by 32 pixels, one bit deep.

- **Black and white only.** Every pixel is black (`#000000`), white (`#ffffff`), or clear. No gray, no
  anti-aliasing; a mid-tone is a checkerboard dither of black and white.
- **A one-pixel black outline** around the whole object, so it reads on the gray dither desktop and on white.
  Inside the outline, the object is mostly white, with black for its details.
- **Straight-on or a slight three-quarter view**, light from the upper left: a shadow, where there is one, is a
  one-pixel black line on the lower and right edges.
- **One familiar object per app**, the way the Finder showed a document or an application: a folder for files, a
  speech balloon for chat, a terminal screen for the terminal. The object fills roughly 24 to 30 pixels of the
  canvas, centred, with clear corners.
- **Avoid** text inside the icon, color, soft gradients, and more detail than 32 pixels can carry.

`icons/files.png`, `icons/chat.png`, and `icons/terminal.png` show the style. A model that cannot draw one bit
deep can draw in two flat colors at a larger size; `workspace-themes icon fit` maps every pixel to black or white
and clears the background.
