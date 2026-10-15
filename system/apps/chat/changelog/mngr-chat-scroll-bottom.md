The transcript can always be scrolled all the way to its bottom, however tall the composer is. Before, a draft long enough to grow the composer pushed the last message under it: the transcript sat short of its bottom and kept getting pulled back up, so scrolling down could not reach the end.

- The composer no longer collapses to one line for a moment each time it measures its text (every redraw), which made the browser pull the transcript's scroll position up to fit that one-line layout.

- When the composer shrinks back (a draft cleared or sent), the transcript keeps following new messages at its bottom. The browser's adjustment as the transcript grew taller was read as the user scrolling up, which stopped the follow.
