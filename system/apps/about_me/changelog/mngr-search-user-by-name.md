A new built-in app, What Studio knows about me (`about-me`, port 8070): one page showing what Studio remembers about you, from what you told it and the opt-in web lookup by name, so you can see, add to, and delete all of it without asking the agent.

- Each fact in `data/memories/user-public-profile.md` is a row with its source as a link (or "You told me" for something you shared) and the date it was found, and a Delete button. A line not in the lookup's fixed shape is still shown and deletable whole; the frontmatter and the closing sentence are not facts.

- An Add box keeps a fact you type about yourself as a "You told me" line after the last fact, creating the memory, its `MEMORY.md` line and your own sources entry when nothing is remembered yet. An empty entry, one over 500 characters, or one holding `<!--` or `-->` is refused with a message.

- Delete removes exactly that line and, from `data/.skills/personalized-ideas/sources.json`, the sources only that line cited (and your own entries once no "you told me" fact remains). Deleting the last fact forgets everything.

- "Forget all of this", after a confirm, deletes the profile, its line in `MEMORY.md` (the rest of the index untouched) and `sources.json`. An empty state says you can add something or ask for a lookup, and "View raw file" shows both files.

- A delete names its fact by line and content, so a page left open while the memory changed gets a 409 and reloads instead of deleting the wrong line. Writes are atomic and serialized. The app never reaches the network.

- The page is plain HTML on the page kit and the design tokens (`[theming] mode = "tokens"`), so it wears every workspace theme. It is listed in the launcher (after the ranked apps), seeds no desktop shortcut, stops when no window shows it, and sits under the Share tab's System group.
