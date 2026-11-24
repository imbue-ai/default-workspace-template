---
name: do-something-new
description: Use immediately when the user asks you to do something net-new -- a task you haven't done before, no existing skill or app applies, and getting it right will require nontrivial research, exploration, or experimentation. Routes the request to the right interactive flow. When doing this, give a very short confirmation message to the user's request, then load this immediately before responding further. Your confirmation message shouldn't mention loading the skill. Skip when an applicable skill or app already exists or for pure dev/code-writing work.
metadata:
  author: imbue
---

# Doing something new

A net-new task that needs research or experimentation has landed. This skill is a
thin **router**: it scans for an existing skill, then sends you to the right
interactive flow. The actual flow lives in the skill you route to (or, for the
rare task that fits neither, in the list under "Neither cleanly applies").

The overriding goal of any net-new interactive task is the same: deliver a result
the user cares about *fast*, confirm the basic shape cheaply, and defer the
expensive thorough work to the background.

## Step 0: Existing-skill scan

Match the user's ask against the descriptions of the existing skills in
`.agents/skills/`. If a *single* skill covers the *whole* ask, bail out:
invoke that skill and stop, without naming it to the user. For *partial*
matches (a skill covers a subset
of the ask), continue routing but reuse the matching skill for the subset it
handles.

## Step 1: Route

Pick the flow that fits the ask:

- **Fetch / process / show data** -- the ask is "go get this data (from an
  external service, API, or third-party source), do something to it, and show me
  the result." Route to **`fetch-process-show`**.

- **Build a web view** -- the ask is "build me a page / dashboard / app I can
  look at." Route to **`build-app`**. It owns the interactive
  mock-confirmation flow for web work.

- **A hybrid: a web view over fetched data** -- start with `fetch-process-show`
  to confirm the data sample, then it hands the surface to `build-app`
  (which runs its own UI-mock confirmation on top of the confirmed data). Begin
  with `fetch-process-show`.

- **Neither cleanly applies** -- the task is genuinely net-new and not a data
  fetch or a web view. Run the same loop yourself, in this order:
  1. Ask only the questions whose answers change what you do next, in the
     user's terms; zero is fine. Default everything else to the simplest
     choice, for one user and the data at hand, and say so in a line.
  2. A fast feasibility pass: a few minutes, enough to know the task is
     possible.
  3. Propose a small plan (the approach, and what they will see at the end)
     and wait for a go-ahead.
  4. Exercise first whatever could sink the task and is not under your control
     (an external API, an auth flow).
  5. Put the smallest real, throwaway version in front of the user and loop on
     it, each round visibly applying their feedback, until they **explicitly
     confirm** the shape. Build nothing production-grade before that. Your
     own check ends at "it came up", before they see it: once it is in front
     of them, do not drive it yourself.
  6. Only then harden it (tests, review gates), in a background worker through
     the `crystallize-creation` skill, never in this chat.
  7. Each further capability is its own delivery with its own confirmation:
     ask whether to build the next one, and wait.

Load the routed skill and proceed; do not re-implement its flow here.

## When NOT to use this skill

- An applicable single skill already exists -- use it.
- Pure code-writing or dev work (bug fixes, refactors, adding a button). Those
  aren't research/exploration tasks.
