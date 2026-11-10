A new built-in app, What Studio knows about me (`about-me`, port 8070): a view onto the workspace agent's own memory (`data/memories/`), so you can see, add to, edit and delete everything it remembers about you without asking it.

- Every memory is listed, grouped by what it is about: About you, How you like to work, Your projects, Links and references. Each has Edit (its summary and text) and Delete (the file and its `MEMORY.md` line).

- The web lookup's memory shows fact by fact under About you, each with its source link and date, its own Edit (words only) and Delete (the line, and the sources nothing else cites), and Forget what the lookup found.

- Add something about yourself writes a new memory in the format the agent writes its own, so the next chat knows it. Forget everything, after a confirm, deletes every memory.

- Every change names the file's content by a hash, so a page left open while the agent rewrote a memory reloads instead of overwriting it. Writes are atomic and serialized. The app never reaches the network.

- The page is plain HTML on the page kit and the design tokens (`[theming] mode = "tokens"`), so it wears every workspace theme. A new desktop gets a shortcut to it; it stops when no window shows it, and sits under the Share tab's System group.
