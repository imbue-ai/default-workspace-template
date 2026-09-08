- The vendored viewer colours a trajectory by who produced each step instead of striping it by
  parity. The agent speaks for most of a run and stays unmarked; the simulated user's turns and the
  workspace's own `system` steps each get their own band, so the conversation is legible while
  scrolling a two-hundred-step trajectory.

- Step boundaries render as a seam rather than as a message. The harness tags its own steps in ATIF
  `extra`, and the viewer reads that tag to draw a labelled divider, dropping the ASCII rule from the
  text since that rule exists only so the marker stays findable in a viewer that renders none of
  this. Stock `harbor view` is unchanged and still shows the step as ordinary system prose.

- A test checks that the `extra` namespace the harness writes is the one the viewer reads. The two
  sides are a Python dict and a TypeScript literal with nothing between them, so a rename would
  otherwise just quietly stop drawing the dividers.
