- The viewer gained a **UI flows** tab, offered on any trial that recorded flows. Until now a flow's
  evidence was reachable only as raw files: a JSON manifest for the verdicts and a JSONL log for what
  the browser agent did, or as the grade-time judge's digest of them.

- Flows are listed with their verdict and, when a flow failed, the reason. A case that declares both
  task-level and step-level expectations groups them under their scope, so the two sets stay legible
  side by side on the final step.

- A selected flow reads like a trajectory: its opening, then one block per step with the action
  taken, what the agent expected of it, what the page did, and the screenshot it captured, in order.
  The accessibility snapshot of each page is available too, behind a toggle -- it is by far the
  largest thing recorded, a few hundred kilobytes for a sixteen-step flow.

- Screenshots, reasoning and page state each have a quick toggle in the tab header. Page state
  starts off.

- A flow's log names what kind each of its lines is. **init** opens the flow with what it was asked
  to do, what it expects, and the page it opened onto; **action** is a step; **final** is the
  agent's closing reading. A reader that meets a kind it does not know shows the record as it
  stands rather than dropping it, so a new kind can be added without every reader learning it first.

- A flow now opens with the page it started from. The opening navigation already captured a frame
  and a page state before any action was decided, and both were recorded nowhere -- so the first
  thing a reader saw was the frame that *followed* the first action, with nothing saying what the
  flow was aiming at.

- The flow agent records what it expects an action to do, alongside its reasoning, and is then
  shown what the page actually did with it. An app whose control does something other than what the
  agent modelled now reads as a contradiction on the next turn instead of being re-derived and
  retried.

- The history the agent is prompted with has a budget of its own. It grows with every step while
  the page state is capped, so a long flow would otherwise end up reasoning mostly about what it
  already did rather than about the page in front of it: recent steps go in whole, older ones keep
  only their action, and anything that still does not fit is dropped and counted.

- What the page did is summarised as the lines that moved, bounded, falling back to how much moved
  once the page has effectively been replaced. An action that landed and changed nothing says so,
  and so does one that removed a row the page still shows another copy of.

- The summary ignores what the browser rewrites on every render -- element ids, focus, cursor --
  and reports the content that actually moved. Comparing those made an untouched page look
  rewritten: on a live run one step reported 27 changed lines in a 24-line tree, and a Delete click
  that did nothing at all was reported as the button gaining focus, which is what let the agent
  click it a second time.
