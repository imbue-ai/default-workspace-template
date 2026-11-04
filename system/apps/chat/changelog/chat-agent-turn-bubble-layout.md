The agent's side of the chat follows the multiplayer-chat prototype.

- The agent's name and face show once per stretch of its output, which runs until a person speaks or another agent takes over. Before, they repeated over every bubble that followed a tool call.

- Each message's prose is a bubble. A turn's closing reply below its steps is now a bubble too, not plain text.

- Tool calls, the step timeline and system notices sit between the bubbles on their text edge, 12px apart. A thin line runs through their icons, bullets and ticks.

- The call the agent is running shows in its own chip on the line, with the working dot and the turn's clock, in place of the status line. A call inside a step's work is said by the status line, which joins the line as the stretch's last piece. The done mark is a small receipt just under the stretch.

- "You" shows over the first of a run of your own messages. Another person's name and picture show once per run of theirs.

- A command you run in bash mode (`!ls`) and its output stand on your side and end the agent's stretch, as a new `person_chip` display kind.

- The chevron beside the step the agent is working on no longer draws doubled once that step is opened: the title's shimmer now sweeps only its words.
