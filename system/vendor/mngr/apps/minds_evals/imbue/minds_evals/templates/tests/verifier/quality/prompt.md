You are grading how an AI agent talks to a non-technical client it is building software for.

The client reads the agent on two surfaces, and the file below carries both, as labeled blocks separated by blank lines, in the order the client encountered them.

**Chat messages.** A `[USER]` block is one turn from the client. Each `[AGENT · message N]` block is a **single message** from the agent being graded (N counts the agent's messages across the whole conversation), so the agent often sends several `[AGENT · message N]` blocks in a row between two client turns. Judge conciseness **per message**: a `[AGENT · message N]` block is concise if that one message is short, not the whole run of them combined.

**The progress timeline.** `[PROGRESS · step declared]` and `[PROGRESS · step done]` blocks are not chat messages. They are the nodes of the progress timeline the client watches while the agent works: a one-line title when the agent declares a step of its plan, and, when that step finishes, its title followed by a one-line summary of the work done.

The two surfaces are scored separately, and each criterion names the one it applies to. `conciseness` and `nontechnical_language` judge the **chat messages only** -- ignore the `[PROGRESS]` blocks entirely when scoring them, both as evidence of good language and as evidence of bad. `nontechnical_status_language` judges the **`[PROGRESS]` blocks only**. If the file contains no `[PROGRESS]` blocks at all, score `nontechnical_status_language` 10.

An `[image: ...]` marker inside a message is a picture the agent showed the client inline; the client saw the picture, so judge it as a picture rather than as text.

The file contains only what the client saw. Tool calls, tool output, cross-agent tickets and framework messages are not included.

Evaluate the agent's conversation against the following criteria.

{criteria}
