A permission card whose service logo fails to load now shows the generic cube instead of a broken image.

The card picks a bundled brand mark from the request's scope and draws it as an `<img>`. It already fell back to the cube for a service we ship no artwork for, but a mark that failed to *load* had no handling: the browser's broken-image glyph stood in for the logo and never recovered, since nothing re-requests the image, so the card read as one that had failed rather than one waiting on the user.

The `<img>` is now its own load probe. A failed URL is remembered module-wide and every card showing that service draws the cube -- the same shape the minds chrome's own ServiceMark uses, and for the same reason: whether an asset is reachable is a property of the server, not of the card asking.

The marks are content-hashed build assets served with `no-cache`, and a build empties `static/` before re-emitting, so the window this covers is a mark requested while the frontend is being rebuilt (routine in a dev loop) -- previously that left a permanently broken glyph until the page was reloaded.
